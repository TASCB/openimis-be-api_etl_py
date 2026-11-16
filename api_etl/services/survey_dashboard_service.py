"""Real-time Survey Monitoring Dashboard — poll & cache backend.
Design notes: docs/SURVEY_DASHBOARD_DEVELOPER_GUIDE.md."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import timedelta, timezone as dt_timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from requests import HTTPError
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

from api_etl.apps import ApiEtlConfig as C
from api_etl.models import SurveyInterviewCache, SurveyDashboardSnapshot, PulledHistory
from api_etl.services import hq_client

LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Survey Solutions interview statuses (see SS REST API docs)
# --------------------------------------------------------------------------- #
S_RESTORED = "Restored"
S_CREATED = "Created"
S_SUPERVISOR_ASSIGNED = "SupervisorAssigned"
S_INTERVIEWER_ASSIGNED = "InterviewerAssigned"
S_REJECTED_BY_SUPERVISOR = "RejectedBySupervisor"
S_READY_FOR_INTERVIEW = "ReadyForInterview"
S_SENT_TO_CAPI = "SentToCapi"
S_RESTARTED = "Restarted"
S_COMPLETED = "Completed"
S_APPROVED_BY_SUPERVISOR = "ApprovedBySupervisor"
S_REJECTED_BY_HEADQUARTERS = "RejectedByHeadquarters"
S_APPROVED_BY_HEADQUARTERS = "ApprovedByHeadquarters"
S_DELETED = "Deleted"

IN_PROGRESS_STATUSES = {
    S_RESTORED,
    S_CREATED,
    S_SUPERVISOR_ASSIGNED,
    S_INTERVIEWER_ASSIGNED,
    S_READY_FOR_INTERVIEW,
    S_RESTARTED,
    S_REJECTED_BY_SUPERVISOR,
}
REJECTED_STATUSES = {S_REJECTED_BY_SUPERVISOR, S_REJECTED_BY_HEADQUARTERS}
# "Completed or beyond" — interview at least reached the supervisor's queue.
COMPLETED_OR_BEYOND = {
    S_COMPLETED,
    S_SENT_TO_CAPI,
    S_APPROVED_BY_SUPERVISOR,
    S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_HEADQUARTERS,
}
APPROVED_SUP_OR_BEYOND = {
    S_SENT_TO_CAPI,
    S_APPROVED_BY_SUPERVISOR,
    S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_HEADQUARTERS,
}

_LAST_POLL_CACHE_KEY = "api_etl:survey_dashboard:last_poll_at"
_LAST_FAIL_CACHE_KEY = "api_etl:survey_dashboard:last_fail_at"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _cfg(name: str, default=None):
    return getattr(C, name, default)


class InvalidQuestionnaireId(ValueError):
    pass


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _clean_guid(value) -> str:
    """Dash-less 32-hex GUID, or InvalidQuestionnaireId: the value goes into HQ
    GraphQL query text and cache keys, so nothing else may pass."""
    g = str(value or "").strip().replace("-", "")
    if len(g) != 32 or not set(g) <= _HEX_DIGITS:
        raise InvalidQuestionnaireId("Unknown questionnaire")
    return g


def _split_qid(questionnaire_id: Optional[str]):
    """Return (guid, version) from a "GUID$version" identity; guid is dash-less."""
    if not questionnaire_id:
        return None, None
    s = str(questionnaire_id).strip()
    if "$" in s:
        guid, ver = s.rsplit("$", 1)
        if not ver.strip().isdigit():
            raise InvalidQuestionnaireId("Unknown questionnaire")
        return _clean_guid(guid), int(ver)
    return _clean_guid(s), None


def validate_questionnaire_id(questionnaire_id: Optional[str]) -> Optional[str]:
    """Canonical "guid$version" / "guid" for a user-supplied scope, None for all."""
    guid, version = _split_qid(questionnaire_id)
    if not guid:
        return None
    return f"{guid}${version}" if version is not None else guid


def _scope_key(questionnaire_id: Optional[str]) -> str:
    """Key for per-scope cache entries and snapshots: full identity, "" = all."""
    return validate_questionnaire_id(questionnaire_id) or ""


def _dashed_guid(guid) -> str:
    g = _clean_guid(guid)
    return f"{g[0:8]}-{g[8:12]}-{g[12:16]}-{g[16:20]}-{g[20:32]}"


def _first(d: Dict[str, Any], *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
        # case-insensitive fallback
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(str(n).lower())
        if v not in (None, ""):
            return v
    return default


def _normalize_dt(dt):
    """
    Normalise a datetime to match this project's USE_TZ mode so it can be saved
    to a DateTimeField and compared with ``timezone.now()`` without errors:
      * USE_TZ=True  → ensure aware (HQ timestamps are UTC).
      * USE_TZ=False → ensure naive in the server's local time.
    """
    if dt is None:
        return None
    if settings.USE_TZ:
        if timezone.is_naive(dt):
            return dt.replace(tzinfo=dt_timezone.utc)
        return dt
    # USE_TZ is False → Django expects naive local datetimes.
    if timezone.is_aware(dt):
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _local(dt):
    """Return ``dt`` in local time for date/hour bucketing, tolerant of USE_TZ."""
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return timezone.localtime(dt)
    return dt


def _dt(value):
    if not value:
        return None
    if hasattr(value, "tzinfo"):
        return _normalize_dt(value)
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = parse_datetime(s)
        if dt is None:
            d = parse_date(s)  # accept plain "YYYY-MM-DD"
            if d is not None:
                from datetime import datetime as _datetime
                dt = _datetime(d.year, d.month, d.day)
        return _normalize_dt(dt)
    except Exception:
        return None


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _payload_items(payload: Any, *, item_keys: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    keys = item_keys or ["Items", "items", "Interviews", "interviews", "Questionnaires", "questionnaires",
                         "Supervisors", "supervisors", "Interviewers", "interviewers", "Users", "users"]
    if isinstance(payload, dict):
        items = None
        for key in keys:
            cand = payload.get(key)
            if isinstance(cand, list):
                items = cand
                break
        if items is None:
            items = payload if all(isinstance(v, list) is False for v in payload.values()) else []
        total = payload.get("TotalCount")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            total = None
        return [x for x in items if isinstance(x, dict)], total
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], None
    return [], None


def _hq_paged_items(path: str, *, params: Optional[Dict[str, Any]] = None, item_keys: Optional[List[str]] = None,
                    page_size: int = 200, max_pages: int = 20) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = hq_client.json_get(path, params={**(params or {}), "limit": page_size, "offset": page})
        items, total = _payload_items(payload, item_keys=item_keys)
        if not items:
            break
        rows.extend(items)
        if total is not None and len(rows) >= total:
            break
        if len(items) < page_size:
            break
    return rows


def _interviews_request(params: Dict[str, Any]):
    """One GET against ``/api/v1/interviews`` → (items_list, total_count)."""
    try:
        payload = hq_client.json_get("interviews", params=params)
    except ValueError:
        if not hq_client.endpoint_base():
            raise
        LOG.error("Survey dashboard: /interviews did not return JSON")
        return [], None
    return _payload_items(payload, item_keys=["Interviews", "Items", "interviews"])


def _base_params(questionnaire_id: Optional[str]) -> Dict[str, Any]:
    guid, version = _split_qid(questionnaire_id)
    p: Dict[str, Any] = {}
    if guid:
        p["questionnaireId"] = guid
    if version is not None:
        p["questionnaireVersion"] = version
    return p


def fetch_total_count(questionnaire_id: Optional[str] = None, status: Optional[str] = None) -> Optional[int]:
    """Cheap: ask HQ for one interview and read ``TotalCount`` of the (filtered) set."""
    # /api/v1/interviews paginates with pageSize/page, not limit/offset
    params = {"pageSize": 1, "page": 1, **_base_params(questionnaire_id)}
    if status:
        params["status"] = status
    _items, total = _interviews_request(params)
    return total


# All Survey Solutions interview statuses we count for the dashboard.
ALL_STATUSES = [
    S_CREATED, S_RESTORED, S_SUPERVISOR_ASSIGNED, S_INTERVIEWER_ASSIGNED,
    S_READY_FOR_INTERVIEW, S_REJECTED_BY_SUPERVISOR, S_SENT_TO_CAPI,
    S_RESTARTED, S_COMPLETED, S_APPROVED_BY_SUPERVISOR,
    S_REJECTED_BY_HEADQUARTERS, S_APPROVED_BY_HEADQUARTERS,
]
# Statuses the live feed / sample fetch prioritises (the ones managers act on).
FEED_STATUSES = [
    S_COMPLETED, S_REJECTED_BY_SUPERVISOR, S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_SUPERVISOR, S_APPROVED_BY_HEADQUARTERS, S_INTERVIEWER_ASSIGNED,
]


def _graphql_where_base(questionnaire_id: Optional[str]) -> List[str]:
    guid, version = _split_qid(questionnaire_id)
    where = []
    if guid:
        where.append(f'questionnaireId: {{eq: "{_dashed_guid(guid)}"}}')
        if version is not None:
            where.append(f"questionnaireVersion: {{eq: {int(version)}}}")
    return where


def fetch_status_counts(questionnaire_id: Optional[str] = None) -> Dict[str, int]:
    """Exact per-status counts for the scope: GraphQL filteredCount when available,
    else one REST TotalCount request per status."""
    if _cfg("dashboard_graphql_enabled", True):
        try:
            counts = hq_client.graphql_status_counts(_graphql_where_base(questionnaire_id), ALL_STATUSES)
            if counts:
                LOG.info("Survey dashboard: status counts via GraphQL (qid=%s) = %s", questionnaire_id or "ALL", counts)
                return counts
        except Exception:
            LOG.info("Survey dashboard: GraphQL status counts unavailable — using REST TotalCount", exc_info=True)

    grand_total = None
    try:
        grand_total = fetch_total_count(questionnaire_id)  # unfiltered
    except Exception:
        LOG.warning("Survey dashboard: unfiltered TotalCount query failed", exc_info=True)

    counts: Dict[str, int] = {}
    for st in ALL_STATUSES:
        try:
            n = fetch_total_count(questionnaire_id, status=st)
            if n is not None:
                counts[st] = int(n)
        except Exception:
            LOG.warning("Survey dashboard: failed to count status=%s", st, exc_info=True)

    total = sum(counts.values())
    if grand_total is not None and total != grand_total:
        LOG.warning(
            "Survey dashboard: status counts sum (%s) != grand total (%s) — a status filter may be unreliable; counts=%s",
            total, grand_total, counts,
        )
    LOG.info(
        "Survey dashboard: status counts (qid=%s) = %s (grandTotal=%s, sum=%s)",
        questionnaire_id or "ALL", counts, grand_total, total,
    )
    return counts


def _to_float(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_numeric_report(payload: Any) -> Optional[Dict[str, Any]]:
    """Parse the /statistics numeric report. HQ 22.02.5 returns ``totalRow`` as an
    object with string values (read it alone: data rows + totalRow double count);
    older builds sent positional rows whose trailing 9 columns are count, average,
    median, sum, min, p05, p50, p95, max."""
    if not isinstance(payload, dict):
        return None
    total_row = payload.get("totalRow") or payload.get("TotalRow")
    if isinstance(total_row, dict):
        c = _to_float(_first(total_row, "count", "Count"))
        s = _to_float(_first(total_row, "sum", "Sum"))
        if c is not None and s is not None:
            if c <= 0:
                return None
            return {"total": round(s), "average": round(s / c, 1), "count": int(c)}
    rows = payload.get("data") or payload.get("Data") or []
    total = 0.0
    count = 0.0
    seen = False
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 9:
            c, s = _to_float(r[-9]), _to_float(r[-6])
        elif isinstance(r, dict):
            c, s = _to_float(_first(r, "count", "Count")), _to_float(_first(r, "sum", "Sum"))
        else:
            continue
        if c is None or s is None:
            continue
        count += c
        total += s
        seen = True
    if not seen:
        totals = payload.get("totals") or payload.get("Totals")
        if isinstance(totals, (list, tuple)) and len(totals) >= 9:
            c, s = _to_float(totals[-9]), _to_float(totals[-6])
            if c is not None and s is not None:
                count, total, seen = c, s, True
    if not seen or count <= 0:
        return None
    return {"total": round(total), "average": round(total / count, 1), "count": int(count)}


def _household_metric_cache_key(questionnaire_id: Optional[str]) -> str:
    return f"api_etl:survey_dashboard:hhsize:{_scope_key(questionnaire_id) or 'ALL'}"


# "All PAAs" household figure: Σ of the per-questionnaire /statistics totals,
# built on the worker poll (≈1 s per questionnaire) and read by the request.
_HH_ALL_AGG_KEY = "api_etl:survey_dashboard:hhsize:ALL:agg"


def _household_variable() -> str:
    return str(_cfg("dashboard_household_size_variable", "hh_size") or "hh_size").strip()


def _household_metric_from_cache(questionnaire_id: Optional[str]) -> Dict[str, Any]:
    """Aggregate the locally harvested per-interview household-size answers.

    Every in-scope interview lands in exactly one of three buckets, so callers can
    tell a complete total from a partial one:
      answered   -- a valid numeric answer we summed
      noAnswer   -- harvested, but the variable was absent or non-numeric
      pending    -- not harvested yet
    """
    agg = _qs(questionnaire_id).filter(status__in=COMPLETED_OR_BEYOND).aggregate(
        total=Sum("hh_size"),
        n=Count("id", filter=Q(hh_size__isnull=False)),
        no_answer=Count("id", filter=Q(hh_size__isnull=True, hh_size_fetched_at__isnull=False)),
        pending=Count("id", filter=Q(hh_size_fetched_at__isnull=True)),
    )
    count = int(agg["n"] or 0)
    total = float(agg["total"] or 0)
    pending = int(agg["pending"] or 0)
    no_answer = int(agg["no_answer"] or 0)
    in_scope = count + no_answer + pending
    return {
        "total": round(total) if count else None,
        "average": round(total / count, 1) if count else None,
        "count": count if count else (0 if pending == 0 else None),
        "variable": _household_variable(),
        "pending": pending,
        "noAnswer": no_answer,
        "inScope": in_scope,
        "coverage": round((count + no_answer) / in_scope * 100.0, 1) if in_scope else None,
        "source": "live-harvest",
    }


def _load_household_metric(questionnaire_id: Optional[str]) -> Dict[str, Any]:
    if not questionnaire_id:
        agg = cache.get(_HH_ALL_AGG_KEY)
        if isinstance(agg, dict):
            return agg
    cached = cache.get(_household_metric_cache_key(questionnaire_id))
    if isinstance(cached, dict):
        return cached
    return _household_metric_from_cache(questionnaire_id)


def _statistics_ok(questionnaire_id: Optional[str]) -> bool:
    """True when HQ /statistics already gives this scope's household figures, so
    the per-interview harvest is not needed."""
    key = _HH_ALL_AGG_KEY if not questionnaire_id else _household_metric_cache_key(questionnaire_id)
    cached = cache.get(key)
    return isinstance(cached, dict) and cached.get("source") == "statistics"


def _combine_household_metrics(metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = sum(m.get("total") or 0 for m in metrics)
    count = sum(m.get("count") or 0 for m in metrics)
    pending = sum(m.get("pending") or 0 for m in metrics)
    no_answer = sum(m.get("noAnswer") or 0 for m in metrics)
    in_scope = sum(m.get("inScope") or 0 for m in metrics)
    sources = {m.get("source") for m in metrics}
    return {
        "total": round(total) if count else None,
        "average": round(total / count, 1) if count else None,
        "count": count if count else None,
        "variable": _household_variable(),
        "pending": pending,
        "noAnswer": no_answer,
        "inScope": in_scope,
        "coverage": round((in_scope - pending) / in_scope * 100.0, 1) if in_scope else None,
        "source": "statistics" if sources == {"statistics"} else "mixed",
        "questionnaires": len(metrics),
    }


def _hq_questionnaire_identities() -> List[str]:
    """Every questionnaire identity on HQ (all versions); the dashboard's own list
    when HQ cannot be listed."""
    try:
        from api_etl.sources.survey_solutions_export_source import SurveySolutionsExportSource

        src = SurveySolutionsExportSource()
        src.base_url = _cfg("export_base_url", "")
        src.workspace = _cfg("export_workspace", "")
        src.meta_api_prefix = _cfg("meta_api_prefix", "/api/v1")
        out = []
        for q in src.list_questionnaires() or []:
            try:
                ident = validate_questionnaire_id(f'{q.get("Id") or q.get("QuestionnaireId")}${q.get("Version")}')
            except InvalidQuestionnaireId:
                continue
            if ident and ident not in out:
                out.append(ident)
        if out:
            return out
    except Exception:
        LOG.warning("Survey dashboard: could not list HQ questionnaires for the all-PAA household total", exc_info=True)
    return [q["identity"] for q in list_dashboard_questionnaires() if q.get("identity")]


def refresh_all_household_metric(force: bool = False) -> Optional[Dict[str, Any]]:
    """Build the "All PAAs" household figure from per-questionnaire /statistics
    totals. Runs on the worker only; at most every dashboard_hhsize_all_refresh_seconds."""
    stamp_key = f"{_HH_ALL_AGG_KEY}:built_at"
    every = _to_int(_cfg("dashboard_hhsize_all_refresh_seconds", 1800), 1800) or 1800
    if not force and cache.get(stamp_key) and isinstance(cache.get(_HH_ALL_AGG_KEY), dict):
        return None
    metrics = []
    for ident in _hq_questionnaire_identities():
        try:
            metrics.append(fetch_household_size_metric(ident))
        except Exception:
            LOG.warning("Survey dashboard: household metric failed for %s", ident, exc_info=True)
    if not metrics:
        return None
    agg = _combine_household_metrics(metrics)
    cache.set(_HH_ALL_AGG_KEY, agg, None)
    cache.set(stamp_key, timezone.now().isoformat(), every)
    LOG.info("Survey dashboard: all-PAA household total = %s members over %s answers (%s questionnaires, source=%s)",
             agg.get("total"), agg.get("count"), len(metrics), agg.get("source"))
    return agg


def _invalidate_household_metric(questionnaire_id: Optional[str] = None) -> None:
    cache.delete(_household_metric_cache_key(questionnaire_id))
    if questionnaire_id:
        cache.delete(_household_metric_cache_key(None))


# Status scope used for both the /statistics report and the local aggregate.
# Derived from COMPLETED_OR_BEYOND so the two rungs of the source ladder can never
# drift apart and report different populations for the same KPI.
_STATS_SCOPE_STATUSES = sorted(COMPLETED_OR_BEYOND)


def _fetch_statistics_metric(questionnaire_id: Optional[str], token: str) -> Optional[Dict[str, Any]]:
    guid, version = _split_qid(questionnaire_id)
    if not guid:
        return None
    # HQ 22.02.5 answers 500 unless the DataTables paging params start+length are
    # present; length=1 is enough because only totalRow is read.
    params = {
        "QuestionnaireId": guid,
        "Question": token,
        "Min": 0,
        "statuses[]": _STATS_SCOPE_STATUSES,
        "start": 0,
        "length": 1,
    }
    if version is not None:
        params["Version"] = version
    payload = hq_client.json_get("statistics", params=params)
    return _parse_numeric_report(payload)


def _find_household_question_token(questionnaire_id: Optional[str]) -> Optional[str]:
    guid, version = _split_qid(questionnaire_id)
    if not guid or version is None:
        return None
    variable = _household_variable().lower()
    explicit = str(_cfg("dashboard_household_size_question_key", "") or "").strip()
    if explicit:
        return explicit
    try:
        payload = hq_client.json_get("statistics/questions", params={"questionnaireId": guid, "version": version})
    except Exception:
        LOG.debug("Survey dashboard: statistics questions request failed questionnaireId=%s", questionnaire_id, exc_info=True)
        return None
    items, _total = _payload_items(payload, item_keys=["Items", "items"])
    if not items and isinstance(payload, list):
        items = [x for x in payload if isinstance(x, dict)]
    fallback = None
    for node in items:
        var = str(_first(node, "VariableName", "variableName") or "").strip()
        public_key = str(_first(node, "Id", "id") or "").strip()
        text = str(_first(node, "QuestionText", "questionText", "Label") or "").strip().lower()
        qtype = str(_first(node, "Type", "type") or "").lower()
        if var and var.lower() == variable:
            return public_key or var
        if fallback is None and "household size" in text and any(t in qtype for t in ["numeric", "integer", "number"]):
            fallback = public_key or var
    if fallback:
        return fallback
    return None


def fetch_household_size_metric(questionnaire_id: Optional[str]) -> Dict[str, Any]:
    """Σ/avg/valid-count of the household-size question: /statistics when the HQ
    report works, else the locally harvested hh_size column."""
    cache_key = _household_metric_cache_key(questionnaire_id)
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    guid, version = _split_qid(questionnaire_id)
    variable = _household_variable()
    scope = f"{guid}${version if version is not None else ''}"
    unsupported_key = f"api_etl:survey_dashboard:hhsize:unsupported:{scope}"
    fail_key = f"api_etl:survey_dashboard:hhsize:failing:{scope}"

    result = None
    if guid and not cache.get(unsupported_key) and not cache.get(fail_key):
        tokens = []
        explicit = str(_cfg("dashboard_household_size_question_key", "") or "").strip()
        if explicit:
            tokens.append(explicit)
        if variable and variable not in tokens:
            tokens.append(variable)
        stats_error = False
        for token in tokens:
            try:
                result = _fetch_statistics_metric(questionnaire_id, token)
            except HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 404:
                    cache.set(unsupported_key, True, _to_int(_cfg("dashboard_stats_unsupported_seconds", 21600), 21600) or 21600)
                else:
                    cache.set(fail_key, True, _to_int(_cfg("dashboard_stats_fail_backoff_seconds", 1800), 1800) or 1800)
                    LOG.warning("Survey dashboard: /statistics failed (HTTP %s) for %s — using harvested hh_size", status, questionnaire_id)
                stats_error = True
                break
            except Exception:
                cache.set(fail_key, True, _to_int(_cfg("dashboard_stats_fail_backoff_seconds", 1800), 1800) or 1800)
                LOG.warning("Survey dashboard: /statistics request failed for %s — using harvested hh_size", questionnaire_id, exc_info=True)
                stats_error = True
                break
            if result:
                break
        if result is None and not stats_error:
            discovered = _find_household_question_token(questionnaire_id)
            if discovered and discovered not in tokens:
                try:
                    result = _fetch_statistics_metric(questionnaire_id, discovered)
                except Exception:
                    LOG.debug("Survey dashboard: /statistics failed for discovered token %s", discovered, exc_info=True)

    if result:
        # HQ aggregated the whole scope server-side, so coverage is complete by
        # definition -- keep the same keys the harvested path returns.
        result = {
            **result,
            "variable": variable,
            "source": "statistics",
            "pending": 0,
            "noAnswer": 0,
            "inScope": result.get("count"),
            "coverage": 100.0,
        }
    else:
        result = _household_metric_from_cache(questionnaire_id)
    cache.set(cache_key, result, _to_int(_cfg("dashboard_question_stats_cache_seconds", 600), 600) or 600)
    return result


def _fetch_interview_hh_size(interview_id: str, variable: str) -> Optional[float]:
    payload = hq_client.json_get(f"interviews/{interview_id}")
    answers = payload.get("Answers") if isinstance(payload, dict) else None
    for answer in answers or []:
        if not isinstance(answer, dict):
            continue
        if str(answer.get("VariableName") or "").strip().lower() != variable.lower():
            continue
        try:
            return float(answer.get("Answer"))
        except (TypeError, ValueError):
            return None
    return None


def _harvest_hh_sizes(questionnaire_id: Optional[str] = None, *, budget: Optional[int] = None, throttle: float = 0.0) -> int:
    """Fetch hh_size for cached interviews that are new or changed; bounded per call."""
    variable = _household_variable()
    if budget is None:
        budget = _to_int(_cfg("dashboard_hhsize_fetch_budget", 50), 50) or 50
    rows = (
        _qs(questionnaire_id)
        .filter(status__in=COMPLETED_OR_BEYOND)
        .filter(Q(hh_size_fetched_at__isnull=True) | Q(server_updated_at_utc__gt=F("hh_size_fetched_at")))
        .order_by(F("hh_size_fetched_at").asc(nulls_first=True), "-server_updated_at_utc")
    )
    max_transient = _to_int(_cfg("dashboard_hhsize_max_consecutive_failures", 5), 5) or 5
    done = 0
    transient = 0
    for row in rows[: max(1, budget)]:
        try:
            value = _fetch_interview_hh_size(row.interview_id, variable)
        except HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                # The interview is gone or unreadable at HQ. Stamp it so it leaves
                # the head of the queue -- ordering is fetched_at NULLS FIRST, so
                # leaving it unstamped blocks every older row indefinitely. It then
                # shows up as noAnswer rather than silently stalling the sweep.
                row.hh_size = None
                row.hh_size_fetched_at = timezone.now()
                row.save(update_fields=["hh_size", "hh_size_fetched_at", "updated_at"])
                LOG.info("Survey dashboard: hh_size unavailable for %s (HTTP %s) — marked as no-answer", row.interview_id, status)
                done += 1
                continue
            transient += 1
            LOG.warning("Survey dashboard: hh_size fetch failed for %s (HTTP %s)", row.interview_id, status, exc_info=True)
            if transient >= max_transient:
                LOG.warning("Survey dashboard: %s consecutive hh_size failures — ending this sweep", transient)
                break
            continue
        except Exception:
            transient += 1
            LOG.warning("Survey dashboard: hh_size fetch failed for %s", row.interview_id, exc_info=True)
            if transient >= max_transient:
                LOG.warning("Survey dashboard: %s consecutive hh_size failures — ending this sweep", transient)
                break
            continue
        transient = 0
        row.hh_size = value
        row.hh_size_fetched_at = timezone.now()
        row.save(update_fields=["hh_size", "hh_size_fetched_at", "updated_at"])
        done += 1
        if throttle:
            time.sleep(throttle)
    if done:
        _invalidate_household_metric(questionnaire_id)
    return done


def _fetch_supervisor_team_rows(supervisor_id: str) -> List[Dict[str, Any]]:
    routes = [
        (f"supervisors/{supervisor_id}/interviewers", None),
        ("interviewers", {"supervisorId": supervisor_id}),
        ("interviewers", {"supervisor_id": supervisor_id}),
        ("interviewers", {"supervisor": supervisor_id}),
    ]
    for idx, (route, params) in enumerate(routes):
        try:
            rows = _hq_paged_items(route, params=params, item_keys=["Interviewers", "interviewers", "Items", "items", "Users", "users"])
            if rows or idx == 0:
                return rows
        except HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                LOG.debug("Survey dashboard: supervisor team endpoint unsupported route=%s", route)
                continue
            LOG.warning("Survey dashboard: supervisor team request failed route=%s status=%s", route, status, exc_info=True)
        except Exception:
            LOG.warning("Survey dashboard: supervisor team request failed route=%s", route, exc_info=True)
    return []


_ROSTER_CACHE_KEY = "api_etl:survey_dashboard:supervisor_roster"
_EMPTY_ROSTER = {"supervisors": [], "interviewerMap": {}, "supervisorAliases": {}}


def _load_roster() -> Dict[str, Any]:
    """Roster from cache only — refreshed by the poller, never in the request path."""
    cached = cache.get(_ROSTER_CACHE_KEY)
    return cached if isinstance(cached, dict) else dict(_EMPTY_ROSTER)


def fetch_supervisor_roster() -> Dict[str, Any]:
    cache_key = _ROSTER_CACHE_KEY
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    supervisors: List[Dict[str, Any]] = []
    interviewer_map: Dict[str, str] = {}
    supervisor_aliases: Dict[str, Dict[str, Any]] = {}
    try:
        rows = _hq_paged_items("supervisors", item_keys=["Supervisors", "supervisors", "Items", "items", "Users", "users"])
    except Exception:
        LOG.debug("Survey dashboard: could not fetch supervisor roster", exc_info=True)
        rows = []

    for row in rows:
        supervisor_id = str(_first(row, "SupervisorId", "UserId", "Id", "id") or "").strip()
        username = str(_first(row, "UserName", "Username", "Login") or "").strip()
        display_name = str(_first(row, "FullName", "Name", "Title", "UserName", "Username") or username or supervisor_id).strip()
        if not display_name:
            continue
        team_rows = _fetch_supervisor_team_rows(supervisor_id) if supervisor_id else []
        team_aliases: Set[str] = set()
        for iv in team_rows:
            for alias in [
                _first(iv, "FullName", "Name", "ResponsibleName"),
                _first(iv, "UserName", "Username", "Login"),
            ]:
                norm = _normalize_name(alias)
                if norm:
                    interviewer_map[norm] = display_name
                    team_aliases.add(norm)
        supervisor = {
            "id": supervisor_id or display_name,
            "name": display_name,
            "username": username or display_name,
            "teamSize": len(team_aliases),
        }
        supervisors.append(supervisor)
        for alias in [display_name, username]:
            norm = _normalize_name(alias)
            if norm:
                supervisor_aliases[norm] = supervisor

    result = {
        "supervisors": sorted(supervisors, key=lambda x: (x.get("name") or "").lower()),
        "interviewerMap": interviewer_map,
        "supervisorAliases": supervisor_aliases,
    }
    cache.set(cache_key, result, _to_int(_cfg("dashboard_roster_cache_seconds", 600), 600) or 600)
    return result


# --------------------------------------------------------------------------- #
# HQ GraphQL: recency-ordered change detection
# --------------------------------------------------------------------------- #
_STATUS_CANON = {s.upper(): s for s in ALL_STATUSES + [S_DELETED]}


def _graphql_node_to_brief(node: Dict[str, Any]) -> Dict[str, Any]:
    status = str(node.get("status") or "")
    role = str(node.get("responsibleRole") or "")
    return {
        "InterviewId": str(node.get("id") or "").replace("-", ""),
        "Key": node.get("key"),
        "Status": _STATUS_CANON.get(status.upper(), status.title() or None),
        "ResponsibleId": node.get("responsibleId"),
        "ResponsibleName": node.get("responsibleName"),
        "ResponsibleRole": role.title() if role else None,
        "SupervisorName": node.get("supervisorName"),
        "ErrorsCount": node.get("errorsCount"),
        "NotAnsweredCount": node.get("notAnsweredCount"),
        "AssignmentId": node.get("assignmentId"),
        "CreatedDate": node.get("createdDate"),
        "LastEntryDate": node.get("updateDateUtc"),
        "ServerLastUpdate": node.get("updateDateUtc"),
        "QuestionnaireId": node.get("questionnaireId"),
        "QuestionnaireVersion": node.get("questionnaireVersion"),
    }


def _iso_utc(dt) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _cursor_key(questionnaire_id: Optional[str]) -> str:
    return f"api_etl:survey_dashboard:cursor:{_scope_key(questionnaire_id) or 'ALL'}"


def _load_cursor(questionnaire_id: Optional[str]) -> Optional[str]:
    val = cache.get(_cursor_key(questionnaire_id))
    if val:
        return str(val)
    latest = (
        _qs(questionnaire_id)
        .exclude(server_updated_at_utc__isnull=True)
        .order_by("-server_updated_at_utc")
        .values_list("server_updated_at_utc", flat=True)
        .first()
    )
    if latest:
        return _iso_utc(latest - timedelta(minutes=5))  # overlap so restarts miss nothing
    lookback = _to_int(_cfg("dashboard_cursor_lookback_hours", 24), 24) or 24
    return _iso_utc(timezone.now() - timedelta(hours=lookback))


def fetch_recent_briefs(
    questionnaire_id: Optional[str] = None,
    *,
    since: Optional[str] = None,
    max_interviews: int = 600,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Briefs changed since ``since`` (oldest first) via GraphQL → (briefs, new_cursor)."""
    where = _graphql_where_base(questionnaire_id)
    if since:
        where.append(f'updateDateUtc: {{gt: "{since}"}}')
    take = min(100, max(1, max_interviews))
    briefs: List[Dict[str, Any]] = []
    cursor = since
    skip = 0
    while len(briefs) < max_interviews:
        parts = [f"take: {take}", f"skip: {skip}", "order: {updateDateUtc: ASC}"]
        if where:
            parts.append("where: {" + ", ".join(where) + "}")
        query = (
            "{ interviews(" + hq_client._ws_arg() + ", ".join(parts) + ") { nodes { "
            "id key status responsibleId responsibleName responsibleRole supervisorName "
            "errorsCount notAnsweredCount assignmentId createdDate updateDateUtc "
            "questionnaireId questionnaireVersion } } }"
        )
        data = hq_client.graphql_post(query)
        nodes = ((data.get("interviews") or {}).get("nodes")) or []
        if not nodes:
            break
        for n in nodes:
            briefs.append(_graphql_node_to_brief(n))
            u = n.get("updateDateUtc")
            if u and (cursor is None or str(u) > str(cursor)):
                cursor = str(u)
        if len(nodes) < take:
            break
        skip += take
    return briefs, cursor


def iter_sample_briefs(
    questionnaire_id: Optional[str] = None,
    *,
    max_interviews: int = 600,
) -> Iterable[Dict[str, Any]]:
    """REST fallback sample: a few pages per feed status plus an unfiltered page."""
    if max_interviews <= 0:
        return
    page_size = _to_int(_cfg("dashboard_interview_page_size", 200), 200) or 200
    base = _base_params(questionnaire_id)
    queries: List[Dict[str, Any]] = [dict(base)]  # unfiltered page(s)
    for st in FEED_STATUSES:
        queries.append({**base, "status": st})
    per_query_budget = max(1, int((max_interviews + len(queries) - 1) / len(queries)))
    yielded = 0
    for q in queries:
        taken = 0
        page = 1
        while taken < per_query_budget:
            try:
                items, _t = _interviews_request({"pageSize": page_size, "page": page, **q})
            except Exception:
                LOG.warning("Survey dashboard: sample fetch failed for query=%s page=%s", q, page, exc_info=True)
                break
            if not items:
                break
            for it in items:
                if isinstance(it, dict):
                    yield it
                    yielded += 1
                    taken += 1
                    if max_interviews and yielded >= max_interviews:
                        LOG.info("Survey dashboard: sampled %s interview brief(s) for the feed (cap)", yielded)
                        return
                    if taken >= per_query_budget:
                        break
            if len(items) < page_size:
                break
            page += 1
    LOG.info("Survey dashboard: sampled %s interview brief(s) for the feed", yielded)


# ---- status-count cache (so the GraphQL layer reads them without re-hitting HQ) ----
def _status_counts_key(questionnaire_id: Optional[str]) -> str:
    return f"api_etl:survey_dashboard:status_counts:{_scope_key(questionnaire_id) or 'ALL'}"


def _store_status_counts(questionnaire_id: Optional[str], counts: Dict[str, int]) -> None:
    cache.set(_status_counts_key(questionnaire_id), dict(counts), None)


def _load_status_counts(questionnaire_id: Optional[str]) -> Optional[Dict[str, int]]:
    val = cache.get(_status_counts_key(questionnaire_id))
    return dict(val) if isinstance(val, dict) else None


# --------------------------------------------------------------------------- #
# polling / caching
# --------------------------------------------------------------------------- #
def _featured_label(brief: Dict[str, Any]) -> Optional[str]:
    """
    The /interviews brief doesn't carry the short interview "key"; it does carry
    ``FeaturedQuestions`` (the questionnaire's identifying questions, e.g. ward /
    village / household number). Join their answers into a readable label.
    """
    fq = brief.get("FeaturedQuestions") or brief.get("featuredQuestions")
    if not isinstance(fq, list):
        return None
    answers = [str(q.get("Answer")).strip() for q in fq if isinstance(q, dict) and q.get("Answer") not in (None, "")]
    label = " / ".join(a for a in answers if a)
    return label[:64] or None


def _questionnaire_title_map() -> Dict[str, str]:
    """Best-effort {questionnaire_guid: latest title} from the configured HQ list (cached)."""
    try:
        from api_etl.sources.survey_solutions_export_source import SurveySolutionsExportSource

        src = SurveySolutionsExportSource()
        src.base_url = _cfg("export_base_url", "")
        src.workspace = _cfg("export_workspace", "")
        src.meta_api_prefix = _cfg("meta_api_prefix", "/api/v1")
        titles: Dict[str, str] = {}
        for q in src.list_questionnaires() or []:
            guid = q.get("Id") or q.get("QuestionnaireId")
            title = q.get("Title")
            if guid and title:
                titles[str(guid).replace("-", "")] = title
        return titles
    except Exception:
        LOG.debug("Survey dashboard: could not fetch questionnaire titles", exc_info=True)
        return {}


def _brief_to_fields(brief: Dict[str, Any], q_titles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    created = _dt(_first(brief, "CreatedDate", "CreatedAtUtc", "WasCreatedOn", "ReceivedByDeviceAtUtc"))
    last_entry = _dt(_first(brief, "LastEntryDate", "UpdateDateUtc", "ServerLastUpdate"))
    server_updated = _dt(_first(brief, "ServerLastUpdate", "UpdateDateUtc", "LastEntryDate"))
    duration = None
    if created and last_entry and last_entry >= created:
        duration = round((last_entry - created).total_seconds() / 60.0, 2)
    guid = _first(brief, "QuestionnaireId", "questionnaireId")
    title = _first(brief, "QuestionnaireTitle", "Title")
    if not title and q_titles and guid:
        title = q_titles.get(str(guid).replace("-", ""))
    return {
        "interview_key": _first(brief, "Key", "InterviewKey", "interview__key") or _featured_label(brief),
        "questionnaire_id": str(guid).replace("-", "") if guid else None,
        "questionnaire_title": title,
        "questionnaire_version": _to_int(_first(brief, "QuestionnaireVersion", "questionnaireVersion"), None),
        "assignment_id": _to_int(_first(brief, "AssignmentId", "assignmentId"), None),
        "responsible_id": _first(brief, "ResponsibleId", "responsibleId"),
        "responsible_name": _first(brief, "ResponsibleName", "responsibleName"),
        "responsible_role": _first(brief, "ResponsibleRole", "responsibleRole"),
        "supervisor_name": _first(brief, "SupervisorName", "TeamLeadName", "supervisorName"),
        "status": _first(brief, "Status", "status"),
        "errors_count": _to_int(_first(brief, "ErrorsCount", "errorsCount"), 0),
        "not_answered_count": _to_int(_first(brief, "NotAnsweredCount", "notAnsweredCount"), 0),
        "created_at_utc": created,
        "last_entry_at_utc": last_entry,
        "server_updated_at_utc": server_updated,
        "duration_minutes": duration,
        # Feed / leaderboard sort key (indexed); None → stamped "now" by _upsert_briefs.
        "activity_at": last_entry or server_updated,
    }


def _brief_json_ext(brief: Dict[str, Any]) -> Dict[str, Any]:
    fq = brief.get("FeaturedQuestions") or brief.get("featuredQuestions")
    out: Dict[str, Any] = {}
    if isinstance(fq, list):
        out["featured"] = [
            {"question": q.get("Question"), "answer": q.get("Answer")}
            for q in fq if isinstance(q, dict)
        ]
    return out


def _upsert_briefs(briefs_iter: Iterable[Dict[str, Any]]) -> Tuple[int, int, int]:
    """Upsert interview briefs into SurveyInterviewCache → (seen, created, changed)."""
    now = timezone.now()
    roster = fetch_supervisor_roster()
    interviewer_map = roster.get("interviewerMap", {}) if isinstance(roster, dict) else {}
    briefs_by_id: Dict[str, Dict[str, Any]] = {}
    for b in briefs_iter:
        iid = _first(b, "InterviewId", "Id", "interviewId")
        if iid:
            briefs_by_id[str(iid).replace("-", "")] = b
    q_titles = _questionnaire_title_map() if briefs_by_id else {}

    seen = changed = created_rows = 0
    with transaction.atomic():
        existing = {
            row.interview_id: row
            for row in SurveyInterviewCache.objects.filter(interview_id__in=list(briefs_by_id.keys()))
        }
        to_create: List[SurveyInterviewCache] = []
        to_update: List[SurveyInterviewCache] = []
        update_names: Set[str] = set()
        for iid, brief in briefs_by_id.items():
            fields = _brief_to_fields(brief, q_titles)
            if fields.get("activity_at") is None:
                prev = existing.get(iid)
                fields["activity_at"] = (prev.activity_at if prev else None) or now
            if not fields.get("supervisor_name") and fields.get("responsible_name"):
                derived = interviewer_map.get(_normalize_name(fields.get("responsible_name")))
                if derived:
                    fields["supervisor_name"] = derived
            jext = _brief_json_ext(brief)
            seen += 1
            row = existing.get(iid)
            if row is None:
                obj = SurveyInterviewCache(interview_id=iid, status_changed_at=now, **fields)
                obj.json_ext = jext
                to_create.append(obj)
                created_rows += 1
                continue
            update_fields = []
            if (row.status or "") != (fields.get("status") or ""):
                row.status_changed_at = now
                changed += 1
                update_fields.append("status_changed_at")
            for k, v in fields.items():
                if getattr(row, k) != v:
                    setattr(row, k, v)
                    update_fields.append(k)
            if jext and row.json_ext != jext:
                row.json_ext = jext
                update_fields.append("json_ext")
            if update_fields:
                row.updated_at = now  # auto_now does not fire in bulk_update
                update_names.update(update_fields)
                to_update.append(row)
        if to_update:
            update_names.add("updated_at")
            SurveyInterviewCache.objects.bulk_update(to_update, sorted(update_names), batch_size=500)
        if to_create:
            SurveyInterviewCache.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
    return seen, created_rows, changed


def poll_interviews(
    questionnaire_id: Optional[str] = None,
    *,
    with_sample: bool = True,
    sample_size: Optional[int] = None,
) -> Dict[str, Any]:
    """One poll cycle: status counts + recent-activity slice. "Last synced" is
    stamped only when HQ answered; an empty answer records a failure instead."""
    now = timezone.now()

    # only overwrite cached counts when HQ actually answered
    status_counts = fetch_status_counts(questionnaire_id)
    total_count = sum(status_counts.values()) if status_counts else 0
    hq_answered = bool(status_counts) and total_count > 0
    if hq_answered:
        _store_status_counts(questionnaire_id, status_counts)
        cache.set(_LAST_POLL_CACHE_KEY, now.isoformat(), None)
        cache.delete(_LAST_FAIL_CACHE_KEY)
    else:
        cache.set(_LAST_FAIL_CACHE_KEY, now.isoformat(), None)
        LOG.warning("Survey dashboard poll (qid=%s): HQ returned no status counts — not marking as synced",
                    questionnaire_id or "ALL")

    seen = changed = created_rows = 0
    if with_sample and hq_answered:
        cap = sample_size if sample_size is not None else _to_int(_cfg("dashboard_sample_size", 600), 600)
        briefs = None
        new_cursor = None
        if _cfg("dashboard_graphql_enabled", True):
            try:
                briefs, new_cursor = fetch_recent_briefs(
                    questionnaire_id, since=_load_cursor(questionnaire_id), max_interviews=int(cap or 0),
                )
            except Exception:
                LOG.warning("Survey dashboard: GraphQL recent slice failed — falling back to REST sample", exc_info=True)
        if briefs is None:
            briefs = list(iter_sample_briefs(questionnaire_id, max_interviews=int(cap or 0)))
        seen, created_rows, changed = _upsert_briefs(briefs)
        if new_cursor:
            cache.set(_cursor_key(questionnaire_id), new_cursor, None)

    LOG.info(
        "Survey dashboard poll (qid=%s): totalCount=%s, sampled=%s (%s new, %s status changes)",
        questionnaire_id or "ALL", total_count, seen, created_rows, changed,
    )
    return {
        "seen": seen, "created": created_rows, "changed": changed, "harvested": 0,
        "total_count": total_count, "status_counts": status_counts,
        "polled_at": now.isoformat() if hq_answered else None,
    }


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def _qs(questionnaire_id: Optional[str]):
    qs = SurveyInterviewCache.objects.exclude(status=S_DELETED)
    guid, version = _split_qid(questionnaire_id)
    if guid:
        qs = qs.filter(questionnaire_id=guid)
        if version is not None:
            qs = qs.filter(questionnaire_version=version)
    return qs


def _activity_order(qs):
    # activity_at is stored and indexed (migration 0007), so this is an index scan.
    return qs.order_by("-activity_at", "-id")


def _row_activity_at(row: Dict[str, Any]):
    return row.get("activity_at") or row.get("last_entry_at_utc") or row.get("server_updated_at_utc")


def last_polled_at():
    """When HQ last answered a poll (None when it never has since the cache was cleared)."""
    raw = cache.get(_LAST_POLL_CACHE_KEY)
    return _dt(raw) if raw else None


def last_failed_at():
    raw = cache.get(_LAST_FAIL_CACHE_KEY)
    return _dt(raw) if raw else None


def _identity(guid: Optional[str], version) -> Optional[str]:
    if not guid:
        return None
    g = str(guid)
    if "$" in g:
        return g  # already an identity
    if version not in (None, ""):
        return f"{g}${int(version)}" if str(version).isdigit() else f"{g}${version}"
    return g


_HEX = set("0123456789abcdefABCDEF-")


def _looks_like_guid(g) -> bool:
    s = str(g or "")
    return len(s) >= 30 and all(c in _HEX for c in s)


def list_dashboard_questionnaires() -> List[Dict[str, Any]]:
    """
    The questionnaires (≈ PAAs) the dashboard knows about — built from the local
    interview cache and the ETL pull history (cheap DB query, no HQ round-trip).
    Returns ``[{identity, id, version, title, count}]`` so the UI picker can offer
    each PAA and pass ``identity`` back as the ``questionnaireId`` scope. Defensive:
    never raises (a broken row must not take the whole dashboard down).
    """
    by_guid: Dict[str, Dict[str, Any]] = {}

    def _merge(guid, version, title, count):
        try:
            if not guid:
                return
            g0 = str(guid).split("$", 1)[0].strip().replace("-", "")
            if not _looks_like_guid(g0):
                return  # skip placeholders like "pending-auto-detection"
            ver = version if version not in (None, "") else None
            if ver is None and "$" in str(guid):
                tail = str(guid).split("$", 1)[1]
                ver = int(tail) if str(tail).isdigit() else None
            try:
                cnt = int(count or 0)
            except (TypeError, ValueError):
                cnt = 0
            ident = _identity(g0, ver) or g0
            ttl = (str(title).strip() if title else None) or None
            cur = by_guid.get(g0)
            if cur is None:
                by_guid[g0] = {"id": g0, "version": ver, "identity": ident, "title": ttl, "count": cnt}
            else:
                cur["count"] += cnt
                if not cur.get("title") and ttl:
                    cur["title"] = ttl
                if cur.get("version") in (None, "") and ver not in (None, ""):
                    cur["version"] = ver
                    cur["identity"] = ident
        except Exception:
            LOG.debug("Survey dashboard: skipping bad questionnaire row %r", guid, exc_info=True)

    # interviews actually in the cache (these have synced samples)
    try:
        for r in (
            SurveyInterviewCache.objects.exclude(questionnaire_id__isnull=True).exclude(questionnaire_id="")
            .values("questionnaire_id", "questionnaire_version", "questionnaire_title")
            .annotate(c=Count("id"))
        ):
            _merge(r["questionnaire_id"], r.get("questionnaire_version"), r.get("questionnaire_title"), r["c"])
    except Exception:
        LOG.warning("Survey dashboard: failed to read questionnaires from cache", exc_info=True)

    # PAAs that have been imported via the ETL (may not have a synced sample yet)
    try:
        for r in (
            PulledHistory.objects.exclude(questionnaire_id__isnull=True).exclude(questionnaire_id="")
            .values("questionnaire_id", "questionnaire_version", "questionnaire_title", "paa_name").distinct()
        ):
            _merge(r["questionnaire_id"], r.get("questionnaire_version"), r.get("questionnaire_title") or r.get("paa_name"), 0)
    except Exception:
        LOG.debug("Survey dashboard: could not read PulledHistory for questionnaire list", exc_info=True)

    out = list(by_guid.values())
    out.sort(key=lambda x: (x.get("title") or x.get("id") or "").lower())
    return out


def _cache_status_counts(questionnaire_id: Optional[str]) -> Dict[str, int]:
    """Status counts from the local cache table (last-resort fallback)."""
    qs = _qs(questionnaire_id)
    return {r["status"]: r["c"] for r in qs.values("status").annotate(c=Count("id")) if r["status"]}


def _snapshots(questionnaire_id: Optional[str]) -> List[SurveyDashboardSnapshot]:
    """Daily snapshots for the scope, oldest first. Rows are keyed by full identity
    (GUID$version); rows written before 2026-11-16 used the GUID alone and are read
    as a fallback for dates that have no identity row."""
    key = _scope_key(questionnaire_id)
    guid, _v = _split_qid(questionnaire_id)
    keys = [key] + ([guid] if guid and guid != key else [])
    by_date: Dict[Any, SurveyDashboardSnapshot] = {}
    for snap in SurveyDashboardSnapshot.objects.filter(questionnaire_id__in=keys).order_by("snapshot_date"):
        if snap.snapshot_date not in by_date or snap.questionnaire_id == key:
            by_date[snap.snapshot_date] = snap
    return [by_date[d] for d in sorted(by_date)]


def _snapshot_status_counts(questionnaire_id: Optional[str]) -> Optional[Dict[str, int]]:
    """Last persisted live status counts (survives restarts / empty process cache)."""
    snaps = _snapshots(questionnaire_id)
    snap = snaps[-1] if snaps else None
    if snap and isinstance(snap.json_ext, dict):
        sc = snap.json_ext.get("status_counts")
        if isinstance(sc, dict) and sc:
            return {k: int(v) for k, v in sc.items() if v is not None}
    return None


def _resolve_status_counts(questionnaire_id: Optional[str]) -> Dict[str, int]:
    # Prefer the first source that actually carries data (live cache → last snapshot →
    # the local sample). Fall back to whatever exists if everything is empty/zero.
    candidates = [
        _load_status_counts(questionnaire_id),
        _snapshot_status_counts(questionnaire_id),
        _cache_status_counts(questionnaire_id),
    ]
    for sc in candidates:
        if sc and sum(sc.values()) > 0:
            return sc
    for sc in candidates:
        if sc:
            return sc
    return {}


def compute_metrics(questionnaire_id: Optional[str] = None, *, days: int = 30) -> Dict[str, Any]:
    now = timezone.now()
    active_window = timedelta(hours=_to_int(_cfg("dashboard_active_window_hours", 24), 24) or 24)
    row_cap = _to_int(_cfg("dashboard_metrics_row_cap", 5000), 5000) or 5000

    # --- headline counts: live status counts (exact) when available, else last snapshot / cache ---
    status_counts = _resolve_status_counts(questionnaire_id)
    total = sum(status_counts.values())
    completed = status_counts.get(S_COMPLETED, 0)
    approved_sup = status_counts.get(S_APPROVED_BY_SUPERVISOR, 0)
    approved_hq = status_counts.get(S_APPROVED_BY_HEADQUARTERS, 0)
    sent_to_capital = status_counts.get(S_SENT_TO_CAPI, 0)
    rejected_sup = status_counts.get(S_REJECTED_BY_SUPERVISOR, 0)
    rejected_hq = status_counts.get(S_REJECTED_BY_HEADQUARTERS, 0)
    in_progress = sum(status_counts.get(s, 0) for s in IN_PROGRESS_STATUSES)
    completed_or_beyond = sum(status_counts.get(s, 0) for s in COMPLETED_OR_BEYOND)
    approved_sup_or_beyond = sum(status_counts.get(s, 0) for s in APPROVED_SUP_OR_BEYOND)
    rejected_total = rejected_sup + rejected_hq
    rejection_rate = round((rejected_total / completed_or_beyond) * 100.0, 1) if completed_or_beyond else 0.0
    target_total = _to_int(_cfg("dashboard_target_total", 0), 0) or total
    roster = _load_roster()
    interviewer_map = roster.get("interviewerMap", {}) if isinstance(roster, dict) else {}
    supervisor_aliases = roster.get("supervisorAliases", {}) if isinstance(roster, dict) else {}
    household_metric = _load_household_metric(questionnaire_id)

    # --- sample rows (bounded) for feed-derived stats: leaderboard / heatmap / active / duration ---
    sample_rows = list(
        _activity_order(_qs(questionnaire_id))
        .values(
            "status", "responsible_name", "supervisor_name", "last_entry_at_utc",
            "server_updated_at_utc", "activity_at",
        )[:row_cap]
    )
    sample_size = _qs(questionnaire_id).count()

    leaderboard_rows = [
        r for r in sample_rows
        if (_row_activity_at(r) is not None and (now - _row_activity_at(r)) <= active_window)
    ] or sample_rows

    active_enums = set()
    enum_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"completed": 0, "approvedBySupervisor": 0, "approvedByHq": 0, "rejected": 0, "total": 0,
                 "supervisorName": None, "lastActivity": None}
    )
    supervisor_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"pendingReview": 0, "reviewed": 0, "rejected": 0, "total": 0, "lastActivity": None,
                 "username": None, "teamSize": 0, "activeInterviewers": set()}
    )
    for sup in (roster.get("supervisors", []) if isinstance(roster, dict) else []):
        ss = supervisor_stats[sup["name"]]
        ss["username"] = sup.get("username")
        ss["teamSize"] = sup.get("teamSize", 0)

    heat: Dict[tuple, int] = defaultdict(int)
    for r in leaderboard_rows:
        st = r["status"] or ""
        name = (r["responsible_name"] or "").strip() or "(unassigned)"
        supervisor_name = (r["supervisor_name"] or "").strip()
        if not supervisor_name and name and name != "(unassigned)":
            supervisor_name = interviewer_map.get(_normalize_name(name), "")
        sup_meta = supervisor_aliases.get(_normalize_name(supervisor_name)) if supervisor_name else None
        if sup_meta:
            supervisor_name = sup_meta.get("name") or supervisor_name
        es = enum_stats[name]
        es["total"] += 1
        es["supervisorName"] = es["supervisorName"] or supervisor_name or None
        act = _row_activity_at(r)
        if act and (es["lastActivity"] is None or act > es["lastActivity"]):
            es["lastActivity"] = act
        if st in COMPLETED_OR_BEYOND:
            es["completed"] += 1
        if st in APPROVED_SUP_OR_BEYOND:
            es["approvedBySupervisor"] += 1
        if st == S_APPROVED_BY_HEADQUARTERS:
            es["approvedByHq"] += 1
        if st in REJECTED_STATUSES:
            es["rejected"] += 1
        if supervisor_name:
            ss = supervisor_stats[supervisor_name]
            if sup_meta:
                ss["username"] = ss.get("username") or sup_meta.get("username")
                ss["teamSize"] = max(ss.get("teamSize", 0), sup_meta.get("teamSize", 0))
            ss["total"] += 1
            if st == S_COMPLETED:
                ss["pendingReview"] += 1
            if st in APPROVED_SUP_OR_BEYOND or st == S_REJECTED_BY_SUPERVISOR:
                ss["reviewed"] += 1
            if st == S_REJECTED_BY_SUPERVISOR:
                ss["rejected"] += 1
            if act and (ss["lastActivity"] is None or act > ss["lastActivity"]):
                ss["lastActivity"] = act
            if act and (now - act) <= active_window and name and name != "(unassigned)":
                ss["activeInterviewers"].add(name)
        if act and (now - act) <= active_window and r["responsible_name"]:
            active_enums.add(name)

    for r in sample_rows:
        le = r["last_entry_at_utc"]
        if le:
            lo = _local(le)
            heat[(lo.weekday(), lo.hour)] += 1

    # --- S-curve / daily productivity from recorded snapshots (built up over polls) ---
    start_day = (_local(now) - timedelta(days=days - 1)).date()
    all_snaps = _snapshots(questionnaire_id)
    by_date = {s.snapshot_date: s.cumulative_completed for s in all_snaps}
    last_cum = 0
    for s in all_snaps:
        if s.snapshot_date < start_day:
            last_cum = s.cumulative_completed
        else:
            break
    daily_productivity = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        cur = by_date.get(d, last_cum)
        delta = max(0, cur - last_cum)
        daily_productivity.append({"date": d.isoformat(), "count": delta, "cumulative": cur})
        last_cum = cur

    # --- approval funnel ---
    approval_funnel = [
        {"stage": "Assigned / In field", "count": total},
        {"stage": "Completed by enumerator", "count": completed_or_beyond},
        {"stage": "Approved by supervisor", "count": approved_sup_or_beyond},
        {"stage": "Approved by HQ", "count": approved_hq},
    ]

    # --- enumerator leaderboard (over the sample) ---
    leaderboard = []
    for name, es in enum_stats.items():
        if name == "(unassigned)":
            continue
        leaderboard.append({
            "name": name,
            "supervisorName": es["supervisorName"],
            "completed": es["completed"],
            "approvedBySupervisor": es["approvedBySupervisor"],
            "approvedByHq": es["approvedByHq"],
            "rejected": es["rejected"],
            "total": es["total"],
            "lastActivity": es["lastActivity"].isoformat() if es["lastActivity"] else None,
        })
    leaderboard.sort(key=lambda x: (x["completed"], x["approvedByHq"], x["lastActivity"] or "", x["total"]), reverse=True)
    leaderboard = leaderboard[:25]

    supervisor_leaderboard = []
    for name, ss in supervisor_stats.items():
        supervisor_leaderboard.append({
            "name": name,
            "username": ss.get("username"),
            "pendingReview": ss["pendingReview"],
            "reviewed": ss["reviewed"],
            "rejected": ss["rejected"],
            "total": ss["total"],
            "teamSize": ss.get("teamSize", 0),
            "activeInterviewers": len(ss.get("activeInterviewers", set())),
            "lastActivity": ss["lastActivity"].isoformat() if ss["lastActivity"] else None,
        })
    supervisor_leaderboard.sort(key=lambda x: (x["reviewed"], x["pendingReview"], x["activeInterviewers"], x["lastActivity"] or "", x["teamSize"], x["total"]), reverse=True)
    supervisor_leaderboard = supervisor_leaderboard[:20]

    # --- heatmap (7 x 24, over the sample) ---
    heatmap = [{"dayOfWeek": dow, "hour": h, "count": heat.get((dow, h), 0)} for dow in range(7) for h in range(24)]

    try:
        questionnaire_options = list_dashboard_questionnaires()
    except Exception:
        LOG.warning("Survey dashboard: list_dashboard_questionnaires failed", exc_info=True)
        questionnaire_options = []

    polled = last_polled_at()
    failed = last_failed_at()
    return {
        "totalInterviews": total,
        "inProgress": in_progress,
        "completed": completed,
        "approvedBySupervisor": approved_sup,
        "approvedByHq": approved_hq,
        "sentToCapital": sent_to_capital,
        "rejectedBySupervisor": rejected_sup,
        "rejectedByHq": rejected_hq,
        "rejectionRate": rejection_rate,
        "supervisorBacklog": completed,
        "hqBacklog": approved_sup + sent_to_capital,
        "pendingReviewBacklog": completed + approved_sup + sent_to_capital,
        "avgInterviewDurationMinutes": None,  # dropped 2026-10-14: wall-clock, misleading
        "householdMembersTotal": household_metric.get("total"),
        "householdSizeAverage": household_metric.get("average"),
        "householdSizeInterviews": household_metric.get("count"),
        "householdSizeVariable": household_metric.get("variable"),
        "householdSizePending": household_metric.get("pending"),
        "householdSizeNoAnswer": household_metric.get("noAnswer"),
        "householdSizeInScope": household_metric.get("inScope"),
        "householdSizeCoverage": household_metric.get("coverage"),
        "householdSizeSource": household_metric.get("source"),
        "activeEnumerators": len(active_enums),
        "targetTotal": target_total,
        "sampleSize": sample_size,
        "lastPolledAt": (polled.isoformat() if polled else None),
        "lastFailedAt": (failed.isoformat() if failed and (polled is None or failed > polled) else None),
        "hqBaseUrl": hq_client.web_base(),
        "questionnaires": questionnaire_options,
        "dailyProductivity": daily_productivity,
        "statusHistory": status_history(questionnaire_id),
        "approvalFunnel": approval_funnel,
        "enumeratorLeaderboard": leaderboard,
        "supervisorLeaderboard": supervisor_leaderboard,
        "activityHeatmap": heatmap,
    }


def status_history(questionnaire_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return [
        {
            "date": r.snapshot_date.isoformat(),
            "inProgress": r.in_progress,
            "completed": r.completed,
            "approvedBySupervisor": r.approved_by_supervisor,
            "sentToCapi": r.sent_to_capital,
            "approvedByHq": r.approved_by_hq,
            "rejectedBySupervisor": r.rejected_by_supervisor,
            "rejectedByHq": r.rejected_by_hq,
            "cumulativeCompleted": r.cumulative_completed,
        }
        for r in _snapshots(questionnaire_id)
    ]


def list_interviews(
    *,
    questionnaire_id: Optional[str] = None,
    status: Optional[str] = None,
    responsible_name: Optional[str] = None,
    supervisor_name: Optional[str] = None,
    search: Optional[str] = None,
    from_date: Optional[Any] = None,
    limit: int = 50,
) -> List[SurveyInterviewCache]:
    qs = _qs(questionnaire_id)
    if status:
        statuses = [x.strip() for x in str(status).split(",") if x.strip()]
        qs = qs.filter(status__in=statuses)
    if responsible_name:
        qs = qs.filter(responsible_name__icontains=responsible_name)
    if supervisor_name:
        qs = qs.filter(supervisor_name__icontains=supervisor_name)
    if search:
        qs = qs.filter(
            Q(interview_key__icontains=search)
            | Q(responsible_name__icontains=search)
            | Q(supervisor_name__icontains=search)
        )
    if from_date:
        d = _dt(from_date)
        if d is not None:
            # "submitted on/after this date" — use the interview's last-entry time, or
            # when we first observed its status if HQ didn't give a last-entry time.
            qs = qs.filter(Q(last_entry_at_utc__gte=d) | (Q(last_entry_at_utc__isnull=True) & Q(status_changed_at__gte=d)))
    limit = max(1, min(_to_int(limit, 50) or 50, 500))
    return list(_activity_order(qs)[:limit])


# --------------------------------------------------------------------------- #
# snapshots / refresh / staleness guard
# --------------------------------------------------------------------------- #
def _active_enumerators_count(questionnaire_id: Optional[str]) -> int:
    window = timedelta(hours=_to_int(_cfg("dashboard_active_window_hours", 24), 24) or 24)
    cutoff = timezone.now() - window
    return (
        _qs(questionnaire_id)
        .filter(last_entry_at_utc__gte=cutoff)
        .exclude(responsible_name__isnull=True).exclude(responsible_name="")
        .values("responsible_name").distinct().count()
    )


def take_daily_snapshot(questionnaire_id: Optional[str] = None, status_counts: Optional[Dict[str, int]] = None) -> None:
    if not status_counts:
        status_counts = _load_status_counts(questionnaire_id) or _cache_status_counts(questionnaire_id)
    today = _local(timezone.now()).date()
    g = lambda s: int(status_counts.get(s, 0))  # noqa: E731
    completed_or_beyond = sum(g(s) for s in COMPLETED_OR_BEYOND)
    SurveyDashboardSnapshot.objects.update_or_create(
        snapshot_date=today,
        questionnaire_id=_scope_key(questionnaire_id),
        defaults={
            "total_interviews": sum(status_counts.values()),
            "in_progress": sum(g(s) for s in IN_PROGRESS_STATUSES),
            "completed": g(S_COMPLETED),
            "approved_by_supervisor": g(S_APPROVED_BY_SUPERVISOR),
            "approved_by_hq": g(S_APPROVED_BY_HEADQUARTERS),
            "sent_to_capital": g(S_SENT_TO_CAPI),
            "rejected_by_supervisor": g(S_REJECTED_BY_SUPERVISOR),
            "rejected_by_hq": g(S_REJECTED_BY_HEADQUARTERS),
            "cumulative_completed": completed_or_beyond,
            "active_enumerators": _active_enumerators_count(questionnaire_id),
            "json_ext": {"status_counts": dict(status_counts)},
        },
    )


def _needs_backfill(questionnaire_id: Optional[str]) -> bool:
    """True when HQ reports more Completed+ interviews than we hold hh_size for."""
    counts = _load_status_counts(questionnaire_id) or {}
    expected = sum(counts.get(s, 0) for s in COMPLETED_OR_BEYOND)
    if not expected:
        return False
    have = (
        _qs(questionnaire_id)
        .filter(status__in=COMPLETED_OR_BEYOND, hh_size_fetched_at__isnull=False)
        .count()
    )
    return have < expected


def _acquire_lock(key: str, ttl: int) -> bool:
    """Atomic on Redis/LocMem: only one caller gets True until ttl or release."""
    return bool(cache.add(key, timezone.now().isoformat(), ttl))


def backfill_hh_size(
    questionnaire_id: Optional[str],
    *,
    budget: Optional[int] = None,
    throttle: Optional[float] = None,
    sync_briefs: bool = True,
) -> Dict[str, Any]:
    """One bounded, throttled, resumable hh_size harvest pass for a questionnaire.
    Only the first pass of a run re-downloads the briefs (sync_briefs)."""
    if budget is None:
        budget = _to_int(_cfg("dashboard_hhsize_backfill_budget", 500), 500) or 500
    if throttle is None:
        try:
            throttle = float(_cfg("dashboard_hhsize_backfill_throttle", 0.3) or 0)
        except (TypeError, ValueError):
            throttle = 0.3
    lock_key = f"api_etl:survey_dashboard:hhsize_backfill:{_scope_key(questionnaire_id) or 'ALL'}"
    if not _acquire_lock(lock_key, 3600):
        return {"skipped": "backfill already running"}
    try:
        seen = created = 0
        if sync_briefs:
            max_briefs = _to_int(_cfg("dashboard_max_interviews", 50000), 50000) or 50000
            briefs, _cursor = fetch_recent_briefs(questionnaire_id, since=None, max_interviews=max_briefs)
            seen, created, _changed = _upsert_briefs(briefs)
        harvested = _harvest_hh_sizes(questionnaire_id, budget=budget, throttle=throttle)
        remaining = (
            _qs(questionnaire_id)
            .filter(status__in=COMPLETED_OR_BEYOND, hh_size_fetched_at__isnull=True)
            .count()
        )
        LOG.info(
            "Survey dashboard backfill (qid=%s): synced=%s (%s new), harvested=%s, remaining=%s",
            questionnaire_id or "ALL", seen, created, harvested, remaining,
        )
        return {"synced": seen, "created": created, "harvested": harvested, "remaining": remaining}
    finally:
        cache.delete(lock_key)


def _maybe_enqueue_backfill(questionnaire_id: Optional[str]) -> None:
    """Fallback only: when /statistics cannot give a PAA's household figures and
    dashboard_hhsize_backfill_auto is on (default off since 2026-11-16)."""
    if not _cfg("dashboard_hhsize_backfill_auto", False) or not questionnaire_id:
        return
    if not _needs_backfill(questionnaire_id) or not _celery_worker_available():
        return  # without a worker the self-requeuing chain would loop in the caller
    try:
        from api_etl.tasks import backfill_hh_size_task

        backfill_hh_size_task.delay(questionnaire_id)
    except Exception:
        LOG.debug("Survey dashboard: could not enqueue hh_size backfill (no broker?)", exc_info=True)


def prune_interview_cache() -> int:
    """Drop Deleted/stale-in-field rows past retention; Completed+ rows are kept
    because they store the harvested hh_size answers."""
    days = _to_int(_cfg("dashboard_cache_retention_days", 90), 90)
    if not days or days <= 0:
        return 0
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _detail = (
        SurveyInterviewCache.objects.filter(updated_at__lt=cutoff)
        .filter(Q(status=S_DELETED) | ~Q(status__in=COMPLETED_OR_BEYOND))
        .delete()
    )
    if deleted:
        LOG.info("Survey dashboard: pruned %s stale cache row(s)", deleted)
    return deleted


def _refresh_household(questionnaire_id: Optional[str]) -> int:
    """Household figures for the scope: HQ /statistics first; the per-interview
    harvest runs only for a PAA whose /statistics call failed. → hh_size rows harvested."""
    if questionnaire_id:
        cache.delete(_household_metric_cache_key(questionnaire_id))
        fetch_household_size_metric(questionnaire_id)
    else:
        refresh_all_household_metric()
    if _statistics_ok(questionnaire_id):
        return 0
    harvested = 0
    try:
        harvested = _harvest_hh_sizes(questionnaire_id)
    except Exception:
        LOG.warning("Survey dashboard: hh_size harvest failed", exc_info=True)
    _maybe_enqueue_backfill(questionnaire_id)
    return harvested


def refresh_dashboard(questionnaire_id: Optional[str] = None, *, with_sample: bool = True) -> Dict[str, Any]:
    result = poll_interviews(questionnaire_id=questionnaire_id, with_sample=with_sample)
    try:
        prune_interview_cache()
    except Exception:
        LOG.warning("Survey dashboard: cache prune failed", exc_info=True)
    if with_sample and result.get("total_count"):
        try:
            result["harvested"] = _refresh_household(questionnaire_id)
        except Exception:
            LOG.warning("Survey dashboard: household metric refresh failed for %s", questionnaire_id or "ALL", exc_info=True)
        cache.set(_full_poll_key(questionnaire_id), timezone.now().isoformat(), None)
    try:
        if result.get("total_count"):  # don't write an all-zero snapshot when HQ was unreachable
            take_daily_snapshot(questionnaire_id=questionnaire_id, status_counts=result.get("status_counts"))
    except Exception:
        LOG.exception("Survey dashboard: failed to take daily snapshot")
    return result


def _full_poll_key(questionnaire_id: Optional[str]) -> str:
    return f"api_etl:survey_dashboard:full_poll_at:{_scope_key(questionnaire_id) or 'ALL'}"


def _sync_lock_key(questionnaire_id: Optional[str]) -> str:
    return f"api_etl:survey_dashboard:sync:{_scope_key(questionnaire_id) or 'ALL'}"


def _in_fail_backoff() -> bool:
    raw = cache.get(_LAST_FAIL_CACHE_KEY)
    if not raw:
        return False
    fdt = _dt(raw)
    polled = last_polled_at()
    if fdt and polled and polled > fdt:
        return False
    backoff = _to_int(_cfg("dashboard_poll_fail_backoff_seconds", 300), 300) or 300
    return bool(fdt and (timezone.now() - fdt).total_seconds() < backoff)


def _run_in_background(questionnaire_id: Optional[str], with_sample: bool, lock_key: Optional[str]) -> None:
    """Thread body used when there is no Celery broker."""
    from django.db import connection
    try:
        run_refresh(questionnaire_id, with_sample, lock_key)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def run_refresh(questionnaire_id: Optional[str], with_sample: bool, lock_key: Optional[str] = None) -> Dict[str, Any]:
    """Worker entry point (Celery task or thread). Releases lock_key when done."""
    try:
        return refresh_dashboard(questionnaire_id=questionnaire_id, with_sample=with_sample)
    except Exception as exc:
        LOG.warning("Survey dashboard: refresh failed (HQ unreachable?) — backing off", exc_info=True)
        cache.set(_LAST_FAIL_CACHE_KEY, timezone.now().isoformat(), None)
        return {"error": str(exc)}
    finally:
        if lock_key:
            cache.delete(lock_key)


def _celery_worker_available() -> bool:
    """False when tasks would run in the caller: CELERY_TASK_ALWAYS_EAGER (local
    dev without a broker) makes .delay() execute inside the web request."""
    try:
        from celery import current_app

        return not current_app.conf.task_always_eager
    except Exception:
        return False


def _enqueue_refresh(questionnaire_id: Optional[str], with_sample: bool, lock_key: str) -> None:
    if _celery_worker_available():
        try:
            from api_etl.tasks import poll_survey_dashboard_task

            poll_survey_dashboard_task.delay(questionnaire_id, with_sample, lock_key)
            return
        except Exception:
            LOG.info("Survey dashboard: Celery broker unreachable — running the poll in a background thread")
    import threading

    threading.Thread(
        target=_run_in_background, args=(questionnaire_id, with_sample, lock_key), daemon=True,
    ).start()


def start_sync(questionnaire_id: Optional[str] = None) -> bool:
    """"Sync now": queue a full poll on the worker and return at once.
    False when a sync for this scope is already running."""
    lock_key = _sync_lock_key(questionnaire_id)
    ttl = _to_int(_cfg("dashboard_sync_lock_seconds", 900), 900) or 900
    if not _acquire_lock(lock_key, ttl):
        return False
    _enqueue_refresh(questionnaire_id, True, lock_key)
    return True


def sync_running(questionnaire_id: Optional[str] = None) -> bool:
    return bool(cache.get(_sync_lock_key(questionnaire_id)))


def maybe_async_refresh(questionnaire_id: Optional[str] = None) -> bool:
    """
    Self-heal on page load, never blocking the request. When the counts are stale
    (dashboard_poll_interval_seconds) a counts-only poll is queued; when the last
    full poll (feed, leaderboards, household figures) is older than
    dashboard_full_poll_interval_seconds a full poll is queued instead, so servers
    without a scheduler stay current while someone has the page open. A per-scope
    lock and the fail back-off keep an unreachable HQ quiet.
    """
    if not _cfg("dashboard_enabled", True):
        return False
    if _in_fail_backoff() or sync_running(questionnaire_id):
        return False
    now = timezone.now()
    interval = _to_int(_cfg("dashboard_poll_interval_seconds", 60), 60) or 60
    full_interval = _to_int(_cfg("dashboard_full_poll_interval_seconds", 300), 300)
    last = last_polled_at()
    counts_stale = last is None or (now - last).total_seconds() > interval
    need_qid_counts = bool(questionnaire_id) and _load_status_counts(questionnaire_id) is None
    full_raw = cache.get(_full_poll_key(questionnaire_id))
    full_last = _dt(full_raw) if full_raw else None
    full_due = bool(full_interval) and (full_last is None or (now - full_last).total_seconds() > full_interval)
    if not (counts_stale or need_qid_counts or full_due):
        return False
    if full_due:
        return start_sync(questionnaire_id)
    lock_key = f"api_etl:survey_dashboard:polling:{_scope_key(questionnaire_id) or 'ALL'}"
    if not _acquire_lock(lock_key, 90):
        return False
    _enqueue_refresh(questionnaire_id, False, lock_key)
    return True
