import graphene as graphene
import uuid
import logging

from django.utils.translation import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

from api_etl.utils import (
    get_class_by_name,
    ETL_CLASS,
)
from api_etl.apps import ApiEtlConfig
from api_etl.paa_aliases import get_paa_scope
from api_etl.models import PulledHistory
from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation
from core.utils import set_current_user, clear_current_user


class ETLServiceMutation(BaseMutation):
    """
    Mutation to execute the ETLService
    """

    _mutation_class = "ETLServiceMutation"
    _mutation_module = "api_etl"

    class Input(OpenIMISMutation.Input):
        name_of_service = graphene.String(required=True)

    @classmethod
    def _validate_mutation(cls, user, **data):
        # Use the mutation permission set for mutations
        if (
            type(user) is AnonymousUser
            or not user.id
            or not user.has_perms(ApiEtlConfig.gql_mutation_execute_api_etl_rule_perms)
        ):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        try:
            data.pop("client_mutation_id", None)
            data.pop("client_mutation_label", None)

            name_of_service = data.pop("name_of_service", None)
            if not name_of_service:
                return [
                    {
                        "message": "api_etl.mutation.failed_to_execute_etl_service",
                        "detail": _("There is no ETL service with provided name"),
                    }
                ]

            etl_service_class = get_class_by_name(ETL_CLASS, name_of_service)
            # Instantiate and execute the ETL service (bridge method on the service)
            etl_service = etl_service_class(user)
            result = etl_service.execute()

            if result.get("success"):
                return None
            return [
                {
                    "message": result.get(
                        "message", "api_etl.mutation.failed_to_execute_etl_service"
                    ),
                    "detail": result.get("detail", ""),
                }
            ]
        except Exception as exc:
            return [
                {
                    "message": "api_etl.mutation.failed_to_execute_etl_service",
                    "detail": str(exc),
                }
            ]


class PAABasedETLMutation(BaseMutation):
    """
    Mutation to execute PAA-based ETL and track history
    """

    _mutation_class = "PAABasedETLMutation"
    _mutation_module = "api_etl"

    class Input(OpenIMISMutation.Input):
        paa_name = graphene.String(required=True)
        district_code = graphene.String(required=False)
        region_code = graphene.String(required=False)
        questionnaire_id = graphene.String(required=False)
        dry_run = graphene.Boolean(required=False, default_value=False)

    @classmethod
    def _validate_mutation(cls, user, **data):
        if (
            type(user) is AnonymousUser
            or not user.id
            or not user.has_perms(ApiEtlConfig.gql_mutation_execute_api_etl_rule_perms)
        ):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        set_current_user(user)
        try:
            data.pop("client_mutation_id", None)
            data.pop("client_mutation_label", None)

            paa_name = data.get("paa_name") or data.get("paaName")
            district_code = data.get("district_code") or data.get("districtCode")
            region_code = data.get("region_code") or data.get("regionCode")
            paa_scope = get_paa_scope(name=paa_name, code=district_code, config=ApiEtlConfig)
            matching_paa_name = paa_scope or paa_name
            questionnaire_id = data.get("questionnaire_id") or data.get(
                "questionnaireId"
            )
            dry_run = (
                data.get("dry_run", False)
                if data.get("dry_run", None) is not None
                else data.get("dryRun", False)
            )

            from api_etl.stale_imports import fail_stale_running_imports

            fail_stale_running_imports(district_code=district_code, user=user)

            if (
                getattr(ApiEtlConfig, "paa_etl_prevent_duplicate_active", True)
                and district_code
                and PulledHistory.objects.filter(
                    district_code=district_code,
                    status="running",
                ).exists()
            ):
                return [
                    {
                        "message": "api_etl.mutation.paa_etl_already_running",
                        "detail": _(
                            "An import is already running for this district. Please wait for it to complete or choose another district."
                        ),
                    }
                ]

            # Generate batch ID for this ETL run
            batch_id = f"ss_batch_{uuid.uuid4().hex[:12]}"

            # Create history record to track the operation
            history_record = PulledHistory.create_from_paa_etl_run(
                paa_name=paa_name,
                district_code=district_code,
                region_code=region_code,
                questionnaire_match={
                    "questionnaire_id": questionnaire_id or "pending-auto-detection",
                    "questionnaire_title": None,
                    "questionnaire_version": None,
                    "matching_strategy": (
                        "manual-override" if questionnaire_id else None
                    ),
                },
                user=user,
                status="running",
                json_ext={
                    "execution_mode": (
                        "async"
                        if getattr(ApiEtlConfig, "paa_etl_async_enabled", True)
                        else "sync"
                    ),
                    "task_status": "queued",
                    "batch_id": batch_id,
                },
            )

            params = {
                "paa_name": paa_name,
                "district_code": district_code,
                "region_code": region_code,
                "matching_paa_name": matching_paa_name,
                "questionnaire_id": questionnaire_id,
                "dry_run": dry_run,
                "batch_id": batch_id,
            }

            from api_etl.tasks import execute_paa_etl_history, run_paa_etl_task

            if getattr(ApiEtlConfig, "paa_etl_async_enabled", True):
                try:
                    queue_name = getattr(ApiEtlConfig, "paa_etl_task_queue", "") or None
                    if queue_name:
                        async_result = run_paa_etl_task.apply_async(
                            args=[str(history_record.id), str(user.id), params],
                            queue=queue_name,
                        )
                    else:
                        async_result = run_paa_etl_task.delay(
                            str(history_record.id),
                            str(user.id),
                            params,
                        )

                    history_record.json_ext = history_record.json_ext or {}
                    history_record.json_ext.update(
                        {
                            "task_id": async_result.id,
                            "task_status": "queued",
                        }
                    )
                    history_record.save(username=user.username, update_fields=["json_ext"])
                    logger.info(
                        "Queued async PAA ETL: history_id=%s task_id=%s district=%s",
                        history_record.id,
                        async_result.id,
                        district_code,
                    )
                    return None
                except Exception as exc:
                    logger.exception("Failed to queue async PAA ETL")
                    if not getattr(
                        ApiEtlConfig,
                        "paa_etl_fallback_to_sync_on_queue_error",
                        True,
                    ):
                        history_record.status = "failed"
                        history_record.error_message = str(exc)
                        history_record.json_ext = history_record.json_ext or {}
                        history_record.json_ext.update({"task_status": "queue_failed"})
                        history_record.save(
                            username=user.username,
                            update_fields=["status", "error_message", "json_ext"],
                        )
                        return [
                            {
                                "message": "api_etl.mutation.failed_to_queue_paa_etl",
                                "detail": str(exc),
                            }
                        ]

                    logger.warning(
                        "Falling back to synchronous PAA ETL execution for history_id=%s",
                        history_record.id,
                    )

            execute_paa_etl_history(str(history_record.id), str(user.id), params)
            return None

        except Exception as exc:
            return [
                {
                    "message": "api_etl.mutation.failed_to_execute_paa_etl",
                    "detail": str(exc),
                }
            ]
        finally:
            # CLEANUP: Clear current user after mutation completes
            clear_current_user()


class RefreshSurveyDashboardMutation(graphene.Mutation):
    """
    Force an immediate poll of the Survey Solutions HQ ``/api/v1/interviews``
    endpoint and refresh the monitoring-dashboard cache. Returns the poll
    summary so the frontend can show "synced N interviews".
    """

    class Arguments:
        questionnaire_id = graphene.String(required=False)

    success = graphene.Boolean()
    seen = graphene.Int()
    created = graphene.Int()
    changed = graphene.Int()
    total_count = graphene.Int()
    polled_at = graphene.String()
    message = graphene.String()

    @classmethod
    def mutate(cls, root, info, questionnaire_id=None):
        user = info.context.user
        if (
            type(user) is AnonymousUser
            or not getattr(user, "id", None)
            or not user.has_perms(ApiEtlConfig.gql_mutation_execute_api_etl_rule_perms)
        ):
            raise PermissionError("Unauthorized")
        try:
            from api_etl.services.survey_dashboard_service import refresh_dashboard

            result = refresh_dashboard(questionnaire_id=questionnaire_id)
            return cls(
                success=True,
                seen=result.get("seen", 0),
                created=result.get("created", 0),
                changed=result.get("changed", 0),
                total_count=result.get("total_count"),
                polled_at=result.get("polled_at"),
                message="ok",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to refresh survey dashboard")
            return cls(
                success=False, seen=0, created=0, changed=0, total_count=None, polled_at=None, message=str(exc),
            )
