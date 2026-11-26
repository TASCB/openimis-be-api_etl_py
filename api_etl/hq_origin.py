import hashlib

from api_etl.apps import ApiEtlConfig

HQ_URL_MAX_LENGTH = 255


def current_hq_url() -> str:
    base = (getattr(ApiEtlConfig, "export_base_url", "") or "").strip().rstrip("/").lower()
    workspace = (getattr(ApiEtlConfig, "export_workspace", "") or "").strip().strip("/").lower()
    url = f"{base}/{workspace}" if base and workspace else base
    return url[:HQ_URL_MAX_LENGTH]


def current_hq_tag() -> str:
    return hashlib.sha1(current_hq_url().encode("utf-8")).hexdigest()[:12]
