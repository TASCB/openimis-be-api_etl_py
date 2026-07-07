"""Survey Solutions HQ client: REST/GraphQL primitives + capability probe.
See docs/SURVEY_DASHBOARD_DEVELOPER_GUIDE.md (openimis-dist_dkr) for design notes."""

import logging
from typing import Any, Dict, List, Optional

import requests
from django.core.cache import cache
from django.utils import timezone

from api_etl.apps import ApiEtlConfig as C

LOG = logging.getLogger(__name__)

_CAPS_CACHE_KEY = "api_etl:hq_client:capabilities"


def _cfg(name: str, default=None):
    return getattr(C, name, default)


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def timeout():
    connect = _to_int(_cfg("dashboard_hq_connect_timeout", 5), 5) or 5
    read = _to_int(_cfg("dashboard_hq_read_timeout", 60), 60) or 60
    return (connect, read)


def base_url() -> str:
    return (str(_cfg("export_base_url") or _cfg("base_url") or "")).strip().rstrip("/")


def workspace() -> str:
    return (str(_cfg("export_workspace") or _cfg("workspace") or "")).strip().strip("/")


def endpoint_base() -> str:
    """``{base}/{workspace}{meta_api_prefix}`` — the workspace-scoped REST base."""
    base = base_url()
    if not base:
        ep = _cfg("export_endpoint_base")
        return str(ep).rstrip("/") if ep else ""
    ws = workspace()
    prefix = str(_cfg("meta_api_prefix", "/api/v1") or "/api/v1").strip()
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    if ws and not base.endswith("/" + ws):
        base = f"{base}/{ws}"
    return f"{base}{prefix}"


def web_base() -> str:
    """HQ web base for deep links (interview review pages)."""
    base = base_url()
    if not base:
        return ""
    ws = workspace()
    return f"{base}/{ws}" if ws else base


def request_kwargs() -> Dict[str, Any]:
    auth_type = str(_cfg("auth_type", "basic") or "basic").lower()
    if auth_type == "basic":
        username = _cfg("auth_basic_username")
        if username:
            return {"auth": (username, _cfg("auth_basic_password"))}
    elif auth_type == "bearer":
        token = _cfg("auth_bearer_token")
        if token:
            return {"headers": {"Authorization": f"Bearer {token}"}}
    return {}


def _prepared_kwargs() -> (Dict[str, str], Dict[str, Any]):
    rkwargs = request_kwargs()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(rkwargs.pop("headers", {}))
    return headers, rkwargs


def json_get(path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
    base = endpoint_base()
    if not base:
        raise ValueError("Survey Solutions HQ base URL is not configured (export_base_url).")
    headers, rkwargs = _prepared_kwargs()
    r = requests.get(f"{base}/{path.lstrip('/')}", params=params or None,
                     headers=headers, timeout=timeout(), **rkwargs)
    r.raise_for_status()
    return r.json()


def graphql_post(query: str) -> Dict[str, Any]:
    base = base_url()
    if not base:
        raise ValueError("Survey Solutions HQ base URL is not configured (export_base_url).")
    headers, rkwargs = _prepared_kwargs()
    r = requests.post(f"{base}/graphql", json={"query": query},
                      headers=headers, timeout=timeout(), **rkwargs)
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"HQ GraphQL error: {str(payload['errors'][0].get('message', ''))[:300]}")
    return payload.get("data") or {}


def _ws_arg() -> str:
    ws = workspace()
    return f'workspace: "{ws}", ' if ws else ""


def probe_capabilities(force: bool = False) -> Dict[str, Any]:
    """Cached probe of what the deployed HQ supports (graphql, aliasing, enum casing)."""
    if not force:
        cached = cache.get(_CAPS_CACHE_KEY)
        if isinstance(cached, dict):
            return cached
    caps: Dict[str, Any] = {"graphql": False, "graphql_alias": False, "status_enum": {},
                            "probed_at": timezone.now().isoformat()}
    try:
        graphql_post("{ interviews(" + _ws_arg() + "take: 1) { totalCount } }")
        caps["graphql"] = True
    except Exception:
        LOG.info("HQ capability probe: GraphQL interviews query unavailable", exc_info=True)
    if caps["graphql"]:
        try:
            data = graphql_post('{ __type(name: "InterviewStatus") { enumValues { name } } }')
            values = ((data.get("__type") or {}).get("enumValues")) or []
            caps["status_enum"] = {
                str(v["name"]).replace("_", "").upper(): str(v["name"])
                for v in values if isinstance(v, dict) and v.get("name")
            }
        except Exception:
            LOG.debug("HQ capability probe: enum introspection failed", exc_info=True)
        try:
            graphql_post(
                "{ a: interviews(" + _ws_arg() + "take: 1) { totalCount } "
                "b: interviews(" + _ws_arg() + "take: 1) { totalCount } }"
            )
            caps["graphql_alias"] = True
        except Exception:
            LOG.info("HQ capability probe: GraphQL aliased documents unsupported (known on 22.02.5)")
    ttl = 86400 if caps["graphql"] else 3600  # re-probe sooner while unavailable
    cache.set(_CAPS_CACHE_KEY, caps, ttl)
    return caps


def status_literal(status: str, caps: Optional[Dict[str, Any]] = None) -> str:
    caps = caps or probe_capabilities()
    return (caps.get("status_enum") or {}).get(status.replace("_", "").upper()) or status.upper()


def graphql_filtered_count(where_parts: List[str]) -> Optional[int]:
    parts = _ws_arg() + "take: 1"
    if where_parts:
        parts += ", where: {" + ", ".join(where_parts) + "}"
    data = graphql_post("{ interviews(" + parts + ") { filteredCount } }")
    count = (data.get("interviews") or {}).get("filteredCount")
    return int(count) if count is not None else None


def graphql_status_counts(base_where: List[str], statuses: List[str]) -> Dict[str, int]:
    """{canonical status: filteredCount} for the scope; raises if GraphQL is unusable."""
    caps = probe_capabilities()
    if not caps.get("graphql"):
        raise RuntimeError("HQ GraphQL interviews query unavailable")
    counts: Dict[str, int] = {}
    if caps.get("graphql_alias"):
        fields = []
        for i, st in enumerate(statuses):
            where = base_where + [f"status: {{eq: {status_literal(st, caps)}}}"]
            fields.append(f"s{i}: interviews({_ws_arg()}take: 1, where: {{{', '.join(where)}}}) {{ filteredCount }}")
        data = graphql_post("{ " + " ".join(fields) + " }")
        for i, st in enumerate(statuses):
            count = (data.get(f"s{i}") or {}).get("filteredCount")
            if count is not None:
                counts[st] = int(count)
        return counts
    for st in statuses:
        where = base_where + [f"status: {{eq: {status_literal(st, caps)}}}"]
        count = graphql_filtered_count(where)
        if count is not None:
            counts[st] = int(count)
    return counts
