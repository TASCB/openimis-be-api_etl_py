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
            "paa_name": ["exact", "icontains"],
            "district_code": ["exact"],
            "region_code": ["exact"],
            "status": ["exact"],
            "date_pulled": ["exact", "gte", "lte"],
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
    region_code = kwargs.get("regionCode")
    if region_code:
        queryset = queryset.filter(region_code=region_code)

    district_code = kwargs.get("districtCode")
    if district_code:
        queryset = queryset.filter(district_code=district_code)

    # Order by date_pulled descending (most recent first)
    queryset = queryset.order_by("-date_pulled")

    # If no PulledHistory records exist, fallback to Individual grouping
    if not queryset.exists():
        return _get_fallback_pulled_questionnaires(region_code, district_code)

    # Convert to expected format
    results = []
    for record in queryset:
        results.append(
            PulledQuestionnaireGQLType(
                paa_name=record.paa_name,
                number_of_households=record.number_of_households,
                date_pulled=record.date_pulled,
            )
        )

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
            location_filters &= Q(location__parent__code=region_code) | Q(
                location__code=region_code
            )
        if district_code:
            location_filters &= Q(location__code=district_code)
        queryset = queryset.filter(location_filters)

    # Group by ss_batch and count households
    batch_groups = {}
    for individual in queryset.filter(json_ext__ss_batch__isnull=False):
        ss_batch = individual.json_ext.get("ss_batch")
        if ss_batch:
            if ss_batch not in batch_groups:
                batch_groups[ss_batch] = {
                    "group_codes": set(),
                    "paa_name": (
                        individual.location.name if individual.location else "Unknown"
                    ),
                    "date_created": individual.date_created,
                }

            # Count unique group codes within this batch
            group_code = individual.json_ext.get("group_code")
            if group_code:
                batch_groups[ss_batch]["group_codes"].add(group_code)

    # Convert to expected format
    results = []
    for batch_id, data in batch_groups.items():
        results.append(
            PulledQuestionnaireGQLType(
                paa_name=data["paa_name"],
                number_of_households=len(data["group_codes"]),
                date_pulled=data["date_created"],
            )
        )

    # Sort by date descending
    results.sort(key=lambda x: x.date_pulled, reverse=True)

    return results


# ============================================================
# Survey Solutions HQ Questionnaire Listing (PAA-aware)
# ============================================================


class QuestionnaireGQLType(graphene.ObjectType):
    """
    Represents a Survey Solutions questionnaire from HQ.
    Enhanced with matching score for PAA filtering.
    """

    identity = graphene.String(description="Questionnaire identity (GUID$version)")
    id = graphene.String(description="Questionnaire GUID")
    title = graphene.String(description="Questionnaire title")
    version = graphene.Int(description="Version number")
    variable = graphene.String(description="Variable name")
    last_entry_date = graphene.String(description="Last entry date")

    # NEW: Matching metadata for PAA filtering
    matching_score = graphene.Int(
        description="Match score for PAA (0-3, higher is better)"
    )
    matching_strategy = graphene.String(
        description="How it matched (code+name, code-aware, name-only)"
    )


def _filter_questionnaires_by_paa(
    questionnaires, district_name=None, district_code=None, region_code=None
):
    """
    Filter and score questionnaires by PAA criteria.

    Uses the same scoring logic as find_matching_questionnaire() but returns
    ALL matches with scores, not just the best one.

    Args:
        questionnaires: List of questionnaire dicts from HQ
        district_name: District/PAA display name (e.g., "Iringa DC")
        district_code: District code (e.g., "0504")
        region_code: Region code (e.g., "05")

    Returns:
        List of questionnaires with matching_score and matching_strategy added
    """
    from api_etl.services.survey_solution_service import (
        _normalize_text,
        _remove_stop_words,
        _extract_code_tokens,
        _cfg_get,
    )
    from api_etl.apps import ApiEtlConfig as C

    if not questionnaires:
        return []

    # Get config for matching
    config = C.__dict__
    stop_words = _cfg_get(
        config, "questionnaire_matching_stop_words", default=["district", "council"]
    )
    enable_code_matching = _cfg_get(
        config, "questionnaire_matching_enable_code_tokens", default=True
    )

    # Score each questionnaire
    scored_questionnaires = []

    for q in questionnaires:
        title = q.get("Title", "")
        if not title:
            continue

        # Normalize
        normalized_title = _normalize_text(title)
        normalized_district = _normalize_text(district_name) if district_name else ""

        # Remove stop words
        if stop_words:
            stop_words_normalized = [_normalize_text(word) for word in stop_words]
            normalized_title = _remove_stop_words(
                normalized_title, stop_words_normalized
            )
            if normalized_district:
                normalized_district = _remove_stop_words(
                    normalized_district, stop_words_normalized
                )

        # Extract codes from title
        title_codes = _extract_code_tokens(title)

        # Check code matches
        has_district_code = False
        has_region_code = False

        if enable_code_matching and district_code:
            district_prefix = (
                district_code[:4] if len(district_code) >= 4 else district_code
            )
            has_district_code = any(
                code.startswith(district_prefix) for code in title_codes
            )

        if enable_code_matching and region_code:
            region_prefix = region_code[:2] if len(region_code) >= 2 else region_code
            has_region_code = any(
                code.startswith(region_prefix) for code in title_codes
            )

        # Check name match
        has_name_match = False
        if normalized_district:
            district_words = normalized_district.split()
            if district_words:
                title_words = normalized_title.split()
                # At least one word must match
                has_name_match = any(
                    any(dword in tword for tword in title_words)
                    for dword in district_words
                )

        # Calculate score and strategy
        score = 0
        strategy = "no-match"

        if has_district_code and has_name_match:
            score = 3
            strategy = "code+name"
        elif has_district_code or has_region_code:
            score = 2
            strategy = "code-aware"
        elif has_name_match:
            score = 1
            strategy = "name-only"

        # Add to results even if score is 0 (for "show all" option)
        q_with_score = dict(q)  # Copy
        q_with_score["matching_score"] = score
        q_with_score["matching_strategy"] = strategy
        scored_questionnaires.append(q_with_score)

    # Sort by score (highest first), then version (latest first)
    scored_questionnaires.sort(
        key=lambda x: (
            -x["matching_score"],
            -int(x.get("Version", 0) or 0),
            -(x.get("LastEntryDate") or ""),
        )
    )

    return scored_questionnaires


def resolve_available_questionnaires(info, **kwargs):
    """
    Fetch available questionnaires from Survey Solutions HQ.

    Optionally filters by PAA (district) if districtCode/regionCode provided.

    Args:
        districtCode (optional): Filter by district code
        regionCode (optional): Filter by region code
        districtName (optional): District display name for name matching
        showAll (optional): If True, show all questionnaires even with score=0

    Returns:
        List[QuestionnaireGQLType]: Questionnaires from HQ, scored and sorted
    """
    from api_etl.sources.survey_solutions_export_source import (
        SurveySolutionsExportSource,
    )
    from api_etl.apps import ApiEtlConfig as C
    import logging

    LOG = logging.getLogger(__name__)

    # Extract filter params
    district_code = kwargs.get("districtCode")
    region_code = kwargs.get("regionCode")
    district_name = kwargs.get("districtName")
    show_all = kwargs.get("showAll", False)

    # Create and configure source
    source = SurveySolutionsExportSource()
    source.base_url = getattr(C, "export_base_url", "")
    source.workspace = getattr(C, "export_workspace", "")
    source.meta_api_prefix = getattr(C, "meta_api_prefix", "/api/v1")

    LOG.info(
        "Fetching questionnaires from HQ (PAA filter: district_code=%s, region_code=%s, district_name=%s)",
        district_code,
        region_code,
        district_name,
    )

    try:
        # Fetch all questionnaires from HQ
        questionnaires = source.list_questionnaires()

        if not questionnaires:
            LOG.warning("No questionnaires returned from HQ")
            return []

        LOG.info("Fetched %d questionnaires from HQ", len(questionnaires))

        # Filter and score by PAA if criteria provided
        if district_code or region_code or district_name:
            questionnaires = _filter_questionnaires_by_paa(
                questionnaires,
                district_name=district_name,
                district_code=district_code,
                region_code=region_code,
            )

            # Filter out non-matches unless showAll is True
            if not show_all:
                questionnaires = [
                    q for q in questionnaires if q.get("matching_score", 0) > 0
                ]

            LOG.info(
                "After PAA filtering: %d questionnaires (showAll=%s)",
                len(questionnaires),
                show_all,
            )

    except Exception as e:
        LOG.exception("Failed to fetch questionnaires from HQ")
        raise Exception(f"Failed to fetch questionnaires from HQ: {str(e)}")

    # Convert to GraphQL types
    results = []
    for q in questionnaires:
        results.append(
            QuestionnaireGQLType(
                identity=q.get("Identity"),
                id=q.get("Id"),
                title=q.get("Title"),
                version=q.get("Version"),
                variable=q.get("Variable"),
                last_entry_date=q.get("LastEntryDate"),
                matching_score=q.get("matching_score", 0),
                matching_strategy=q.get("matching_strategy", "none"),
            )
        )

    return results
