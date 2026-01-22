import graphene
import graphene_django_optimizer as gql_optimizer
from django.db.models import Q
from core.schema import OrderedDjangoFilterConnectionField
from core.utils import append_validity_filter
from core.services import wait_for_mutation

from api_etl.apps import ApiEtlConfig
from api_etl.gql_queries import (
    ETLServicesGQLType,
    ETLServicesListGQLType,
    PulledHistoryGQLType,
    PulledQuestionnaireGQLType,
    QuestionnaireGQLType,
    resolve_pulled_questionnaires,
    resolve_available_questionnaires,
)
from api_etl.gql_mutations import ETLServiceMutation, PAABasedETLMutation
from api_etl.models import PulledHistory
from api_etl.utils import (
    get_class_by_name,
    get_classes_in_module,
    ETL_CLASS,
)


class Query(graphene.ObjectType):
    etl_services_by_service_name = graphene.Field(
        ETLServicesListGQLType,
        name_of_service=graphene.Argument(graphene.String, required=False),
    )

    pulled_questionnaires_history = OrderedDjangoFilterConnectionField(
        PulledHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        client_mutation_id=graphene.String(),
    )

    pulled_questionnaires = graphene.List(
        PulledQuestionnaireGQLType,
        region_code=graphene.String(),
        district_code=graphene.String(),
    )

    available_questionnaires = graphene.List(
        QuestionnaireGQLType,
        district_code=graphene.String(description="Filter by district code"),
        region_code=graphene.String(description="Filter by region code"),
        district_name=graphene.String(description="District name for matching"),
        show_all=graphene.Boolean(
            description="Show all questionnaires even with score=0"
        ),
        description="List questionnaires from HQ, optionally filtered by PAA",
    )

    def resolve_etl_services_by_service_name(parent, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")

        list_sr = []
        service_name = kwargs.get("name_of_service")
        if service_name:
            cls = get_class_by_name(ETL_CLASS, service_name)
            if cls:
                list_sr.append(ETLServicesGQLType(name_of_service=cls.__name__))
        else:
            for cls_name in get_classes_in_module(ETL_CLASS):
                list_sr.append(ETLServicesGQLType(name_of_service=cls_name))

        return ETLServicesListGQLType(etl_services=list_sr)

    def resolve_pulled_questionnaires_history(self, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")

        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        query = PulledHistory.objects.filter(*filters).order_by("-date_pulled")
        return gql_optimizer.query(query, info)

    def resolve_pulled_questionnaires(self, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")

        return resolve_pulled_questionnaires(info, **kwargs)

    def resolve_available_questionnaires(self, info, **kwargs):
        """Fetch questionnaires from HQ, optionally filtered by PAA."""
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")
        return resolve_available_questionnaires(info, **kwargs)


class Mutation(graphene.ObjectType):
    etl_service_mutation = ETLServiceMutation.Field()
    paa_based_etl = PAABasedETLMutation.Field()
