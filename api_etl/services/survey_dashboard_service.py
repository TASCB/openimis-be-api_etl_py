"""
Real-time Survey Monitoring Dashboard — backend service.

Survey Solutions does not push status changes, so this module implements the
"poll & cache" half of a poll/broadcast pattern:

  * ``poll_interviews()``       — pages the HQ ``/api/v1/interviews`` endpoint and
                                  upserts each interview brief into
                                  ``SurveyInterviewCache``, recording
                                  ``status_changed_at`` on every transition.
  * ``take_daily_snapshot()``   — rolls the cache up into a
                                  ``SurveyDashboardSnapshot`` row for trend / S-curve charts.
  * ``compute_metrics()``       — derives the KPIs + visualisation series the
                                  GraphQL layer / frontend consume.
  * ``refresh_dashboard()``     — poll + snapshot, used by the Celery task and the
                                  manual "Refresh" mutation.
  * ``maybe_async_refresh()``   — cheap staleness guard the GraphQL query calls so
                                  the dashboard self-heals without Celery beat.

It reuses the HQ connection (base URL / workspace / basic auth) already
configured for the ETL flow via ``api_etl.apps.ApiEtlConfig`` — nothing here
touches the existing ETL pipeline.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta, timezone as dt_timezone, date as date_cls
from typing import Any, Dict, Iterable, List, Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

from api_etl.apps import ApiEtlConfig as C
from api_etl.models import SurveyInterviewCache, SurveyDashboardSnapshot, PulledHistory

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
S_SENT_TO_CAPITAL = "SentToCapital"
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
    S_REJECTED_BY_SUPERVISOR,
}
REJECTED_STATUSES = {S_REJECTED_BY_SUPERVISOR, S_REJECTED_BY_HEADQUARTERS}
# "Completed or beyond" — interview at least reached the supervisor's queue.
COMPLETED_OR_BEYOND = {
    S_COMPLETED,
    S_SENT_TO_CAPITAL,
    S_APPROVED_BY_SUPERVISOR,
    S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_HEADQUARTERS,
}
APPROVED_SUP_OR_BEYOND = {
    S_SENT_TO_CAPITAL,
    S_APPROVED_BY_SUPERVISOR,
    S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_HEADQUARTERS,
}

_LAST_POLL_CACHE_KEY = "api_etl:survey_dashboard:last_poll_at"
_LAST_FAIL_CACHE_KEY = "api_etl:survey_dashboard:last_fail_at"


def _hq_timeout():
    """(connect, read) timeout for HQ requests — short connect so a dead HQ fails fast."""
    connect = _to_int(_cfg("dashboard_hq_connect_timeout", 5), 5) or 5
    read = _to_int(_cfg("dashboard_hq_read_timeout", 60), 60) or 60
    return (connect, read)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _cfg(name: str, default=None):
    return getattr(C, name, default)


def _split_qid(questionnaire_id: Optional[str]):
    """Return (guid, version) from a "GUID$version" identity, version may be None."""
    if not questionnaire_id:
        return None, None
    s = str(questionnaire_id)
    if "$" in s:
        guid, ver = s.rsplit("$", 1)
        try:
            return guid, int(ver)
        except (TypeError, ValueError):
            return guid, None
    return s, None


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


# --------------------------------------------------------------------------- #
# HQ client (reuses the ETL connection settings)
# --------------------------------------------------------------------------- #
def _hq_endpoint_base() -> str:
    # Reuse the helpers from the export source so behaviour stays consistent.
    from api_etl.sources.survey_solutions_export_source import _compose_endpoint_base

    return _compose_endpoint_base(
        base_url=_cfg("export_base_url") or _cfg("base_url"),
        workspace=_cfg("export_workspace") or _cfg("workspace"),
        api_prefix=_cfg("meta_api_prefix", "/api/v1") or "/api/v1",
    ).rstrip("/")


def hq_web_base() -> str:
    """
    The Survey Solutions HQ web base for deep-linking (e.g. an interview review page):
    ``{export_base_url}/{export_workspace}`` from the api_etl module config. Empty if
    the base URL isn't configured.
    """
    base = (str(_cfg("export_base_url") or _cfg("base_url") or "")).strip().rstrip("/")
    if not base:
        return ""
    ws = (str(_cfg("export_workspace") or _cfg("workspace") or "")).strip().strip("/")
    return f"{base}/{ws}" if ws else base


def _hq_request_kwargs() -> Dict[str, Any]:
    from api_etl.sources.survey_solutions_export_source import _merge_auth

    return _merge_auth(
        auth_type=_cfg("auth_type", "basic"),
        username=_cfg("auth_basic_username"),
        password=_cfg("auth_basic_password"),
        bearer=_cfg("auth_bearer_token"),
    )


def _interviews_request(params: Dict[str, Any]):
    """One GET against ``/api/v1/interviews`` → (items_list, total_count)."""
    endpoint_base = _hq_endpoint_base()
    if not endpoint_base:
        raise ValueError("Survey Solutions HQ base URL is not configured (export_base_url).")
    url = f"{endpoint_base}/interviews"
    rkwargs = _hq_request_kwargs()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if "headers" in rkwargs:
        headers.update(rkwargs["headers"])
    rkwargs = {k: v for k, v in rkwargs.items() if k != "headers"}
    r = requests.get(url, params=params, headers=headers, timeout=_hq_timeout(), **rkwargs)
    r.raise_for_status()
    try:
        payload = r.json()
    except Exception:
        LOG.error("Survey dashboard: failed to parse /interviews JSON: %s", (r.text or "")[:500])
        return [], None
    if isinstance(payload, dict):
        items = payload.get("Interviews") or payload.get("Items") or payload.get("interviews") or []
        total = payload.get("TotalCount")
    elif isinstance(payload, list):
        items, total = payload, None
    else:
        items, total = [], None
    return (items if isinstance(items, list) else []), (int(total) if total is not None else None)


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
    params = {"limit": 1, "offset": 1, **_base_params(questionnaire_id)}
    if status:
        params["status"] = status
    _items, total = _interviews_request(params)
    return total


# All Survey Solutions interview statuses we count for the dashboard.
ALL_STATUSES = [
    S_CREATED, S_RESTORED, S_SUPERVISOR_ASSIGNED, S_INTERVIEWER_ASSIGNED,
    S_READY_FOR_INTERVIEW, S_REJECTED_BY_SUPERVISOR, S_SENT_TO_CAPITAL,
    S_COMPLETED, S_APPROVED_BY_SUPERVISOR, S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_HEADQUARTERS,
]
# Statuses the live feed / sample fetch prioritises (the ones managers act on).
FEED_STATUSES = [
    S_COMPLETED, S_REJECTED_BY_SUPERVISOR, S_REJECTED_BY_HEADQUARTERS,
    S_APPROVED_BY_SUPERVISOR, S_APPROVED_BY_HEADQUARTERS, S_INTERVIEWER_ASSIGNED,
]


def fetch_status_counts(questionnaire_id: Optional[str] = None) -> Dict[str, int]:
    """
    One small ``TotalCount`` request per status → ``{status: count}``. This keeps
    the headline KPIs / approval funnel exact even when the workspace has hundreds
    of thousands of interviews (we never page through them all).

    Observed quirk: HQ's ``/api/v1/interviews?status=SentToCapital`` returns the
    *whole* set (it's a transient state the filter doesn't honour), so we don't
    query it — we derive it as ``grand_total - sum(other statuses)``.
    """
    grand_total = None
    try:
        grand_total = fetch_total_count(questionnaire_id)  # unfiltered
    except Exception:
        LOG.warning("Survey dashboard: unfiltered TotalCount query failed", exc_info=True)

    counts: Dict[str, int] = {}
    for st in ALL_STATUSES:
        if st == S_SENT_TO_CAPITAL:
            continue  # derived below
        try:
            n = fetch_total_count(questionnaire_id, status=st)
            if n is not None:
                counts[st] = int(n)
        except Exception:
            LOG.warning("Survey dashboard: failed to count status=%s", st, exc_info=True)

    others = sum(counts.values())
    if grand_total is not None:
        if others > grand_total:
            LOG.warning(
                "Survey dashboard: status counts sum (%s) exceeds grand total (%s) — a status filter may be unreliable; counts=%s",
                others, grand_total, counts,
            )
        counts[S_SENT_TO_CAPITAL] = max(0, grand_total - others)
    else:
        counts[S_SENT_TO_CAPITAL] = 0
    LOG.info(
        "Survey dashboard: status counts (qid=%s) = %s (grandTotal=%s, sum=%s)",
        questionnaire_id or "ALL", counts, grand_total, sum(counts.values()),
    )
    return counts


def iter_sample_briefs(
    questionnaire_id: Optional[str] = None,
    *,
    max_interviews: int = 600,
) -> Iterable[Dict[str, Any]]:
    """
    Yield a small sample of interview briefs for the live feed / leaderboard / heatmap.

    Reality check: this Survey Solutions server's ``GET /api/v1/interviews`` **ignores
    ``limit`` and ``offset``** — it always returns the same ~10 rows for a given query
    (``TotalCount`` is still accurate, which is why the headline KPIs are exact). So
    here we just take one page per "interesting" status plus one unfiltered page,
    yielding ~50–70 distinct interviews. The on-disk cache accumulates them across
    polls (the "recent ~10" rotates over time), so the picture gradually fills in.
    """
    if max_interviews <= 0:
        return
    base = _base_params(questionnaire_id)
    queries: List[Dict[str, Any]] = [dict(base)]  # unfiltered "most recent" page
    for st in FEED_STATUSES:
        queries.append({**base, "status": st})
    yielded = 0
    for q in queries:
        try:
            items, _t = _interviews_request({"limit": 200, "offset": 1, **q})
        except Exception:
            LOG.warning("Survey dashboard: sample fetch failed for query=%s", q, exc_info=True)
            continue
        for it in items:
            if isinstance(it, dict):
                yield it
                yielded += 1
                if max_interviews and yielded >= max_interviews:
                    LOG.info("Survey dashboard: sampled %s interview brief(s) for the feed (cap)", yielded)
                    return
    LOG.info("Survey dashboard: sampled %s interview brief(s) for the feed", yielded)


# ---- status-count cache (so the GraphQL layer reads them without re-hitting HQ) ----
def _status_counts_key(questionnaire_id: Optional[str]) -> str:
    guid, _v = _split_qid(questionnaire_id)
    return f"api_etl:survey_dashboard:status_counts:{guid or 'ALL'}"


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
                titles[str(guid)] = title
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
        title = q_titles.get(str(guid))
    return {
        "interview_key": _first(brief, "Key", "InterviewKey", "interview__key") or _featured_label(brief),
        "questionnaire_id": guid,
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


def poll_interviews(
    questionnaire_id: Optional[str] = None,
    *,
    with_sample: bool = True,
    sample_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Refresh the dashboard against HQ:
      1) exact status counts via cheap ``TotalCount`` queries (always),
      2) optionally a *bounded* sample of interview briefs upserted into the
         local cache (for the live feed / leaderboard / heatmap).

    On a workspace with hundreds of thousands of interviews we never page through
    them all — the headline KPIs come from (1), and (2) is just a recent sample.
    """
    now = timezone.now()

    # 1) status counts (exact, cheap)
    status_counts = fetch_status_counts(questionnaire_id)
    total_count = sum(status_counts.values()) if status_counts else 0
    # Only overwrite the cached counts if HQ actually answered (don't blank the
    # dashboard out when HQ is unreachable — keep the last good values).
    if status_counts and total_count > 0:
        _store_status_counts(questionnaire_id, status_counts)

    seen = changed = created_rows = 0
    if with_sample:
        cap = sample_size if sample_size is not None else _to_int(_cfg("dashboard_sample_size", 600), 600)
        # Collect + de-duplicate by interview id (HQ pages can overlap / repeat).
        briefs_by_id: Dict[str, Dict[str, Any]] = {}
        for b in iter_sample_briefs(questionnaire_id, max_interviews=int(cap or 0)):
            iid = _first(b, "InterviewId", "Id", "interviewId")
            if iid:
                briefs_by_id[str(iid)] = b
        briefs = list(briefs_by_id.items())  # (iid, brief)
        q_titles = _questionnaire_title_map() if briefs else {}

        with transaction.atomic():
            existing = {
                row.interview_id: row
                for row in SurveyInterviewCache.objects.filter(interview_id__in=list(briefs_by_id.keys()))
            }
            to_create: List[SurveyInterviewCache] = []
            for iid, brief in briefs:
                fields = _brief_to_fields(brief, q_titles)
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
                    update_fields.append("updated_at")
                    row.save(update_fields=list(set(update_fields)))
            if to_create:
                SurveyInterviewCache.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)

    cache.set(_LAST_POLL_CACHE_KEY, now.isoformat(), None)
    LOG.info(
        "Survey dashboard poll (qid=%s): totalCount=%s, sampled=%s (%s new, %s status changes)",
        questionnaire_id or "ALL", total_count, seen, created_rows, changed,
    )
    return {
        "seen": seen, "created": created_rows, "changed": changed,
        "total_count": total_count, "status_counts": status_counts,
        "polled_at": now.isoformat(),
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


def last_polled_at():
    raw = cache.get(_LAST_POLL_CACHE_KEY)
    if raw:
        return _dt(raw)
    latest = SurveyInterviewCache.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    return latest


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
            g0 = str(guid).split("$", 1)[0].strip()
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


def _snapshot_status_counts(questionnaire_id: Optional[str]) -> Optional[Dict[str, int]]:
    """Last persisted live status counts (survives restarts / empty process cache)."""
    guid, _v = _split_qid(questionnaire_id)
    snap = (
        SurveyDashboardSnapshot.objects.filter(questionnaire_id=(guid or ""))
        .order_by("-snapshot_date").first()
    )
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
    guid, _v = _split_qid(questionnaire_id)

    # --- headline counts: live status counts (exact) when available, else last snapshot / cache ---
    status_counts = _resolve_status_counts(questionnaire_id)
    total = sum(status_counts.values())
    completed = status_counts.get(S_COMPLETED, 0)
    approved_sup = status_counts.get(S_APPROVED_BY_SUPERVISOR, 0)
    approved_hq = status_counts.get(S_APPROVED_BY_HEADQUARTERS, 0)
    sent_to_capital = status_counts.get(S_SENT_TO_CAPITAL, 0)
    rejected_sup = status_counts.get(S_REJECTED_BY_SUPERVISOR, 0)
    rejected_hq = status_counts.get(S_REJECTED_BY_HEADQUARTERS, 0)
    in_progress = sum(status_counts.get(s, 0) for s in IN_PROGRESS_STATUSES)
    completed_or_beyond = sum(status_counts.get(s, 0) for s in COMPLETED_OR_BEYOND)
    approved_sup_or_beyond = sum(status_counts.get(s, 0) for s in APPROVED_SUP_OR_BEYOND)
    rejected_total = rejected_sup + rejected_hq
    rejection_rate = round((rejected_total / completed_or_beyond) * 100.0, 1) if completed_or_beyond else 0.0
    target_total = _to_int(_cfg("dashboard_target_total", 0), 0) or total

    # --- sample rows (bounded) for feed-derived stats: leaderboard / heatmap / active / duration ---
    sample_rows = list(
        _qs(questionnaire_id)
        .order_by("-status_changed_at", "-updated_at")
        .values("status", "responsible_name", "supervisor_name", "last_entry_at_utc", "duration_minutes")[:row_cap]
    )
    sample_size = _qs(questionnaire_id).count()

    durations: List[float] = []
    active_enums = set()
    enum_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"completed": 0, "approvedBySupervisor": 0, "approvedByHq": 0, "rejected": 0, "total": 0,
                 "supervisorName": None, "lastActivity": None}
    )
    heat: Dict[tuple, int] = defaultdict(int)
    for r in sample_rows:
        st = r["status"] or ""
        name = (r["responsible_name"] or "").strip() or "(unassigned)"
        es = enum_stats[name]
        es["total"] += 1
        es["supervisorName"] = es["supervisorName"] or r["supervisor_name"]
        le = r["last_entry_at_utc"]
        if le and (es["lastActivity"] is None or le > es["lastActivity"]):
            es["lastActivity"] = le
        if st in COMPLETED_OR_BEYOND:
            es["completed"] += 1
        if st in APPROVED_SUP_OR_BEYOND:
            es["approvedBySupervisor"] += 1
        if st == S_APPROVED_BY_HEADQUARTERS:
            es["approvedByHq"] += 1
        if st in REJECTED_STATUSES:
            es["rejected"] += 1
        if r["duration_minutes"] is not None and st in COMPLETED_OR_BEYOND:
            durations.append(r["duration_minutes"])
        if le and (now - le) <= active_window and r["responsible_name"]:
            active_enums.add(name)
        if le:
            lo = _local(le)
            heat[(lo.weekday(), lo.hour)] += 1
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None

    # --- S-curve / daily productivity from recorded snapshots (built up over polls) ---
    start_day = (_local(now) - timedelta(days=days - 1)).date()
    all_snaps = list(
        SurveyDashboardSnapshot.objects.filter(questionnaire_id=(guid or "")).order_by("snapshot_date")
    )
    by_date = {s.snapshot_date: s.cumulative_completed for s in all_snaps}
    last_cum = 0
    for s in all_snaps:
        if s.snapshot_date < start_day:
            last_cum = s.cumulative_completed
        else:
            break
    completion_series = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        cur = by_date.get(d, last_cum)
        delta = max(0, cur - last_cum)
        completion_series.append({"date": d.isoformat(), "count": delta, "cumulative": cur})
        last_cum = cur
    daily_productivity = [dict(x) for x in completion_series]

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
    leaderboard.sort(key=lambda x: (x["completed"], x["approvedByHq"], x["total"]), reverse=True)
    leaderboard = leaderboard[:25]

    # --- heatmap (7 x 24, over the sample) ---
    heatmap = [{"dayOfWeek": dow, "hour": h, "count": heat.get((dow, h), 0)} for dow in range(7) for h in range(24)]

    try:
        questionnaire_options = list_dashboard_questionnaires()
    except Exception:
        LOG.warning("Survey dashboard: list_dashboard_questionnaires failed", exc_info=True)
        questionnaire_options = []

    polled = last_polled_at()
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
        "avgInterviewDurationMinutes": avg_duration,
        "activeEnumerators": len(active_enums),
        "targetTotal": target_total,
        "sampleSize": sample_size,
        "lastPolledAt": (polled.isoformat() if polled else None),
        "hqBaseUrl": hq_web_base(),
        "questionnaires": questionnaire_options,
        "dailyProductivity": daily_productivity,
        "completionSeries": completion_series,
        "approvalFunnel": approval_funnel,
        "enumeratorLeaderboard": leaderboard,
        "activityHeatmap": heatmap,
    }


def list_interviews(
    *,
    questionnaire_id: Optional[str] = None,
    status: Optional[str] = None,
    responsible_name: Optional[str] = None,
    search: Optional[str] = None,
    from_date: Optional[Any] = None,
    limit: int = 50,
) -> List[SurveyInterviewCache]:
    from django.db.models import Q
    qs = _qs(questionnaire_id)
    if status:
        qs = qs.filter(status=status)
    if responsible_name:
        qs = qs.filter(responsible_name__icontains=responsible_name)
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
    return list(qs.order_by("-status_changed_at", "-updated_at")[:limit])


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
    guid, _v = _split_qid(questionnaire_id)
    if not status_counts:
        status_counts = _load_status_counts(questionnaire_id) or _cache_status_counts(questionnaire_id)
    today = _local(timezone.now()).date()
    g = lambda s: int(status_counts.get(s, 0))  # noqa: E731
    completed_or_beyond = sum(g(s) for s in COMPLETED_OR_BEYOND)
    SurveyDashboardSnapshot.objects.update_or_create(
        snapshot_date=today,
        questionnaire_id=(guid or ""),
        defaults={
            "total_interviews": sum(status_counts.values()),
            "in_progress": sum(g(s) for s in IN_PROGRESS_STATUSES),
            "completed": g(S_COMPLETED),
            "approved_by_supervisor": g(S_APPROVED_BY_SUPERVISOR),
            "approved_by_hq": g(S_APPROVED_BY_HEADQUARTERS),
            "sent_to_capital": g(S_SENT_TO_CAPITAL),
            "rejected_by_supervisor": g(S_REJECTED_BY_SUPERVISOR),
            "rejected_by_hq": g(S_REJECTED_BY_HEADQUARTERS),
            "cumulative_completed": completed_or_beyond,
            "active_enumerators": _active_enumerators_count(questionnaire_id),
            "json_ext": {"status_counts": dict(status_counts)},
        },
    )


def refresh_dashboard(questionnaire_id: Optional[str] = None, *, with_sample: bool = True) -> Dict[str, Any]:
    result = poll_interviews(questionnaire_id=questionnaire_id, with_sample=with_sample)
    try:
        if result.get("total_count"):  # don't write an all-zero snapshot when HQ was unreachable
            take_daily_snapshot(questionnaire_id=questionnaire_id, status_counts=result.get("status_counts"))
    except Exception:
        LOG.exception("Survey dashboard: failed to take daily snapshot")
    return result


def _in_fail_backoff() -> bool:
    raw = cache.get(_LAST_FAIL_CACHE_KEY)
    if not raw:
        return False
    fdt = _dt(raw)
    backoff = _to_int(_cfg("dashboard_poll_fail_backoff_seconds", 300), 300) or 300
    return bool(fdt and (timezone.now() - fdt).total_seconds() < backoff)


def is_stale() -> bool:
    if _in_fail_backoff():
        return False
    interval = _to_int(_cfg("dashboard_poll_interval_seconds", 60), 60) or 60
    last = last_polled_at()
    if last is None:
        return True
    return (timezone.now() - last).total_seconds() > interval


def _background_self_heal(questionnaire_id: Optional[str]) -> None:
    """Run a lightweight refresh (status counts only) in a daemon thread so the
    dashboard request returns immediately. Used only when there is no Celery broker."""
    from django.db import connection
    try:
        refresh_dashboard(questionnaire_id=questionnaire_id, with_sample=False)
        cache.set(_LAST_POLL_CACHE_KEY, timezone.now().isoformat(), None)
        cache.delete(_LAST_FAIL_CACHE_KEY)
    except Exception:
        LOG.warning("Survey dashboard: background self-heal poll failed (HQ unreachable?) — backing off", exc_info=True)
        cache.set(_LAST_FAIL_CACHE_KEY, timezone.now().isoformat(), None)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def maybe_async_refresh(questionnaire_id: Optional[str] = None) -> bool:
    """
    Self-heal: if the dashboard cache is stale *or* we have never fetched the
    status counts for the requested questionnaire/PAA, refresh it *without blocking
    the request* — enqueue a Celery poll if a broker is available, otherwise run a
    lightweight status-count refresh in a daemon thread. The interview sample
    (feed / leaderboard / heatmap) is refreshed by the Celery task or the manual
    "Sync now" mutation, not on every page load. Per-target lock + fail back-off
    keep an unreachable HQ quiet.
    """
    if not _cfg("dashboard_enabled", True):
        return False
    if _in_fail_backoff():
        return False
    now = timezone.now()
    interval = _to_int(_cfg("dashboard_poll_interval_seconds", 60), 60) or 60
    last = last_polled_at()
    global_stale = last is None or (now - last).total_seconds() > interval
    need_qid_counts = bool(questionnaire_id) and _load_status_counts(questionnaire_id) is None
    if not global_stale and not need_qid_counts:
        return False
    # per-target short-lived lock so concurrent loads don't pile up polls
    lock_key = f"api_etl:survey_dashboard:polling:{questionnaire_id or 'ALL'}"
    if cache.get(lock_key):
        return False
    cache.set(lock_key, now.isoformat(), 90)
    if global_stale and not questionnaire_id:
        cache.set(_LAST_POLL_CACHE_KEY, now.isoformat(), None)  # keep the "last synced" marker fresh
    try:
        from api_etl.tasks import poll_survey_dashboard_task

        poll_survey_dashboard_task.delay(questionnaire_id)
        return True
    except Exception:
        LOG.info("Survey dashboard: no Celery broker — running self-heal poll in a background thread")
        import threading

        threading.Thread(target=_background_self_heal, args=(questionnaire_id,), daemon=True).start()
        return True
