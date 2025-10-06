from __future__ import annotations

import uuid as _uuid
import json
import logging
from typing import List, Dict

from django.apps import apps

from individual.services import WorkflowHandler, IndividualImportService
from api_etl.utils import data_to_file  # builds an InMemoryUploadedFile (CSV)
from api_etl.workflows.pmt import enrich_rows_with_pmt
from api_etl.apps import ApiEtlConfig as C  

LOG = logging.getLogger(__name__)


def _pad_codes(field_name: str, value: str) -> str:
    """
    Zero-pad configured code-like fields according to ApiEtlConfig.sink_field_pad,
    but only if the value is numeric.
    """
    pads = getattr(C, "sink_field_pad", {}) or {}
    s = ("" if value is None else str(value)).strip()
    if field_name in pads and s.isdigit():
        return s.zfill(int(pads[field_name]))
    return s


def _project_for_sink(rows: list[dict]) -> list[dict]:
    """
    Prepare rows for the importer:
      - The importer stores the entire row in Json_ext already).
      - Ensure PMT fields are top-level columns so they land in Json_ext naturally.
      - Zero-pad configured code fields (e.g., location_code) using config sink_field_pad.
      - Use ApiEtlConfig.sink_csv_fields if provided; otherwise apply a safe default.
    """
    allowed = list(getattr(C, "sink_csv_fields", []) or [])
    if not allowed:
        # safe minimal default, WITHOUT 'json_ext'
        allowed = [
            "first_name", "last_name", "dob", "gender",
            "location_name", "location_code",
            "group_code", "individual_role",
            "external_id", "interview_key",
            # intentionally no 'json_ext'
        ]

    # Build header (start with allowed; then add PMT if present anywhere)
    header = []
    seen = set()
    for col in allowed:
        if col == "json_ext":
            continue
        if col not in seen:
            seen.add(col)
            header.append(col)

    maybe_pmt = ("pmt_score", "pmt_class")
    if any(
        (k in r) or (isinstance(r.get("json_ext"), dict) and k in (r["json_ext"] or {}))
        for r in rows for k in maybe_pmt
    ):
        for k in maybe_pmt:
            if k not in seen:
                seen.add(k)
                header.append(k)

    out: list[dict] = []
    for r in rows:
        obj: Dict[str, str] = {}
        for col in header:
            if col in ("pmt_score", "pmt_class"):
                # prefer top-level if present, else look inside json_ext
                v = r.get(col)
                if v is None:
                    jx = r.get("json_ext")
                    if isinstance(jx, str):
                        try:
                            jx = json.loads(jx) if jx.strip() else {}
                        except Exception:
                            jx = {}
                    if isinstance(jx, dict):
                        v = jx.get(col)
                obj[col] = "" if v is None else str(v)
            else:
                val = r.get(col)
                obj[col] = _pad_codes(col, val)
        out.append(obj)
    return out


class TargetingWorkflow(WorkflowHandler):
    """
    Enrich the uploaded file with PMT (pmt_score / pmt_class),
    then trigger import of valid items.
    """
    name = "TARGETING"

    def __init__(self, user=None):
        self.user = user

    def _resolve_upload_key(self, upload_uuid_str):
        try:
            u_uuid = _uuid.UUID(str(upload_uuid_str))
        except Exception:
            raise ValueError(f"Invalid upload_uuid: {upload_uuid_str!r}")

        try:
            R = apps.get_model("individual", "IndividualDataUploadRecords")
        except Exception as e:
            LOG.debug("Cannot load IndividualDataUploadRecords model: %s", e, exc_info=True)
            return u_uuid

        rec = R.objects.filter(uuid=u_uuid).order_by("-id").first()
        if rec:
            return rec.id
        LOG.warning("Upload UUID %s not found; falling back to UUID.", u_uuid)
        return u_uuid

    def _svc_get_rows(self, svc, upload_key):
        try:
            return svc.get_upload_rows(upload_key)
        except Exception:
            try:
                return svc.get_upload_rows(str(upload_key))
            except Exception:
                LOG.debug("svc.get_upload_rows failed for key=%r", upload_key, exc_info=True)
                return None

    def _svc_replace_file(self, svc, upload_key, file_obj):
        try:
            return svc.replace_upload_file(upload_key, file_obj)
        except Exception:
            try:
                return svc.replace_upload_file(str(upload_key), file_obj)
            except Exception:
                LOG.debug("svc.replace_upload_file failed for key=%r", upload_key, exc_info=True)
                return None

    def run(self, payload: dict):
        try:
            upload_uuid = payload.get("upload_uuid")
            if not upload_uuid:
                return {"success": False, "message": "Missing upload_uuid in workflow payload"}

            upload_key = self._resolve_upload_key(upload_uuid)
            svc = IndividualImportService(self.user)

            # --- PMT enrichment  ---
            if hasattr(svc, "get_upload_rows") and hasattr(svc, "replace_upload_file"):
                try:
                    rows = self._svc_get_rows(svc, upload_key)
                    if rows:
                        # keep using interview_key as HH grouping id
                        rows = enrich_rows_with_pmt(rows, hh_key="interview_key")

                        # IMPORTANT: sanitize columns for importer 
                        projected = _project_for_sink(rows)

                        new_file = data_to_file(projected, identifier=f"{upload_uuid}_pmt")
                        self._svc_replace_file(svc, upload_key, new_file)
                        LOG.info("PMT: enriched upload %s (key=%r) – pmt emitted as CSV columns.", upload_uuid, upload_key)
                    else:
                        LOG.info("PMT: no rows found for upload %s; skipping enrichment.", upload_uuid)
                except Exception:
                    LOG.exception("PMT enrichment failed for upload %s; proceeding without enrichment.", upload_uuid)
            else:
                LOG.warning("PMT: svc.get_upload_rows/replace_upload_file not available; skipping enrichment step.")

            # --- Trigger import of valid items ---
            svc.create_task_with_importing_valid_items(upload_key)
            LOG.info("TargetingWorkflow.run(): dispatched task for upload %s (key=%r)", upload_uuid, upload_key)
            return {"success": True, "message": "Workflow triggered"}
        except Exception as e:
            LOG.exception("TargetingWorkflow.run() failed: %s", e)
            return {"success": False, "message": str(e)}
