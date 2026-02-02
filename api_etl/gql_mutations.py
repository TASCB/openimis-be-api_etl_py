import graphene as graphene
import uuid
from datetime import datetime
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
            questionnaire_id = data.get("questionnaire_id") or data.get(
                "questionnaireId"
            )
            dry_run = (
                data.get("dry_run", False)
                if data.get("dry_run", None) is not None
                else data.get("dryRun", False)
            )

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
            )

            try:
                # VALIDATION: Ensure questionnaire matches selected PAA/district
                if questionnaire_id and paa_name:
                    from api_etl.gql_queries import (
                        _strip_district_suffix,
                        _extract_district_from_questionnaire,
                    )
                    from api_etl.apps import ApiEtlConfig
                    from api_etl.sources.survey_solutions_export_source import (
                        SurveySolutionsExportSource,
                    )

                    # Get questionnaire title from HQ to validate
                    try:
                        source = SurveySolutionsExportSource()
                        source.base_url = getattr(ApiEtlConfig, "export_base_url", "")
                        source.workspace = getattr(ApiEtlConfig, "export_workspace", "")
                        source.meta_api_prefix = getattr(
                            ApiEtlConfig, "meta_api_prefix", "/api/v1"
                        )

                        questionnaires = source.list_questionnaires()
                        selected_q = next(
                            (
                                q
                                for q in questionnaires
                                if q.get("Identity") == questionnaire_id
                                or q.get("Id") == questionnaire_id
                            ),
                            None,
                        )

                        if selected_q:
                            q_title = selected_q.get("Title", "")

                            # Extract district from questionnaire title
                            suffixes = getattr(
                                ApiEtlConfig,
                                "district_name_suffixes",
                                ["DC", "TC", "MC"],
                            )
                            prefix = getattr(
                                ApiEtlConfig,
                                "questionnaire_title_prefix",
                                "DODOSO LA KAYA-RM4_",
                            )

                            q_district = _extract_district_from_questionnaire(
                                q_title, prefix
                            )
                            q_district_base = _strip_district_suffix(
                                q_district, suffixes
                            )
                            paa_base = _strip_district_suffix(paa_name, suffixes)

                            # Compare base names (case-insensitive)
                            if q_district_base.upper() != paa_base.upper():
                                history_record.status = "failed"
                                history_record.error_message = (
                                    f"Questionnaire validation failed: "
                                    f"Selected questionnaire '{q_title}' does not belong to district '{paa_name}'. "
                                    f"Expected district: '{paa_name}', Found: '{q_district}'"
                                )
                                history_record.save(
                                    username=user.username,
                                    update_fields=["status", "error_message"],
                                )
                                return [
                                    {
                                        "message": "api_etl.mutation.questionnaire_mismatch",
                                        "detail": history_record.error_message,
                                    }
                                ]
                    except Exception as validation_error:
                        # Log but don't block import if validation fails
                        logger.error(
                            f"Questionnaire validation error (non-blocking): {str(validation_error)}"
                        )

                # Find and execute the Survey Solutions ETL service
                etl_service_class = get_class_by_name(
                    ETL_CLASS, "SurveySolutionService"
                )
                if not etl_service_class:
                    from api_etl.utils import get_classes_in_module

                    available_services = get_classes_in_module(ETL_CLASS)
                    if available_services:
                        etl_service_class = get_class_by_name(
                            ETL_CLASS, available_services[0]
                        )
                    else:
                        raise Exception("No ETL service available")

                # Fallback: if questionnaire_id not provided, auto-pick best match
                if not questionnaire_id and paa_name:
                    try:
                        from api_etl.gql_queries import resolve_available_questionnaires

                        matches = resolve_available_questionnaires(
                            info=None,  # not used inside resolve_available_questionnaires
                            districtName=paa_name,
                            districtCode=district_code,
                            regionCode=region_code,
                            showAll=False,
                        )
                        if matches:
                            questionnaire_id = matches[
                                0
                            ].identity  # top scored & sorted already
                            logger.warning(
                                "Auto-selected questionnaire_id=%s for paa_name=%s",
                                questionnaire_id,
                                paa_name,
                            )
                    except Exception as e:
                        logger.error(
                            "Auto-selection of questionnaire failed: %s", str(e)
                        )

                # Configure the service with PAA parameters
                service_config = {
                    "paa_name": paa_name,
                    "district_code": district_code,
                    "region_code": region_code,
                    "questionnaire_id": questionnaire_id,
                    "dry_run": dry_run,
                    "batch_id": batch_id,
                }

                etl_service = etl_service_class(user, config=service_config)

                # If questionnaire_id not provided, auto-detect using the service matcher
                if not questionnaire_id:
                    from api_etl.services.survey_solution_service import (
                        find_matching_questionnaire,
                    )

                    qid, match_info = find_matching_questionnaire(
                        district_name=paa_name,
                        district_code=district_code or "",
                        region_code=region_code or "",
                        source=etl_service.source,
                        config=etl_service.config,
                        manual_questionnaire_id=None,
                    )
                    questionnaire_id = qid
                    logger.warning(
                        "Auto-detected questionnaire_id=%s match=%s",
                        questionnaire_id,
                        match_info,
                    )

                    if not questionnaire_id:
                        history_record.status = "failed"
                        history_record.error_message = (
                            match_info.get("error") or "No matching questionnaire found"
                        )
                        history_record.save(
                            username=user.username,
                            update_fields=["status", "error_message"],
                        )
                        return [
                            {
                                "message": "api_etl.mutation.failed_to_execute_paa_etl",
                                "detail": history_record.error_message,
                            }
                        ]

                # Run explicitly with questionnaire_id (do not rely on execute())
                out = etl_service.run(
                    questionnaire_id=questionnaire_id, dry_run=dry_run
                )

                result = {
                    "success": True,
                    "message": "ok",
                    "detail": out,
                }

                # DEBUG: Log result details
                logger.error(f"=== ETL EXECUTE COMPLETED ===")
                logger.error(f"Result type: {type(result)}")
                logger.error(
                    f"Result keys: {result.keys() if isinstance(result, dict) else 'NOT A DICT'}"
                )
                logger.error(
                    f"Result success: {result.get('success') if isinstance(result, dict) else 'N/A'}"
                )
                logger.error(
                    f"Result detail type: {type(result.get('detail')) if isinstance(result, dict) else 'N/A'}"
                )

                if result.get("success"):
                    # Update history record with results
                    logger.error(f"=== STATUS UPDATE STARTING ===")
                    logger.error(f"History record uuid: {history_record.uuid}")

                    history_record.status = "completed"
                    logger.error(f"Status set to: {history_record.status}")
                    logger.error(f"About to call update_counts_from_etl_result")

                    history_record.update_counts_from_etl_result(
                        result.get("detail", {}),
                        user=user,  # Pass user for audit logging
                    )
                    logger.error(f"update_counts_from_etl_result completed")

                    history_record.refresh_from_db()
                    logger.error(
                        f"After refresh_from_db, status={history_record.status}"
                    )

                    if history_record.status != "completed":
                        history_record.status = "completed"
                        history_record.save(
                            username=user.username, update_fields=["status"]
                        )
                        logger.error(f"Did defensive save with update_fields")

                    logger.error(f"=== STATUS UPDATE COMPLETED ===")
                    logger.error(f"Final status: {history_record.status}")
                    logger.error(
                        f"Final household count: {history_record.number_of_households}"
                    )
                    return None
                else:
                    logger.error(f"=== ETL FAILED ===")
                    logger.error(f"Result was NOT successful")
                    logger.error(f"Result detail: {result.get('detail', 'NO DETAIL')}")

                    history_record.status = "failed"
                    history_record.error_message = result.get(
                        "detail", "ETL execution failed"
                    )
                    history_record.save(
                        username=user.username,
                        update_fields=["status", "error_message"],
                    )

                    return [
                        {
                            "message": result.get(
                                "message", "api_etl.mutation.failed_to_execute_paa_etl"
                            ),
                            "detail": result.get("detail", ""),
                        }
                    ]

            except Exception as exc:
                # Update history record with error
                logger.error(f"=== EXCEPTION IN ETL EXECUTION ===")
                logger.error(f"Exception type: {type(exc)}")
                logger.error(f"Exception message: {str(exc)}")
                import traceback

                logger.error(f"Traceback: {traceback.format_exc()}")

                history_record.status = "failed"
                history_record.error_message = str(exc)
                history_record.save(
                    username=user.username, update_fields=["status", "error_message"]
                )
                # Don't re-raise to avoid transaction rollback
                return [
                    {
                        "message": "api_etl.mutation.failed_to_execute_paa_etl",
                        "detail": str(exc),
                    }
                ]

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
