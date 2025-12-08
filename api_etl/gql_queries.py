import graphene
from django.db.models import Q
from graphene_django import DjangoObjectType
from individual.models import Individual
from .models import PulledHistory


class ETLServicesGQLType(graphene.ObjectType):
    name_of_service = graphene.String()


class ETLServicesListGQLType(graphene.ObjectType):
    etl_services = graphene.List(ETLServicesGQLType)


class PulledHistoryGQLType(DjangoObjectType):
    class Meta:
        model = PulledHistory
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            'paa_name': ['exact', 'icontains'],
            'district_code': ['exact'],
            'region_code': ['exact'],
            'status': ['exact'],
            'date_pulled': ['exact', 'gte', 'lte']
        }


class PulledQuestionnaireGQLType(graphene.ObjectType):
    paa_name = graphene.String()
    number_of_households = graphene.Int()
    date_pulled = graphene.DateTime()


def resolve_pulled_questionnaires(info, **kwargs):
    """
    Resolver for pulledQuestionnaires query.
    Returns list of pulled questionnaire history records.
    """
    queryset = PulledHistory.get_queryset(None, info.context.user)

    # Apply optional filters
    region_code = kwargs.get('regionCode')
    if region_code:
        queryset = queryset.filter(region_code=region_code)

    district_code = kwargs.get('districtCode')
    if district_code:
        queryset = queryset.filter(district_code=district_code)

    # Order by date_pulled descending (most recent first)
    queryset = queryset.order_by('-date_pulled')

    # If no PulledHistory records exist, fallback to Individual grouping
    if not queryset.exists():
        return _get_fallback_pulled_questionnaires(region_code, district_code)

    # Convert to expected format
    results = []
    for record in queryset:
        results.append(PulledQuestionnaireGQLType(
            paa_name=record.paa_name,
            number_of_households=record.number_of_households,
            date_pulled=record.date_pulled
        ))

    return results


def _get_fallback_pulled_questionnaires(region_code=None, district_code=None):
    """
    Fallback method when no PulledHistory records exist.
    Groups Individual records by ss_batch in json_ext.
    """
    queryset = Individual.objects.filter(is_deleted=False)

    # Apply location filters if provided
    if region_code or district_code:
        location_filters = Q()
        if region_code:
            location_filters &= Q(location__parent__code=region_code) | Q(location__code=region_code)
        if district_code:
            location_filters &= Q(location__code=district_code)
        queryset = queryset.filter(location_filters)

    # Group by ss_batch and count households
    batch_groups = {}
    for individual in queryset.filter(json_ext__ss_batch__isnull=False):
        ss_batch = individual.json_ext.get('ss_batch')
        if ss_batch:
            if ss_batch not in batch_groups:
                batch_groups[ss_batch] = {
                    'group_codes': set(),
                    'paa_name': individual.location.name if individual.location else 'Unknown',
                    'date_created': individual.date_created
                }

            # Count unique group codes within this batch
            group_code = individual.json_ext.get('group_code')
            if group_code:
                batch_groups[ss_batch]['group_codes'].add(group_code)

    # Convert to expected format
    results = []
    for batch_id, data in batch_groups.items():
        results.append(PulledQuestionnaireGQLType(
            paa_name=data['paa_name'],
            number_of_households=len(data['group_codes']),
            date_pulled=data['date_created']
        ))

    # Sort by date descending
    results.sort(key=lambda x: x.date_pulled, reverse=True)

    return results
