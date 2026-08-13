from __future__ import annotations

import logging
import traceback
from typing import Any, Dict, Optional

from celery import shared_task
from django.utils import timezone

from api_etl.apps import ApiEtlConfig
from api_etl.models import PulledHistory
from api_etl.paa_aliases import (
    are_paa_equivalent,
    district_suffixes_conflict,
    get_paa_scope,
)
from api_etl.utils import ETL_CLASS, get_class_by_name, get_classes_in_module
from core.signals import register_service_signal

logger = logging.getLogger(__name__)


def _username(user) -> Optional[str]:
    return getattr(user, "username", None) or getattr(user, "login_name", None)


def _extract_questionnaire_version(questionnaire_id: Optional[str]) -> Optional[int]:
    if not questionnaire_id or "$" not in str(questionnaire_id):
        return None
    try:
        return int(str(questionnaire_id).rsplit("$", 1)[1])
    except (TypeError, ValueError):
        return None


def _save_history(history: PulledHistory, user=None, update_fields=None):
    username = _username(user)
    if username:
        history.save(username=username, update_fields=update_fields)
    else:
        history.save(update_fields=update_fields)


class ApiEtlService:
    """Carries the service signals api_etl publishes. A class, not a bare function: the core
    decorator takes args[0] as the signal sender, and receivers read the actor off it."""

    def __init__(self, user=None):
        self.user = user

    @register_service_signal("api_etl_service.import_finished")
    def import_finished(self, *, history_id, status, paa_name="", counts=None):
        """Terminal state of a PAA ETL run. api_etl does nothing with it; listeners bind
        AFTER. Passed by keyword so receivers read data[1]."""
        return {
            "history_id": history_id,
            "status": status,
            "paa_name": paa_name,
            "counts": counts or {},
        }


def _emit_finished(history: PulledHistory, user=None):
    """Announce a terminal PulledHistory state. Never raises: no listener may break an import."""
    try:
        ApiEtlService(user=user or getattr(history, "user_initiated", None)).import_finished(
            history_id=str(history.id),
            status=history.status,
            paa_name=history.paa_name or "",
            counts={
                "individuals": history.n_individuals_inserted,
                "households": history.number_of_households,
            },
        )
    except Exception:
        logger.warning(
            "api_etl: import_finished signal failed for history=%s", history.id, exc_info=True
        )


def _mark_failed(history: PulledHistory, message: str, user=None):
    history.status = "failed"
    history.error_message = message
    if not history.json_ext:
        history.json_ext = {}
    history.json_ext.update(
        {
            "task_status": "failed",
            "failed_at": timezone.now().isoformat(),
        }
    )
    _save_history(history, user=user, update_fields=["status", "error_message", "json_ext"])
    _emit_finished(history, user=user)


def _validate_questionnaire_for_paa(
    *, questionnaire_id: Optional[str], paa_name: Optional[str]
) -> Optional[str]:
    if not questionnaire_id or not paa_name:
        return None

    from api_etl.gql_queries import (
        _extract_district_from_questionnaire,
        _strip_district_suffix,
    )
    from api_etl.sources.survey_solutions_export_source import (
        SurveySolutionsExportSource,
    )

    source = SurveySolutionsExportSource()
    source.base_url = getattr(ApiEtlConfig, "export_base_url", "")
    source.workspace = getattr(ApiEtlConfig, "export_workspace", "")
    source.meta_api_prefix = getattr(ApiEtlConfig, "meta_api_prefix", "/api/v1")

    questionnaires = source.list_questionnaires()
    selected_q = next(
        (
            q
            for q in questionnaires
            if q.get("Identity") == questionnaire_id or q.get("Id") == questionnaire_id
        ),
        None,
    )
    if not selected_q:
        return None

    q_title = selected_q.get("Title", "")
    suffixes = getattr(ApiEtlConfig, "district_name_suffixes", ["DC", "TC", "MC"])
    prefix = getattr(
        ApiEtlConfig,
        "questionnaire_title_prefix",
        "DODOSO LA KAYA-RM4-",
    )

    q_district = _extract_district_from_questionnaire(q_title, prefix)
    q_district_base = _strip_district_suffix(q_district, suffixes)
    paa_base = _strip_district_suffix(paa_name, suffixes)

    if district_suffixes_conflict(q_district, paa_name, suffixes):
        return (
            "Questionnaire validation failed: "
            f"Selected questionnaire '{q_title}' targets a different council type than "
            f"PAA/district '{paa_name}'. Expected PAA/district: '{paa_name}', Found: '{q_district}'"
        )

    if are_paa_equivalent(q_district_base, paa_base, ApiEtlConfig):
        return None

    return (
        "Questionnaire validation failed: "
        f"Selected questionnaire '{q_title}' does not belong to PAA/district '{paa_name}'. "
        f"Expected PAA/district: '{paa_name}', Found: '{q_district}'"
    )


def execute_paa_etl_history(history_id: str, user_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    from core.models import User
    from core.utils import clear_current_user, set_current_user

    from api_etl.services.survey_solution_service import find_matching_questionnaire

    user = User.objects.get(id=user_id)
    history = PulledHistory.objects.get(id=history_id)
    set_current_user(user)

    paa_name = params.get("paa_name")
    district_code = params.get("district_code")
    region_code = params.get("region_code")
    questionnaire_id = params.get("questionnaire_id")
    dry_run = bool(params.get("dry_run", False))
    batch_id = params.get("batch_id")
    matching_paa_name = (
        get_paa_scope(name=paa_name, code=district_code, config=ApiEtlConfig)
        or paa_name
    )

    try:
        if not history.json_ext:
            history.json_ext = {}
        history.json_ext.update(
            {
                "task_status": "started",
                "started_at": timezone.now().isoformat(),
            }
        )
        _save_history(history, user=user, update_fields=["json_ext"])

        try:
            mismatch = _validate_questionnaire_for_paa(
                questionnaire_id=questionnaire_id,
                paa_name=paa_name,
            )
            if mismatch:
                _mark_failed(history, mismatch, user=user)
                return {"success": False, "message": mismatch}
        except Exception as validation_error:
            logger.error(
                "Questionnaire validation error (non-blocking): %s",
                str(validation_error),
                exc_info=True,
            )

        etl_service_class = get_class_by_name(ETL_CLASS, "SurveySolutionService")
        if not etl_service_class:
            available_services = get_classes_in_module(ETL_CLASS)
            if available_services:
                etl_service_class = get_class_by_name(ETL_CLASS, available_services[0])
            else:
                raise RuntimeError("No ETL service available")

        service_config = {
            "paa_name": paa_name,
            "district_code": district_code,
            "region_code": region_code,
            "questionnaire_id": questionnaire_id,
            "dry_run": dry_run,
            "batch_id": batch_id,
        }
        etl_service = etl_service_class(user, config=service_config)

        match_info = {}
        if not questionnaire_id and paa_name:
            try:
                from api_etl.gql_queries import resolve_available_questionnaires

                matches = resolve_available_questionnaires(
                    info=None,
                    districtName=matching_paa_name,
                    districtCode=district_code,
                    regionCode=region_code,
                    showAll=False,
                )
                if matches:
                    questionnaire_id = matches[0].identity
                    match_info = {
                        "questionnaire_title": matches[0].title,
                        "questionnaire_version": matches[0].version,
                        "matching_strategy": matches[0].matching_strategy,
                        "candidates_count": len(matches),
                        "error": None,
                    }
            except Exception as exc:
                logger.error("Auto-selection of questionnaire failed: %s", str(exc), exc_info=True)

        if not questionnaire_id:
            questionnaire_id, match_info = find_matching_questionnaire(
                district_name=matching_paa_name,
                district_code=district_code or "",
                region_code=region_code or "",
                source=etl_service.source,
                config=etl_service.config,
                manual_questionnaire_id=None,
            )

        if not questionnaire_id:
            error_message = (match_info or {}).get("error") or "No matching questionnaire found"
            _mark_failed(history, error_message, user=user)
            return {"success": False, "message": error_message}

        if (
            questionnaire_id
            and not (match_info or {}).get("questionnaire_version")
        ):
            match_info = {
                **(match_info or {}),
                "questionnaire_version": _extract_questionnaire_version(questionnaire_id),
            }

        history.questionnaire_id = questionnaire_id
        history.questionnaire_title = (match_info or {}).get("questionnaire_title")
        history.questionnaire_version = (match_info or {}).get("questionnaire_version")
        history.matching_strategy = (
            (match_info or {}).get("matching_strategy")
            or history.matching_strategy
            or ("manual-override" if params.get("questionnaire_id") else None)
        )
        _save_history(
            history,
            user=user,
            update_fields=[
                "questionnaire_id",
                "questionnaire_title",
                "questionnaire_version",
                "matching_strategy",
            ],
        )

        out = etl_service.run(questionnaire_id=questionnaire_id, dry_run=dry_run)

        history.status = "completed"
        if not history.json_ext:
            history.json_ext = {}
        history.json_ext.update(
            {
                "task_status": "completed",
                "completed_at": timezone.now().isoformat(),
            }
        )
        history.update_counts_from_etl_result(out, user=user)

        history.refresh_from_db()
        if history.status != "completed":
            history.status = "completed"
            if not history.json_ext:
                history.json_ext = {}
            history.json_ext.update(
                {
                    "task_status": "completed",
                    "completed_at": timezone.now().isoformat(),
                }
            )
            _save_history(history, user=user, update_fields=["status", "json_ext"])

        logger.info(
            "PAA ETL completed: history_id=%s district=%s questionnaire=%s households=%s",
            history_id,
            district_code,
            questionnaire_id,
            history.number_of_households,
        )
        _emit_finished(history, user=user)
        return {"success": True, "history_id": str(history.id)}

    except Exception as exc:
        logger.error("PAA ETL failed: history_id=%s error=%s", history_id, str(exc), exc_info=True)
        if not history.json_ext:
            history.json_ext = {}
        history.json_ext.update(
            {
                "task_status": "failed",
                "failed_at": timezone.now().isoformat(),
                "traceback": traceback.format_exc(),
            }
        )
        history.status = "failed"
        history.error_message = str(exc)
        _save_history(history, user=user, update_fields=["status", "error_message", "json_ext"])
        _emit_finished(history, user=user)
        return {"success": False, "history_id": str(history.id), "message": str(exc)}

    finally:
        clear_current_user()


@shared_task(bind=True, max_retries=0, name="api_etl.run_paa_etl")
def run_paa_etl_task(self, history_id: str, user_id: str, params: Dict[str, Any]):
    logger.info(
        "Starting async PAA ETL task: task_id=%s history_id=%s district=%s",
        self.request.id,
        history_id,
        params.get("district_code"),
    )
    return execute_paa_etl_history(history_id, user_id, params)


@shared_task(name="api_etl.poll_survey_dashboard")
def poll_survey_dashboard_task(questionnaire_id: Optional[str] = None, with_sample: bool = True):
    """Poll Survey Solutions HQ for the monitoring dashboard and refresh the cache."""
    from api_etl.services.survey_dashboard_service import refresh_dashboard

    try:
        return refresh_dashboard(questionnaire_id=questionnaire_id, with_sample=with_sample)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Survey dashboard poll task failed: %s", exc)
        return {"error": str(exc)}


@shared_task(name="api_etl.backfill_hh_size")
def backfill_hh_size_task(questionnaire_id: Optional[str] = None, budget: Optional[int] = None):
    """One bounded backfill pass; re-enqueues itself until the scope is covered."""
    from api_etl.services.survey_dashboard_service import backfill_hh_size

    try:
        result = backfill_hh_size(questionnaire_id, budget=budget)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Survey dashboard hh_size backfill failed: %s", exc)
        return {"error": str(exc)}
    if (result or {}).get("remaining"):
        backfill_hh_size_task.apply_async(args=[questionnaire_id, budget], countdown=30)
    return result


def poll_survey_dashboard_periodic(*args, **kwargs):
    """
    Plain (non-Celery) callable for the openIMIS APScheduler — wired via
    ``SCHEDULER_JOBS`` so the Survey Monitoring dashboard refreshes (and its daily
    snapshot gets written) on a fixed interval, without anyone having the page open.

    Fully guarded: this runs inside the app's scheduler thread, so it must never
    raise. Honours ``dashboard_enabled`` in the api_etl module config.
    """
    try:
        from django.db import close_old_connections
        close_old_connections()
    except Exception:  # noqa: BLE001
        pass
    try:
        from api_etl.apps import ApiEtlConfig
        if not getattr(ApiEtlConfig, "dashboard_enabled", True):
            return {"skipped": "dashboard_enabled is false"}
        from api_etl.services.survey_dashboard_service import refresh_dashboard
        result = refresh_dashboard()
        logger.info(
            "Survey dashboard scheduled poll: totalCount=%s, sampled=%s",
            (result or {}).get("total_count"), (result or {}).get("seen"),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Survey dashboard scheduled poll failed: %s", exc, exc_info=True)
        return {"error": str(exc)}
    finally:
        try:
            from django.db import close_old_connections
            close_old_connections()
        except Exception:  # noqa: BLE001
            pass
