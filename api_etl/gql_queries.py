import re
from datetime import datetime

import graphene
from django.db.models import Q
from graphene_django import DjangoObjectType
from individual.models import Individual

from api_etl.paa_aliases import (
    get_paa_alias_candidates,
    get_paa_aliases,
    get_paa_scope,
    scopes_for_codes,
)
from .models import PulledHistory


# -----------------------------------------------------------------------------
# Helpers: District/PAA parsing + normalization
# -----------------------------------------------------------------------------
def _strip_district_suffix(district_name, suffixes=None):
    if not district_name:
        return ""

    if suffixes is None:
        from api_etl.apps import ApiEtlConfig

        suffixes = getattr(ApiEtlConfig, "district_name_suffixes", ["DC", "TC", "MC"])

    district_upper = district_name.upper().strip()

    for suffix in suffixes:
        suffix_upper = suffix.upper()
        if district_upper.endswith(" " + suffix_upper):
            return district_upper[: -(len(suffix_upper) + 1)].strip()
        elif district_upper.endswith(suffix_upper):
            return district_upper[: -len(suffix_upper)].strip()

    return district_upper


def _extract_district_from_questionnaire(questionnaire_title, prefix=None):
    """
    Extract district/PAA token from questionnaire title.

    Handles:
    - DODOSO LA KAYA-RM4-MONDULIDC
    - DODOSO LA KAYA - RM4-KISARAWEDC
    - DODOSO LA KAYA -RM4-KISARAWEDC
    - DODOSO LA KAYA - RM4 - KISARAWEDC
    - titles with unicode dashes
    """
    if not questionnaire_title:
        return ""

    title = str(questionnaire_title).strip()

    # Normalize different dash characters to normal hyphen
    title = (
        title.replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .replace("-", "-")
    )

    # Normalize repeated whitespace
    title = re.sub(r"\s+", " ", title).strip()

    # Robust pattern for all expected title variants
    match = re.match(
        r"^\s*DODOSO\s+LA\s+KAYA\s*-\s*RM4\s*-\s*(.+?)\s*$",
        title,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    # fallback to configured prefix after normalization
    if prefix:
        normalized_prefix = str(prefix).strip()
        normalized_prefix = (
            normalized_prefix.replace("–", "-")
            .replace("—", "-")
            .replace("−", "-")
            .replace("-", "-")
        )
        normalized_prefix = re.sub(r"\s+", " ", normalized_prefix).strip()

        if title.startswith(normalized_prefix):
            return title[len(normalized_prefix):].strip()

    # fallback heuristics
    if "_" in title:
        return title.split("_")[-1].strip()
    if "-" in title:
        return title.split("-")[-1].strip()

    return title.strip()


def _safe_ts(value):
    if not value:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, str):
        try:
            vv = value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(vv).timestamp())
        except Exception:
            return 0
    return 0


def _get_questionnaire_aliases_map():
    return {scope: data.get("names", []) for scope, data in get_paa_aliases().items()}


def _get_candidate_district_names(district_name=None, district_code=None):
    from api_etl.services.survey_solution_service import _normalize_text
    from api_etl.apps import ApiEtlConfig as C

    if not district_name and not district_code:
        return set()

    suffixes = getattr(C, "district_name_suffixes", ["DC", "TC", "MC"])
    aliases_cfg = _get_questionnaire_aliases_map()

    raw = (district_name or "").strip()
    base = _strip_district_suffix(raw, suffixes)

    candidates = {
        _normalize_text(raw),
        _normalize_text(base),
    }
    candidates.update(get_paa_alias_candidates(raw, district_code, C))

    raw_upper = raw.upper().strip()
    base_upper = base.upper().strip()

    alias_values = []
    alias_values.extend(aliases_cfg.get(raw_upper, []))
    alias_values.extend(aliases_cfg.get(base_upper, []))

    # Reverse lookup:
    # if selected district is already one of alias values, include the alias key
    # and all sibling alias values as candidates too.
    raw_norm = _normalize_text(raw)
    base_norm = _normalize_text(base)

    for alias_key, alias_list in aliases_cfg.items():
        alias_norms = {_normalize_text(a) for a in alias_list}
        if raw_norm in alias_norms or base_norm in alias_norms:
            alias_values.append(alias_key)
            alias_values.extend(alias_list)

    for alias in alias_values:
        candidates.add(_normalize_text(alias))
        candidates.add(_normalize_text(_strip_district_suffix(alias, suffixes)))

    return {c for c in candidates if c}


# -----------------------------------------------------------------------------
# GQL Types
# -----------------------------------------------------------------------------
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
    questionnaireVersion = graphene.Int()
    datePulled = graphene.DateTime()
    status = graphene.String()
    errorMessage = graphene.String()


class PulledQuestionnaireConnection(graphene.relay.Connection):
    class Meta:
        node = PulledQuestionnaireGQLType

    totalCount = graphene.Int()

    def resolve_totalCount(root, info, **kwargs):
        try:
            return len(root.iterable)
        except Exception:
            return None


# -----------------------------------------------------------------------------
# Pulled questionnaires (history + fallback)
# -----------------------------------------------------------------------------
def resolve_pulled_questionnaires(root, info, **kwargs):
    from api_etl.stale_imports import fail_stale_running_imports

    queryset = PulledHistory.get_queryset(None, info.context.user)

    region_code = kwargs.get("region_code") or kwargs.get("regionCode")
    if region_code:
        queryset = queryset.filter(region_code=region_code)

    district_code = kwargs.get("district_code") or kwargs.get("districtCode")
    if district_code:
        queryset = queryset.filter(district_code=district_code)

    fail_stale_running_imports(district_code=district_code, user=info.context.user)

    queryset = queryset.order_by("-date_pulled", "-id")

    if not queryset.exists():
        return _get_fallback_pulled_questionnaires(region_code, district_code)

    items = []
    for record in queryset:
        items.append(
            PulledQuestionnaireGQLType(
                paaName=record.paa_name,
                numberOfHouseholds=record.number_of_households,
                numberOfMembers=(record.n_individuals_inserted or 0)
                + (record.n_individuals_updated or 0),
                questionnaireVersion=record.questionnaire_version,
                datePulled=record.date_pulled,
                status=record.status,
                errorMessage=record.error_message,
            )
        )
    return items


def _get_fallback_pulled_questionnaires(region_code=None, district_code=None):
    queryset = Individual.objects.filter(is_deleted=False)

    if region_code or district_code:
        location_filters = Q()
        if region_code:
            location_filters &= Q(location__parent__code=region_code) | Q(location__code=region_code)
        if district_code:
            location_filters &= Q(location__code=district_code)
        queryset = queryset.filter(location_filters)

    batch_groups = {}
    for individual in queryset.filter(json_ext__ss_batch__isnull=False):
        ss_batch = individual.json_ext.get("ss_batch")
        if ss_batch:
            if ss_batch not in batch_groups:
                batch_groups[ss_batch] = {
                    "group_codes": set(),
                    "paa_name": (individual.location.name if individual.location else "Unknown"),
                    "date_created": individual.date_created,
                }
            group_code = individual.json_ext.get("group_code")
            if group_code:
                batch_groups[ss_batch]["group_codes"].add(group_code)

    results = []
    for _, data in batch_groups.items():
        results.append(
            PulledQuestionnaireGQLType(
                paaName=data["paa_name"],
                numberOfHouseholds=len(data["group_codes"]),
                numberOfMembers=0,
                questionnaireVersion=None,
                datePulled=data["date_created"],
                status="completed",
                errorMessage=None,
            )
        )
    results.sort(key=lambda x: x.datePulled, reverse=True)
    return results


# -----------------------------------------------------------------------------
# Survey Solutions HQ Questionnaire Listing (PAA-aware)
# -----------------------------------------------------------------------------
class QuestionnaireGQLType(graphene.ObjectType):
    identity = graphene.String()
    id = graphene.String()
    title = graphene.String()
    version = graphene.Int()
    variable = graphene.String()
    last_entry_date = graphene.String()
    matching_score = graphene.Int()
    matching_strategy = graphene.String()


def _paa_from_pseudo_district_code(district_code: str):
    """
    Pseudo "districts" used only for legacy import UI:
      9101 = Pemba
      9102 = Unguja

    Real Zanzibar location codes are resolved through api_etl.paa_aliases.
    """
    scope = get_paa_scope(code=district_code)
    if scope:
        return scope

    mapping = {
        "9101": "PEMBA",
        "9102": "UNGUJA",
    }
    return mapping.get(str(district_code))


def _filter_questionnaires_by_allowed_paas(questionnaires, allowed_paas):
    """
    For Zanzibar region selection (code=91) when user did NOT pick a district/PAA,
    show only the two PAA questionnaires: UNGUJA & PEMBA.
    """
    from api_etl.services.survey_solution_service import _normalize_text
    from api_etl.apps import ApiEtlConfig as C

    suffixes = getattr(C, "district_name_suffixes", ["DC", "TC", "MC"])
    prefix = getattr(C, "questionnaire_title_prefix", "DODOSO LA KAYA - RM4-")

    allowed_norm = {_normalize_text(p) for p in (allowed_paas or [])}

    results = []
    for q in questionnaires or []:
        title = q.get("Title", "")
        if not title:
            continue

        q_district = _extract_district_from_questionnaire(title, prefix)
        q_district_base = _strip_district_suffix(q_district, suffixes)
        q_norm = _normalize_text(q_district_base)

        if q_norm in allowed_norm:
            q2 = dict(q)
            q2["matching_score"] = 100
            q2["matching_strategy"] = "paa-allowed"
            results.append(q2)

    results.sort(
        key=lambda x: (
            int(x.get("Version", 0) or 0),
            _safe_ts(x.get("LastEntryDate")),
        ),
        reverse=True,
    )
    return results


def _filter_questionnaires_by_paa(questionnaires, district_name=None, district_code=None, region_code=None):
    """
    Score questionnaires by how well their extracted district/PAA token matches
    the requested district name.

    This improves matching for:
    - KISARAWE -> KISARAWEDC / KISARAWE DC / KISARAWE
    - KAKONKO -> KAKONKODC / KAKONKO DC / KAKONKOTC / KAKONKO TC
    - KARATU -> KARATU
    - Zanzibar PAA aliases such as PEMBA / UNGUJA
    """
    from api_etl.services.survey_solution_service import _normalize_text
    from api_etl.apps import ApiEtlConfig as C

    if not questionnaires:
        return []

    suffixes = getattr(C, "district_name_suffixes", ["DC", "TC", "MC"])
    prefix = getattr(C, "questionnaire_title_prefix", "DODOSO LA KAYA - RM4-")

    candidates = _get_candidate_district_names(district_name, district_code)

    scored = []
    for q in questionnaires:
        title = q.get("Title", "")
        if not title:
            continue

        q_district = _extract_district_from_questionnaire(title, prefix)
        q_district_base = _strip_district_suffix(q_district, suffixes)

        q_norm = _normalize_text(q_district)
        q_base_norm = _normalize_text(q_district_base)
        q_candidates = get_paa_alias_candidates(q_district_base, config=C)

        score = 0
        strategy = "no-match"

        if not district_name and not district_code:
            score = 50
            strategy = "no-filter"
        elif q_candidates and q_candidates.intersection(candidates):
            score = 100
            strategy = "paa-alias-match"
        elif q_norm in candidates:
            score = 100
            strategy = "exact-or-alias-match"
        elif q_base_norm in candidates:
            score = 95
            strategy = "base-or-alias-match"
        elif any(candidate and (candidate in q_norm or candidate in q_base_norm) for candidate in candidates):
            score = 60
            strategy = "partial-match"

        q2 = dict(q)
        q2["matching_score"] = score
        q2["matching_strategy"] = strategy
        scored.append(q2)

    scored.sort(
        key=lambda x: (
            x.get("matching_score", 0),
            int(x.get("Version", 0) or 0),
            _safe_ts(x.get("LastEntryDate")),
        ),
        reverse=True,
    )
    return scored


def resolve_available_questionnaires(info, **kwargs):
    from api_etl.sources.survey_solutions_export_source import SurveySolutionsExportSource
    from api_etl.apps import ApiEtlConfig as C
    import logging

    LOG = logging.getLogger(__name__)

    district_code = kwargs.get("district_code") or kwargs.get("districtCode")
    region_code = kwargs.get("region_code") or kwargs.get("regionCode")
    district_name = kwargs.get("district_name") or kwargs.get("districtName")
    show_all = kwargs.get("show_all", False) or kwargs.get("showAll", False)

    source = SurveySolutionsExportSource()
    source.base_url = getattr(C, "export_base_url", "")
    source.workspace = getattr(C, "export_workspace", "")
    source.meta_api_prefix = getattr(C, "meta_api_prefix", "/api/v1")

    try:
        questionnaires = source.list_questionnaires()
        if not questionnaires:
            return []

        paa_name = _paa_from_pseudo_district_code(district_code) if district_code else None
        if paa_name:
            district_name = paa_name

        allowed_scopes = scopes_for_codes([region_code], C)
        if str(region_code) == "91":
            allowed_scopes.update(["UNGUJA", "PEMBA"])

        if allowed_scopes and not district_code and not district_name:
            questionnaires = _filter_questionnaires_by_allowed_paas(
                questionnaires,
                allowed_paas=sorted(allowed_scopes),
            )
        else:
            if district_code or region_code or district_name:
                questionnaires = _filter_questionnaires_by_paa(
                    questionnaires,
                    district_name=district_name,
                    district_code=district_code,
                    region_code=region_code,
                )
                if not show_all:
                    questionnaires = [
                        q for q in questionnaires if q.get("matching_score", 0) > 0
                    ]

    except Exception as e:
        LOG.exception("Failed to fetch questionnaires from HQ")
        raise Exception(f"Failed to fetch questionnaires from HQ: {str(e)}")

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


# =============================================================================
# Real-time Survey Monitoring Dashboard — GraphQL types & resolvers
# =============================================================================
class SurveyDailyCountGQLType(graphene.ObjectType):
    date = graphene.String()
    count = graphene.Int()
    cumulative = graphene.Int()


class SurveyFunnelStageGQLType(graphene.ObjectType):
    stage = graphene.String()
    count = graphene.Int()


class SurveyEnumeratorStatGQLType(graphene.ObjectType):
    name = graphene.String()
    supervisor_name = graphene.String()
    completed = graphene.Int()
    approved_by_supervisor = graphene.Int()
    approved_by_hq = graphene.Int()
    rejected = graphene.Int()
    total = graphene.Int()
    last_activity = graphene.String()


class SurveyHeatmapCellGQLType(graphene.ObjectType):
    day_of_week = graphene.Int()
    hour = graphene.Int()
    count = graphene.Int()


class SurveyQuestionnaireOptionGQLType(graphene.ObjectType):
    identity = graphene.String()
    id = graphene.String()
    version = graphene.Int()
    title = graphene.String()
    count = graphene.Int()


class SurveyInterviewGQLType(graphene.ObjectType):
    interview_id = graphene.String()
    interview_key = graphene.String()
    questionnaire_id = graphene.String()
    questionnaire_title = graphene.String()
    questionnaire_version = graphene.Int()
    assignment_id = graphene.Int()
    responsible_name = graphene.String()
    responsible_role = graphene.String()
    supervisor_name = graphene.String()
    status = graphene.String()
    errors_count = graphene.Int()
    not_answered_count = graphene.Int()
    created_at_utc = graphene.String()
    last_entry_at_utc = graphene.String()
    status_changed_at = graphene.String()
    duration_minutes = graphene.Float()


class SurveyDashboardMetricsGQLType(graphene.ObjectType):
    total_interviews = graphene.Int()
    in_progress = graphene.Int()
    completed = graphene.Int()
    approved_by_supervisor = graphene.Int()
    approved_by_hq = graphene.Int()
    sent_to_capital = graphene.Int()
    rejected_by_supervisor = graphene.Int()
    rejected_by_hq = graphene.Int()
    rejection_rate = graphene.Float()
    supervisor_backlog = graphene.Int()
    hq_backlog = graphene.Int()
    pending_review_backlog = graphene.Int()
    avg_interview_duration_minutes = graphene.Float()
    active_enumerators = graphene.Int()
    target_total = graphene.Int()
    sample_size = graphene.Int()
    hq_base_url = graphene.String()
    last_polled_at = graphene.String()
    questionnaires = graphene.List(SurveyQuestionnaireOptionGQLType)
    daily_productivity = graphene.List(SurveyDailyCountGQLType)
    completion_series = graphene.List(SurveyDailyCountGQLType)
    approval_funnel = graphene.List(SurveyFunnelStageGQLType)
    enumerator_leaderboard = graphene.List(SurveyEnumeratorStatGQLType)
    activity_heatmap = graphene.List(SurveyHeatmapCellGQLType)


def _iso(value):
    if not value:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def resolve_survey_dashboard(info, **kwargs):
    from api_etl.services import survey_dashboard_service as svc

    questionnaire_id = kwargs.get("questionnaire_id") or kwargs.get("questionnaireId")

    # Self-heal: if the cache is stale, kick off a (non-blocking) poll.
    try:
        svc.maybe_async_refresh(questionnaire_id=questionnaire_id)
    except Exception:  # noqa: BLE001
        pass

    m = svc.compute_metrics(questionnaire_id=questionnaire_id)
    return SurveyDashboardMetricsGQLType(
        total_interviews=m["totalInterviews"],
        in_progress=m["inProgress"],
        completed=m["completed"],
        approved_by_supervisor=m["approvedBySupervisor"],
        approved_by_hq=m["approvedByHq"],
        sent_to_capital=m["sentToCapital"],
        rejected_by_supervisor=m["rejectedBySupervisor"],
        rejected_by_hq=m["rejectedByHq"],
        rejection_rate=m["rejectionRate"],
        supervisor_backlog=m["supervisorBacklog"],
        hq_backlog=m["hqBacklog"],
        pending_review_backlog=m["pendingReviewBacklog"],
        avg_interview_duration_minutes=m["avgInterviewDurationMinutes"],
        active_enumerators=m["activeEnumerators"],
        target_total=m["targetTotal"],
        sample_size=m.get("sampleSize", 0),
        hq_base_url=m.get("hqBaseUrl") or "",
        last_polled_at=m["lastPolledAt"],
        questionnaires=[
            SurveyQuestionnaireOptionGQLType(
                identity=q.get("identity"), id=q.get("id"), version=q.get("version"),
                title=q.get("title"), count=q.get("count", 0),
            )
            for q in m.get("questionnaires", [])
        ],
        daily_productivity=[SurveyDailyCountGQLType(**d) for d in m["dailyProductivity"]],
        completion_series=[SurveyDailyCountGQLType(**d) for d in m["completionSeries"]],
        approval_funnel=[SurveyFunnelStageGQLType(**d) for d in m["approvalFunnel"]],
        enumerator_leaderboard=[
            SurveyEnumeratorStatGQLType(
                name=d["name"],
                supervisor_name=d["supervisorName"],
                completed=d["completed"],
                approved_by_supervisor=d["approvedBySupervisor"],
                approved_by_hq=d["approvedByHq"],
                rejected=d["rejected"],
                total=d["total"],
                last_activity=d["lastActivity"],
            )
            for d in m["enumeratorLeaderboard"]
        ],
        activity_heatmap=[
            SurveyHeatmapCellGQLType(day_of_week=d["dayOfWeek"], hour=d["hour"], count=d["count"])
            for d in m["activityHeatmap"]
        ],
    )


def resolve_survey_interviews(info, **kwargs):
    from api_etl.services import survey_dashboard_service as svc

    questionnaire_id = kwargs.get("questionnaire_id") or kwargs.get("questionnaireId")
    status = kwargs.get("status")
    responsible_name = kwargs.get("responsible_name") or kwargs.get("responsibleName")
    search = kwargs.get("search")
    from_date = kwargs.get("from_date") or kwargs.get("fromDate")
    limit = kwargs.get("limit") or kwargs.get("first") or 50

    rows = svc.list_interviews(
        questionnaire_id=questionnaire_id,
        status=status,
        responsible_name=responsible_name,
        search=search,
        from_date=from_date,
        limit=limit,
    )
    return [
        SurveyInterviewGQLType(
            interview_id=r.interview_id,
            interview_key=r.interview_key,
            questionnaire_id=r.questionnaire_id,
            questionnaire_title=r.questionnaire_title,
            questionnaire_version=r.questionnaire_version,
            assignment_id=r.assignment_id,
            responsible_name=r.responsible_name,
            responsible_role=r.responsible_role,
            supervisor_name=r.supervisor_name,
            status=r.status,
            errors_count=r.errors_count,
            not_answered_count=r.not_answered_count,
            created_at_utc=_iso(r.created_at_utc),
            last_entry_at_utc=_iso(r.last_entry_at_utc),
            status_changed_at=_iso(r.status_changed_at),
            duration_minutes=r.duration_minutes,
        )
        for r in rows
    ]
