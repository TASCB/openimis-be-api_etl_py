from __future__ import annotations

import os
import re
import time
import logging
import zipfile
from typing import Dict, Iterable, Iterator, List, Optional, Union

import requests

from api_etl.apps import ApiEtlConfig as C
from api_etl.sources.base import DataSource
from api_etl.utils import (
    iter_tab_files,
    to_datetime_str,
    get_timestamped_batch_identifier,
)

LOG = logging.getLogger(__name__)

_JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_QID_RE = re.compile(
    r"^[0-9a-fA-F-]{32,36}\$\d+$"
)  # GUID (with or without dashes) + $version


# ----------------------------- helpers -----------------------------
# --- helpers for tab filtering ---
def _as_list(val):
    if not val:
        return []
    if isinstance(val, (list, tuple, set)):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        # allow comma-separated string in ModuleConfiguration
        return [s.strip() for s in val.split(",") if s.strip()]
    return []


def _should_exclude_tab(tab_basename: str) -> bool:
    """
    tab_basename is the file name without extension, e.g. 'hhroster' or 'interview__actions'.
    Returns True if the tab should be skipped based on ModuleConfiguration.
    """
    try:
        from api_etl.apps import ApiEtlConfig as C

        patterns = _as_list(getattr(C, "export_exclude_tabs_contains", []))
    except Exception:
        patterns = []
    name = (tab_basename or "").lower()
    return any(p.lower() in name for p in patterns)


def _compose_endpoint_base(
    *,
    base_url: Optional[str],
    workspace: Optional[str],
    api_prefix: Optional[str],
) -> str:
    """
    Build: {base}/{workspace}{api_prefix}
    - If base_url is missing, try C.export_endpoint_base (already fully-formed).
    - Defaults api_prefix to /api/v2 (export) when not provided.
    """
    if not base_url:
        ep = getattr(C, "export_endpoint_base", None)
        if ep:
            return ep.rstrip("/")

    base = (base_url or getattr(C, "export_base_url", "") or "").rstrip("/")
    if not base:
        raise ValueError(
            "Missing base_url. Provide per call or set EXPORT_BASE_URL in ModuleConfiguration."
        )

    ws = (workspace or getattr(C, "export_workspace", "") or "").strip().strip("/")
    prefix = (
        api_prefix or getattr(C, "export_api_prefix", "/api/v2") or "/api/v2"
    ).strip()
    if not prefix.startswith("/"):
        prefix = "/" + prefix

    if ws:
        return f"{base}/{ws}{prefix}"
    return f"{base}{prefix}"


def _merge_auth(
    *,
    auth_type: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    bearer: Optional[str] = None,
) -> Dict:
    at = (auth_type or getattr(C, "auth_type", "basic") or "basic").lower()
    if at == "bearer":
        token = bearer if bearer is not None else getattr(C, "auth_bearer_token", "")
        if not token:
            raise ValueError("Bearer token required but missing.")
        return {"headers": {"Authorization": f"Bearer {token}"}}

    if at == "basic":
        user = (
            username
            if username is not None
            else getattr(C, "auth_basic_username", None)
        )
        pwd = (
            password
            if password is not None
            else getattr(C, "auth_basic_password", None)
        )
        if not user or pwd is None:
            raise ValueError("Basic auth requires username and password.")
        return {"auth": (user, pwd)}

    return {}


class SurveySolutionsExportSource(DataSource):
    """
    Source that pulls Survey Solutions data via the HQ Export API.

    Supports two prefixes:
      - meta_api_prefix: used for metadata/listing (e.g., /api/v1/questionnaires)
      - api_prefix:      used for export lifecycle (e.g., /api/v2/export)
    """

    base_url: str = ""
    workspace: str = ""
    api_prefix: str = "/api/v2"
    meta_api_prefix: str = "/api/v1"

    # ------------------- list available questionnaires -------------------
    def list_questionnaires(
        self,
        *,
        base_url: Optional[str] = None,
        workspace: Optional[str] = None,
        api_prefix: Optional[str] = None,
        meta_api_prefix: Optional[str] = None,
        auth_type: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        bearer: Optional[str] = None,
    ) -> List[Dict]:
        """
        Fetch list of available questionnaires from HQ.

        Returns a list of dicts like:
          [{"Identity": "GUID$version", "Id": "GUID", "Title": "...", "Version": 1}, ...]
        """
        prefix = (
            meta_api_prefix
            or getattr(self, "meta_api_prefix", None)
            or getattr(C, "meta_api_prefix", None)
            or api_prefix
            or getattr(self, "api_prefix", None)
            or getattr(C, "export_api_prefix", None)
            or "/api/v1"
        )

        endpoint_base = _compose_endpoint_base(
            base_url=base_url
            or getattr(self, "base_url", None)
            or getattr(C, "export_base_url", None),
            workspace=workspace
            or getattr(self, "workspace", None)
            or getattr(C, "export_workspace", None),
            api_prefix=prefix,
        )

        url = f"{endpoint_base}/questionnaires"
        rkwargs = _merge_auth(
            auth_type=auth_type,
            username=username,
            password=password,
            bearer=bearer,
        )

        headers = dict(_JSON_HEADERS)
        if "headers" in rkwargs:
            headers.update(rkwargs["headers"])

        # ---- FIX: paginate the questionnaires list ----
        limit = int(getattr(C, "questionnaires_page_size", 200) or 200)
        offset = 0
        all_rows: List[Dict] = []
        total_count = None

        LOG.info(
            "Fetching questionnaires list from %s (limit=%s, offset=%s)",
            url,
            limit,
            offset,
        )

        while True:
            r = requests.get(
                url,
                params={"limit": limit, "offset": offset},
                headers=headers,
                timeout=30,
                **{k: v for k, v in rkwargs.items() if k != "headers"},
            )
            r.raise_for_status()

            try:
                payload = r.json()
            except Exception:
                LOG.error("Failed to parse JSON: %s", (r.text or "")[:500])
                break

            # Extract items + optional TotalCount safely
            if isinstance(payload, dict):
                if "Items" in payload:
                    page_items = payload.get("Items") or []
                    total_count = payload.get("TotalCount", total_count)
                elif "Questionnaires" in payload:
                    page_items = payload.get("Questionnaires") or []
                    total_count = payload.get("TotalCount", total_count)
                elif "questionnaires" in payload:
                    page_items = payload.get("questionnaires") or []
                    total_count = payload.get("TotalCount", total_count)
                else:
                    LOG.warning(
                        "Unexpected questionnaires dict format keys=%s",
                        list(payload.keys())[:30],
                    )
                    page_items = []
            elif isinstance(payload, list):
                page_items = payload
            else:
                LOG.warning("Expected dict/list, got %s", type(payload))
                page_items = []

            if not isinstance(page_items, list) or len(page_items) == 0:
                break

            all_rows.extend([x for x in page_items if isinstance(x, dict)])

            # Stop conditions
            if total_count is not None and len(all_rows) >= int(total_count):
                break
            if len(page_items) < limit:
                break

            offset += limit

        data = all_rows
        LOG.info(
            "Fetched %d questionnaires from HQ list endpoint (totalCount=%s)",
            len(data),
            total_count,
        )

        # Step 1: Get unique questionnaire GUIDs
        unique_guids = set()
        for q in data:
            guid = q.get("QuestionnaireId") or q.get("Id")
            if guid:
                unique_guids.add(guid)

        LOG.info(
            "Found %d unique questionnaire GUIDs, fetching all versions...",
            len(unique_guids),
        )

        # Step 2: For each GUID, fetch all versions
        all_questionnaires: List[Dict] = []

        for guid in unique_guids:
            try:
                version_url = f"{endpoint_base}/questionnaires/{guid}"
                LOG.debug("Fetching versions from: %s", version_url)

                r_versions = requests.get(
                    version_url,
                    headers=headers,
                    timeout=30,
                    **{k: v for k, v in rkwargs.items() if k != "headers"},
                )
                r_versions.raise_for_status()

                versions_data = r_versions.json()

                versions_list = []
                if isinstance(versions_data, dict):
                    if "Questionnaires" in versions_data:
                        versions_list = versions_data["Questionnaires"]
                    elif "questionnaires" in versions_data:
                        versions_list = versions_data["questionnaires"]
                    elif "Items" in versions_data:
                        versions_list = versions_data["Items"]
                elif isinstance(versions_data, list):
                    versions_list = versions_data

                for qv in versions_list:
                    if not isinstance(qv, dict):
                        continue
                    all_questionnaires.append(
                        {
                            "Identity": qv.get("QuestionnaireIdentity")
                            or f"{qv.get('Id')}${qv.get('Version')}",
                            "Id": qv.get("QuestionnaireId") or qv.get("Id"),
                            "Title": qv.get("Title"),
                            "Version": qv.get("Version"),
                            "Variable": qv.get("Variable"),
                            "LastEntryDate": qv.get("LastEntryDate"),
                        }
                    )

            except Exception as e:
                LOG.warning(
                    "Failed to fetch versions for questionnaire %s: %s", guid, e
                )
                # Fallback: add first matching item from 'data'
                for q0 in data:
                    q_guid = q0.get("QuestionnaireId") or q0.get("Id")
                    if q_guid == guid:
                        all_questionnaires.append(
                            {
                                "Identity": q0.get("QuestionnaireIdentity")
                                or f"{q0.get('Id')}${q0.get('Version')}",
                                "Id": q0.get("QuestionnaireId") or q0.get("Id"),
                                "Title": q0.get("Title"),
                                "Version": q0.get("Version"),
                                "Variable": q0.get("Variable"),
                                "LastEntryDate": q0.get("LastEntryDate"),
                            }
                        )
                        break

        LOG.info("Fetched total of %d questionnaire versions", len(all_questionnaires))
        return all_questionnaires

    # ------------------- low-level Export API calls -------------------

    def _list_jobs(
        self, endpoint_base: str, rkwargs: Dict, *, limit: int = 50, offset: int = 0
    ) -> list:
        url = f"{endpoint_base}/export?limit={limit}&offset={offset}"
        headers = dict(_JSON_HEADERS)
        if "headers" in rkwargs:
            headers.update(rkwargs["headers"])
        r = requests.get(
            url,
            headers=headers,
            timeout=30,
            **{k: v for k, v in rkwargs.items() if k != "headers"},
        )
        if r.status_code >= 400:
            LOG.error(
                "Export GET list failed %s %s\nURL=%s\nResp=%s",
                r.status_code,
                r.reason,
                url,
                r.text[:2000],
            )
            r.raise_for_status()
        try:
            return r.json() or []
        except Exception:
            return []

    def _find_latest_completed_job(
        self,
        endpoint_base: str,
        questionnaire_id: str,
        export_format: Optional[str],
        interview_status: Optional[str],
        rkwargs: Dict,
        *,
        pages: int = 3,
        page_size: int = 50,
    ) -> Optional[int]:
        """
        Scan recent export jobs and return the latest Completed job_id for this questionnaire.
        """
        fmt = export_format or getattr(C, "export_format", "Tabular") or "Tabular"
        ist = interview_status or getattr(C, "export_interview_status", "All") or "All"
        best = None
        best_complete_date = ""

        for p in range(pages):
            jobs = self._list_jobs(
                endpoint_base, rkwargs, limit=page_size, offset=p * page_size
            )
            if not jobs:
                break
            for j in jobs:
                try:
                    qid = j.get("QuestionnaireId") or j.get("questionnaireId")
                    status = (
                        j.get("ExportStatus") or j.get("exportStatus") or ""
                    ).lower()
                    has_file = bool(j.get("HasExportFile") or j.get("hasExportFile"))
                    efmt = j.get("ExportType") or j.get("exportType") or ""
                    istat = j.get("InterviewStatus") or j.get("interviewStatus") or ""
                    cdate = (
                        j.get("CompleteDate")
                        or j.get("completeDate")
                        or j.get("StartDate")
                        or ""
                    )
                except Exception:
                    continue

                if qid != questionnaire_id:
                    continue
                if status != "completed" or not has_file:
                    continue
                if efmt and efmt != fmt:
                    continue
                if istat and istat != ist:
                    continue

                if cdate and cdate > best_complete_date:
                    best_complete_date = cdate
                    best = int(j.get("JobId") or j.get("jobId"))

        if best:
            LOG.warning(
                "Reusing latest Completed export job %s for qid=%s (fmt=%s, status=%s).",
                best,
                questionnaire_id,
                fmt,
                ist,
            )
        return best

    def _create_job(
        self,
        *,
        endpoint_base: str,
        questionnaire_id: str,
        export_format: str,
        interview_status: str,
        include_meta: Optional[bool],
        from_dt: Optional[str],
        to_dt: Optional[str],
        rkwargs: Dict,
    ) -> int:
        if not _QID_RE.match(questionnaire_id):
            raise ValueError(
                f"QuestionnaireId must be 'GUID$version'. Got: {questionnaire_id!r}"
            )

        body: Dict[str, object] = {
            "QuestionnaireId": questionnaire_id,
            "ExportType": export_format or "Tabular",
            "InterviewStatus": interview_status or "All",
        }
        if include_meta is not None:
            body["IncludeMeta"] = bool(include_meta)
        if from_dt:
            body["From"] = from_dt
        if to_dt:
            body["To"] = to_dt

        url = f"{endpoint_base}/export"
        headers = dict(_JSON_HEADERS)
        if "headers" in rkwargs:
            headers.update(rkwargs["headers"])
            rkwargs = {k: v for k, v in rkwargs.items() if k != "headers"}

        # Retry on 5xx/network; do not retry on 4xx
        attempts = int(getattr(C, "export_post_retries", 2) or 2) + 1
        backoff = int(getattr(C, "export_post_backoff_seconds", 2) or 2)

        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                r = requests.post(
                    url, json=body, headers=headers, timeout=60, **rkwargs
                )

                # ---- Server error: retryable ----
                if 500 <= r.status_code < 600:
                    LOG.error(
                        "Export POST failed %s %s\nURL=%s\nBody=%s\nResp=%s",
                        r.status_code,
                        r.reason,
                        url,
                        body,
                        (r.text or "")[:2000],
                    )
                    last_exc = requests.HTTPError(
                        f"{r.status_code} {r.reason}", response=r
                    )

                # ---- Success ----
                elif 200 <= r.status_code < 300:
                    data = r.json() or {}
                    job_id = int(data.get("JobId") or data.get("jobId"))
                    LOG.info(
                        "Export job created: %s (qid=%s)", job_id, questionnaire_id
                    )
                    return job_id

                # ---- Client/other error: do not retry by default ----
                else:
                    # Log full context BEFORE raising, so we can see HQ's message.
                    LOG.error(
                        "Export POST error %s %s\nURL=%s\nBody=%s\nResp=%s",
                        r.status_code,
                        r.reason,
                        url,
                        body,
                        (r.text or "")[:2000],
                    )

                    # Optional fallback: reuse latest Completed job for selected 4xx statuses
                    if r.status_code in (400, 403, 404) and getattr(
                        C, "export_reuse_latest_on_4xx", False
                    ):
                        LOG.warning(
                            "Attempting fallback to latest Completed export (status=%s).",
                            r.status_code,
                        )
                        jid = self._find_latest_completed_job(
                            endpoint_base=endpoint_base,
                            questionnaire_id=questionnaire_id,
                            export_format=export_format,
                            interview_status=interview_status,
                            rkwargs=rkwargs,
                            pages=int(getattr(C, "export_list_scan_pages", 3) or 3),
                            page_size=int(
                                getattr(C, "export_list_page_size", 50) or 50
                            ),
                        )
                        if jid:
                            LOG.info("Fallback: reusing Completed export job: %s", jid)
                            return jid

                    # Raise into the HTTPError handler (keeps original behavior)
                    r.raise_for_status()

            except requests.HTTPError as e:
                # 4xx: client error — do not retry (unchanged behavior)
                if e.response is not None and 400 <= e.response.status_code < 500:
                    LOG.error(
                        "Export POST 4xx, not retrying. URL=%s Body=%s Resp=%s",
                        url,
                        body,
                        (e.response.text or "")[:2000],
                    )
                    raise
                last_exc = e
                LOG.warning(
                    "Export POST HTTPError (attempt %s/%s): %s", i + 1, attempts, e
                )

            except Exception as e:
                # Network/other errors: retry
                last_exc = e
                LOG.warning(
                    "Export POST network error (attempt %s/%s): %s", i + 1, attempts, e
                )

            if i < attempts - 1:
                time.sleep(backoff)

        # Fallback: reuse the latest Completed job for this qid if configured (5xx path)
        if getattr(C, "export_reuse_latest_on_5xx", True):
            jid = self._find_latest_completed_job(
                endpoint_base=endpoint_base,
                questionnaire_id=questionnaire_id,
                export_format=export_format,
                interview_status=interview_status,
                rkwargs=rkwargs,
                pages=int(getattr(C, "export_list_scan_pages", 3) or 3),
                page_size=int(getattr(C, "export_list_page_size", 50) or 50),
            )
            if jid:
                return jid

        if last_exc:
            raise last_exc
        raise RuntimeError("Export POST failed and no fallback job found.")

    def _poll_until_complete(
        self, endpoint_base: str, job_id: int, rkwargs: Dict
    ) -> None:
        url = f"{endpoint_base}/export/{job_id}"
        deadline = time.time() + int(getattr(C, "export_timeout_seconds", 900) or 900)
        interval = int(getattr(C, "export_poll_interval_seconds", 3) or 3)

        headers = dict(_JSON_HEADERS)
        if "headers" in rkwargs:
            headers.update(rkwargs["headers"])

        while time.time() < deadline:
            r = requests.get(
                url,
                headers=headers,
                timeout=30,
                **{k: v for k, v in rkwargs.items() if k != "headers"},
            )
            if r.status_code >= 400:
                LOG.error(
                    "Export GET status failed %s %s\nURL=%s\nResp=%s",
                    r.status_code,
                    r.reason,
                    url,
                    r.text[:2000],
                )
                r.raise_for_status()
            meta = r.json() or {}
            status = (meta.get("ExportStatus") or meta.get("status") or "").lower()
            if status == "completed":
                LOG.info("Export job %s completed.", job_id)
                return
            err = meta.get("Error") or meta.get("error")
            if err:
                raise RuntimeError(f"Export job {job_id} failed: {err}")
            time.sleep(interval)

        raise TimeoutError(
            f"Export job {job_id} timed out after {getattr(C, 'export_timeout_seconds', 900)}s"
        )

    def _download_zip(self, endpoint_base: str, job_id: int, rkwargs: Dict) -> str:
        url = f"{endpoint_base}/export/{job_id}/file"
        ts = get_timestamped_batch_identifier(prefix=f"export_{job_id}_")
        out_dir = getattr(C, "export_tmp_dir", "/tmp/ss_exports") or "/tmp/ss_exports"
        path = os.path.join(out_dir, f"{ts}.zip")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        r = requests.get(url, stream=True, allow_redirects=True, timeout=300, **rkwargs)
        if r.status_code >= 400:
            text = ""
            try:
                text = r.text[:2000]
            except Exception:
                pass
            LOG.error(
                "Export file GET failed %s %s\nURL=%s\nResp=%s",
                r.status_code,
                r.reason,
                url,
                text,
            )
            r.raise_for_status()

        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

        LOG.info("Downloaded export ZIP to %s", path)
        return path

    # -------------------- iterators (single / multi qid) --------------------

    def _iter_single_qid(
        self,
        *,
        endpoint_base: str,
        questionnaire_id: str,
        tab_name_contains: Optional[str],
        export_format: Optional[str],
        interview_status: Optional[str],
        include_meta: Optional[bool],
        from_dt: Optional[str],
        to_dt: Optional[str],
        rkwargs: Dict,
        yield_source_meta: bool,
        workspace: Optional[str],
    ) -> Iterator[Dict]:
        job_id = self._create_job(
            endpoint_base=endpoint_base,
            questionnaire_id=questionnaire_id,
            export_format=export_format
            or getattr(C, "export_format", "Tabular")
            or "Tabular",
            interview_status=interview_status
            or getattr(C, "export_interview_status", "All")
            or "All",
            include_meta=(
                include_meta
                if include_meta is not None
                else getattr(C, "export_include_meta", None)
            ),
            from_dt=from_dt,
            to_dt=to_dt,
            rkwargs=rkwargs,
        )
        # Poll only if we actually created a fresh job; if we reused a completed job,
        # polling will immediately return completed anyway.
        self._poll_until_complete(endpoint_base, job_id, rkwargs)
        zip_path = self._download_zip(endpoint_base, job_id, rkwargs)

        # Resolve include filter (from CLI or config)
        name_filter = (
            tab_name_contains or getattr(C, "export_tab_name_contains", "") or ""
        ).strip() or None

        # Build the list of .tab basenames that pass include AND do not match exclude list
        selected_tabs: List[str] = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    nm = info.filename
                    if not nm.lower().endswith(".tab"):
                        continue
                    base = os.path.splitext(os.path.basename(nm))[0]  # e.g. 'hhroster'
                    if name_filter and name_filter.lower() not in base.lower():
                        continue
                    if _should_exclude_tab(base):
                        LOG.info(
                            "Skipping tab due to export_exclude_tabs_contains: %s", nm
                        )
                        continue
                    selected_tabs.append(base)
        except Exception:
            LOG.exception(
                "Failed to inspect export ZIP for tab selection; falling back to name_filter only."
            )
            selected_tabs = []

        try:
            if selected_tabs:
                # Iterate each chosen tab explicitly; this preserves your iter_tab_files helper
                # and lets us attach per-tab metadata.
                for base in selected_tabs:
                    for row in iter_tab_files(zip_path, base):
                        if yield_source_meta:
                            row = dict(row)
                            row["_source"] = {
                                "workspace": workspace
                                or getattr(C, "export_workspace", "")
                                or "",
                                "questionnaire_id": questionnaire_id,
                                "job_id": job_id,
                                "endpoint": endpoint_base,
                                "tab_filter": name_filter or "",
                                "tab_name": base,  # NEW: actual tab processed
                            }
                        yield row
            else:
                # Fallback to original behaviour (single pass with include filter only)
                for row in iter_tab_files(zip_path, name_filter):
                    if yield_source_meta:
                        row = dict(row)
                        row["_source"] = {
                            "workspace": workspace
                            or getattr(C, "export_workspace", "")
                            or "",
                            "questionnaire_id": questionnaire_id,
                            "job_id": job_id,
                            "endpoint": endpoint_base,
                            "tab_filter": name_filter or "",
                        }
                    yield row
        finally:
            if not bool(getattr(C, "export_keep_zip", False)):
                try:
                    os.remove(zip_path)
                except Exception:
                    LOG.debug("Cleanup: could not remove %s", zip_path, exc_info=True)

    # -------------------- public API expected by framework --------------------

    def rows(
        self,
        questionnaire_id: Optional[str] = None,
        questionnaire_ids: Optional[List[str]] = None,
        *,
        # workspace / endpoint
        workspace: Optional[str] = None,
        base_url: Optional[str] = None,
        api_prefix: Optional[str] = None,
        # auth overrides
        auth_type: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        bearer: Optional[str] = None,
        # export options
        export_format: Optional[str] = None,
        interview_status: Optional[str] = None,
        include_meta: Optional[bool] = None,
        from_dt: Optional[Union[str, "datetime.datetime", "datetime.date"]] = None,
        to_dt: Optional[Union[str, "datetime.datetime", "datetime.date"]] = None,
        tab_name_contains: Optional[str] = None,
        # extras
        yield_source_meta: bool = False,
    ) -> Iterable[Dict]:
        """
        Stream rows from one or multiple questionnaires.

        - If 'questionnaire_ids' provided, iterate them; else use 'questionnaire_id' or config default.
        - You can override workspace/base_url/auth per call; otherwise it uses ModuleConfiguration defaults.
        - 'from_dt'/'to_dt' accept str/datetime/date (normalized via to_datetime_str()).
        """
        endpoint_base = _compose_endpoint_base(
            base_url=base_url
            or getattr(self, "base_url", None)
            or getattr(C, "export_base_url", None),
            workspace=workspace
            or getattr(self, "workspace", None)
            or getattr(C, "export_workspace", None),
            api_prefix=(
                api_prefix
                or getattr(self, "api_prefix", None)
                or getattr(C, "export_api_prefix", None)
                or "/api/v2"
            ),
        )
        rkwargs = _merge_auth(
            auth_type=auth_type,
            username=username,
            password=password,
            bearer=bearer,
        )

        fdt = to_datetime_str(from_dt) if from_dt else None
        tdt = to_datetime_str(to_dt) if to_dt else None

        # Determine which QIDs to process
        qids: List[str] = []
        if questionnaire_ids:
            qids = list(questionnaire_ids)
        else:
            qid = questionnaire_id or (getattr(C, "questionnaire_id", "") or "")
            if qid:
                qids = [qid]

        if not qids:
            raise ValueError(
                "At least one questionnaire id is required ('questionnaire_id' or 'questionnaire_ids')."
            )

        for q in qids:
            yield from self._iter_single_qid(
                endpoint_base=endpoint_base,
                questionnaire_id=q,
                tab_name_contains=tab_name_contains,
                export_format=export_format,
                interview_status=interview_status,
                include_meta=include_meta,
                from_dt=fdt,
                to_dt=tdt,
                rkwargs=rkwargs,
                yield_source_meta=yield_source_meta,
                workspace=workspace,
            )

    # Framework compatibility
    def pull(self, *args, **kwargs):
        """
        Accepts same kwargs as 'rows'. For positional convenience:
          - args[0] can be questionnaire_id (if provided).
        """
        if (
            args
            and "questionnaire_id" not in kwargs
            and "questionnaire_ids" not in kwargs
        ):
            kwargs["questionnaire_id"] = args[0]
        return self.rows(**kwargs)
