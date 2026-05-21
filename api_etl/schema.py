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
    QuestionnaireGQLType,
    PulledQuestionnaireConnection,
    resolve_pulled_questionnaires,
    resolve_available_questionnaires,
    SurveyDashboardMetricsGQLType,
    SurveyInterviewGQLType,
    resolve_survey_dashboard,
    resolve_survey_interviews,
)
from api_etl.gql_mutations import (
    ETLServiceMutation,
    PAABasedETLMutation,
    RefreshSurveyDashboardMutation,
)
from api_etl.models import PulledHistory
from api_etl.utils import get_class_by_name, get_classes_in_module, ETL_CLASS


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

    # IMPORTANT: ConnectionField must receive a Connection subclass
    pulled_questionnaires = graphene.relay.ConnectionField(
        PulledQuestionnaireConnection,
        region_code=graphene.String(),
        district_code=graphene.String(),
    )

    available_questionnaires = graphene.List(
        QuestionnaireGQLType,
        district_code=graphene.String(description="Filter by district code"),
        region_code=graphene.String(description="Filter by region code"),
        district_name=graphene.String(description="District name for matching"),
        show_all=graphene.Boolean(description="Show all questionnaires even with score=0"),
        description="List questionnaires from HQ, optionally filtered by PAA",
    )

    # --- Real-time Survey Monitoring Dashboard ---
    survey_dashboard = graphene.Field(
        SurveyDashboardMetricsGQLType,
        questionnaire_id=graphene.String(description="Optional 'GUID$version' filter"),
        description="Aggregated Survey Solutions monitoring KPIs + chart series",
    )
    survey_interviews = graphene.List(
        SurveyInterviewGQLType,
        questionnaire_id=graphene.String(),
        status=graphene.String(),
        responsible_name=graphene.String(),
        supervisor_name=graphene.String(),
        search=graphene.String(),
        from_date=graphene.String(description="Only interviews with activity on/after this date (YYYY-MM-DD)"),
        first=graphene.Int(),
        description="Recent interview activity feed (from the dashboard cache)",
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
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = PulledHistory.objects.filter(*filters).order_by("-date_pulled")
        return gql_optimizer.query(query, info)

    def resolve_pulled_questionnaires(self, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")

        # IMPORTANT: resolver signature for ConnectionField is (root, info, **kwargs)
        return resolve_pulled_questionnaires(self, info, **kwargs)

    def resolve_available_questionnaires(self, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")
        return resolve_available_questionnaires(info, **kwargs)

    def resolve_survey_dashboard(self, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")
        return resolve_survey_dashboard(info, **kwargs)

    def resolve_survey_interviews(self, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")
        return resolve_survey_interviews(info, **kwargs)


class Mutation(graphene.ObjectType):
    etl_service_mutation = ETLServiceMutation.Field()
    paa_based_etl = PAABasedETLMutation.Field()
    refresh_survey_dashboard = RefreshSurveyDashboardMutation.Field()
