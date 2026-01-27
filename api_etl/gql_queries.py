import graphene
from django.db.models import Q
from graphene_django import DjangoObjectType
from individual.models import Individual
from .models import PulledHistory


def _strip_district_suffix(district_name, suffixes=None):
    """
    Strip common district suffixes (DC, TC, MC) from district name.
    Handles both with and without spaces before suffix.

    Args:
        district_name: District name (e.g., "KILOLODC", "Kilolo DC", "KILOLO")
        suffixes: List of suffixes to strip (default: ["DC", "TC", "MC"])

    Returns:
        Base district name without suffix (e.g., "KILOLO")

    Examples:
        "KILOLODC" -> "KILOLO"
        "Kilolo DC" -> "KILOLO"  (with space)
        "KILOLOTC" -> "KILOLO"
        "KILOLO" -> "KILOLO"
        "Morogoro MC" -> "MOROGORO"  (with space)
    """
    if not district_name:
        return ""

    if suffixes is None:
        from api_etl.apps import ApiEtlConfig

        suffixes = getattr(ApiEtlConfig, "district_name_suffixes", ["DC", "TC", "MC"])

    district_upper = district_name.upper().strip()

    # Try to strip each suffix (with or without space)
    for suffix in suffixes:
        suffix_upper = suffix.upper()

        # Try with space first: "MOROGORO MC" -> "MOROGORO"
        if district_upper.endswith(" " + suffix_upper):
            return district_upper[: -(len(suffix_upper) + 1)].strip()

        # Try without space: "MOROGOROMC" -> "MOROGORO"
        elif district_upper.endswith(suffix_upper):
            return district_upper[: -len(suffix_upper)].strip()

    return district_upper


def _extract_district_from_questionnaire(questionnaire_title, prefix=None):
    """
    Extract district name from questionnaire title.

    Args:
        questionnaire_title: Full questionnaire title (e.g., "DODOSO LA KAYA-RM4_KILOLODC")
        prefix: Expected prefix to strip (default: from config)

    Returns:
        District name extracted from title (e.g., "KILOLODC")

    Examples:
        "DODOSO LA KAYA-RM4_KILOLODC" -> "KILOLODC"
        "DODOSO LA KAYA-RM4_KILOLO" -> "KILOLO"
    """
    if not questionnaire_title:
        return ""

    if prefix is None:
        from api_etl.apps import ApiEtlConfig

        prefix = getattr(
            ApiEtlConfig, "questionnaire_title_prefix", "DODOSO LA KAYA-RM4_"
        )

    # Remove prefix if present
    if prefix and questionnaire_title.startswith(prefix):
        return questionnaire_title[len(prefix) :].strip()

    # Fallback: extract text after last underscore or dash
    if "_" in questionnaire_title:
        return questionnaire_title.split("_")[-1].strip()
    elif "-" in questionnaire_title:
        return questionnaire_title.split("-")[-1].strip()

    return questionnaire_title.strip()


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
    paaName = graphene.String()
    numberOfHouseholds = graphene.Int()
    numberOfMembers = graphene.Int()
    datePulled = graphene.DateTime()
    status = graphene.String()
    errorMessage = graphene.String()


def resolve_pulled_questionnaires(info, **kwargs):
    """
    Resolver for pulledQuestionnaires query.
    Returns list of pulled questionnaire history records.
    """
    queryset = PulledHistory.get_queryset(None, info.context.user)

    # Apply optional filters
    region_code = kwargs.get("region_code") or kwargs.get("regionCode")
    if region_code:
        queryset = queryset.filter(region_code=region_code)

    district_code = kwargs.get("district_code") or kwargs.get("districtCode")
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
                paaName=record.paa_name,
                numberOfHouseholds=record.number_of_households,
                numberOfMembers=(record.n_individuals_inserted or 0)
                + (record.n_individuals_updated or 0),
                datePulled=record.date_pulled,
                status=record.status,
                errorMessage=record.error_message,
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
                paaName=data["paa_name"],
                numberOfHouseholds=len(data["group_codes"]),
                numberOfMembers=0,
                datePulled=data["date_created"],
                status="completed",
                errorMessage=None,
            )
        )
    # Sort by date descending
    results.sort(key=lambda x: x.datePulled, reverse=True)

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
    Filter and score questionnaires by PAA criteria with SUFFIX-AWARE matching.

    Scoring:
        100: Exact match (KILOLODC == KILOLODC)
        90:  Base match with different suffix / no suffix (KILOLODC matches KILOLOTC/KILOLO)
        50:  Partial base match (substring)
        0:   No match (kept when showAll=True; removed later when showAll=False)
    """
    from api_etl.services.survey_solution_service import _normalize_text
    from api_etl.apps import ApiEtlConfig as C
    import logging
    from datetime import datetime

    LOG = logging.getLogger(__name__)

    if not questionnaires:
        return []

    # Config
    suffixes = getattr(C, "district_name_suffixes", ["DC", "TC", "MC"])
    prefix = getattr(C, "questionnaire_title_prefix", "DODOSO LA KAYA-RM4_")

    LOG.error("=== MATCHING ALGORITHM DEBUG ===")
    LOG.error(f"Config - suffixes: {suffixes}")
    LOG.error(f"Config - prefix: {prefix}")
    LOG.error(f"Input - district_name: {district_name}")

    def _safe_ts(v):
        """
        Convert LastEntryDate to a comparable timestamp (int).
        Handles ISO strings like '2026-01-19T08:55:10Z' safely.
        """
        if not v:
            return 0
        if isinstance(v, datetime):
            return int(v.timestamp())
        if isinstance(v, str):
            try:
                vv = v.replace("Z", "+00:00")
                return int(datetime.fromisoformat(vv).timestamp())
            except Exception:
                return 0
        return 0

    # Base district name (strip suffix)
    district_base = (
        _strip_district_suffix(district_name, suffixes) if district_name else ""
    )
    district_normalized = _normalize_text(district_name) if district_name else ""
    district_base_normalized = _normalize_text(district_base) if district_base else ""

    LOG.error(f"Base district name (after stripping suffix): {district_base}")
    LOG.error(f"Normalized district name: {district_normalized}")

    scored_questionnaires = []

    for q in questionnaires:
        title = q.get("Title", "")
        if not title:
            continue

        # Extract district name from questionnaire title
        q_district = _extract_district_from_questionnaire(title, prefix)

        # Base district extracted from questionnaire (strip suffix)
        q_district_base = _strip_district_suffix(q_district, suffixes)

        # Normalize
        q_district_normalized = _normalize_text(q_district)
        q_district_base_normalized = _normalize_text(q_district_base)

        score = 0
        strategy = "no-match"

        if not district_name:
            score = 50
            strategy = "no-filter"
        elif q_district_normalized == district_normalized:
            score = 100
            strategy = "exact-match"
        elif q_district_base_normalized == district_base_normalized:
            score = 90
            strategy = "base-match"
        elif (
            district_base_normalized
            and district_base_normalized in q_district_base_normalized
        ):
            score = 50
            strategy = "partial-match"

        # Debug log (first few or any match)
        if len(scored_questionnaires) < 5 or score > 0:
            LOG.error(
                f"  Q: '{title}' | Extracted: '{q_district}' | Base: '{q_district_base}' "
                f"| Score: {score} | Strategy: {strategy}"
            )

        # ALWAYS append (so showAll=True can show score=0 items)
        q_with_score = dict(q)
        q_with_score["matching_score"] = score
        q_with_score["matching_strategy"] = strategy
        scored_questionnaires.append(q_with_score)

    # Sort ONCE after the loop
    scored_questionnaires.sort(
        key=lambda x: (
            x.get("matching_score", 0),
            int(x.get("Version", 0) or 0),
            _safe_ts(x.get("LastEntryDate")),
        ),
        reverse=True,
    )

    return scored_questionnaires


def resolve_available_questionnaires(info, **kwargs):
    """
    Fetch available questionnaires from Survey Solutions HQ.

    Optionally filters by PAA (district) if districtCode/regionCode provided.

    Args:
        district_code (optional): Filter by district code
        region_code (optional): Filter by region code
        district_name (optional): District display name for name matching
        show_all (optional): If True, show all questionnaires even with score=0

    Returns:
        List[QuestionnaireGQLType]: Questionnaires from HQ, scored and sorted
    """
    from api_etl.sources.survey_solutions_export_source import (
        SurveySolutionsExportSource,
    )
    from api_etl.apps import ApiEtlConfig as C
    import logging

    LOG = logging.getLogger(__name__)

    # Extract filter params - try multiple parameter name formats
    district_code = kwargs.get("district_code") or kwargs.get("districtCode")
    region_code = kwargs.get("region_code") or kwargs.get("regionCode")
    district_name = kwargs.get("district_name") or kwargs.get("districtName")
    show_all = kwargs.get("show_all", False) or kwargs.get("showAll", False)

    # DEBUG: Log ALL received parameters
    LOG.error(f"=== QUESTIONNAIRE FILTER DEBUG ===")
    LOG.error(f"All kwargs received: {kwargs}")
    LOG.error(f"Extracted - district_code: {district_code}")
    LOG.error(f"Extracted - region_code: {region_code}")
    LOG.error(f"Extracted - district_name: {district_name}")
    LOG.error(f"Extracted - show_all: {show_all}")

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
            LOG.error(f"=== APPLYING FILTER ===")
            LOG.error(
                f"Filter criteria: district_code={district_code}, region_code={region_code}, district_name={district_name}"
            )

            questionnaires = _filter_questionnaires_by_paa(
                questionnaires,
                district_name=district_name,
                district_code=district_code,
                region_code=region_code,
            )

            LOG.error(f"After filtering: {len(questionnaires)} questionnaires")

            # Filter out non-matches unless showAll is True
            if not show_all:
                original_count = len(questionnaires)
                questionnaires = [
                    q for q in questionnaires if q.get("matching_score", 0) > 0
                ]
                LOG.error(
                    f"After removing score=0: {len(questionnaires)} questionnaires (removed {original_count - len(questionnaires)})"
                )

            LOG.info(
                "After PAA filtering: %d questionnaires (showAll=%s)",
                len(questionnaires),
                show_all,
            )
        else:
            LOG.error(f"=== NO FILTER APPLIED (no criteria provided) ===")
            LOG.error(f"Returning all {len(questionnaires)} questionnaires")

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
