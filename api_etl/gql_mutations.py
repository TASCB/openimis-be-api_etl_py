import graphene as graphene
import uuid
from datetime import datetime

from django.utils.translation import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from api_etl.utils import (
    get_class_by_name,
    ETL_CLASS,
)
from api_etl.apps import ApiEtlConfig
from api_etl.models import PulledHistory
from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation


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
        try:
            data.pop("client_mutation_id", None)
            data.pop("client_mutation_label", None)

            paa_name = data.get("paa_name")
            district_code = data.get("district_code")
            region_code = data.get("region_code")
            questionnaire_id = data.get("questionnaire_id")
            dry_run = data.get("dry_run", False)

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
                # Find and execute the Survey Solutions ETL service
                etl_service_class = get_class_by_name(
                    ETL_CLASS, "SurveySolutionsService"
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
                result = etl_service.execute()

                if result.get("success"):
                    # Update history record with results
                    history_record.status = "completed"
                    history_record.update_counts_from_etl_result(result)
                    return None
                else:
                    history_record.status = "failed"
                    history_record.error_message = result.get(
                        "detail", "ETL execution failed"
                    )
                    history_record.save()

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
                history_record.status = "failed"
                history_record.error_message = str(exc)
                history_record.save()
                raise exc

        except Exception as exc:
            return [
                {
                    "message": "api_etl.mutation.failed_to_execute_paa_etl",
                    "detail": str(exc),
                }
            ]
