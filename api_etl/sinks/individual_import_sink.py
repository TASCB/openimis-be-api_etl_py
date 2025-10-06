# api_etl/sinks/individual_import_sink.py
from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
import uuid as _uuid
import json
from typing import Any, Dict, Iterable, List, Optional, Callable

from api_etl.sinks.base import DataSink
from api_etl.apps import ApiEtlConfig as C
from api_etl.utils import data_to_file
from individual.services import IndividualImportService

LOG = logging.getLogger(__name__)

AUTO_INCLUDE_EXTRAS = [
    "external_id",
    "pmt_score",
    "pmt_class",
    "interview_key",
    "hhrep",
    "individual_role_code",
    "group_code",
]

# Hard exclude these keys from CSV headers (debug/meta that trip validation)
CSV_FIELD_DENYLIST = {"raw", "_source"}


class _SimpleRunner:
    """Minimal wrapper exposing .run(ctx) so IndividualImportService can call it."""
    def __init__(self, fn: Callable[..., Any], name: str = "wrapped"):
        self._fn = fn
        self.name = name

    def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_uuid = ctx.get("user_uuid")
            upload_uuid = ctx.get("upload_uuid")
            accepted = ctx.get("accepted")

            sig = inspect.signature(self._fn)
            params = sig.parameters

            if "accepted" in params:
                self._fn(user_uuid, upload_uuid, accepted)
            elif any(p.kind == p.VAR_KEYWORD for p in params.values()):
                self._fn(user_uuid, upload_uuid, accepted=accepted)
            else:
                self._fn(user_uuid, upload_uuid)

            return {"success": True, "detail": f"{self.name} executed"}
        except Exception as e:
            LOG.exception("Workflow runner failed")
            return {"success": False, "message": str(e)}


def _resolve_workflow_arg(user, workflow_cfg):
    """
    Resolve to an object exposing .run(ctx).

    Supported forms:
      1) Existing instance with .run -> return as-is
      2) Dotted CLASS path (has .run) -> instantiate (prefers user=)
      3) Dotted FUNCTION path -> wrap in _SimpleRunner
      4) Registered Python workflow name from Individual module (defensive import)
      5) Short key: scan individual.workflows.* for classes with .run
      6) Fallback: NOOP runner
    """
    # 1) already a runner
    if hasattr(workflow_cfg, "run"):
        return workflow_cfg

    # 2/3) dotted path
    if isinstance(workflow_cfg, str) and "." in workflow_cfg and " " not in workflow_cfg:
        mod_path, _, name = workflow_cfg.rpartition(".")
        try:
            mod = importlib.import_module(mod_path)
            obj = getattr(mod, name)
            if inspect.isclass(obj):
                try:
                    sig = inspect.signature(obj)
                    if "user" in sig.parameters:
                        return obj(user=user)
                    return obj()
                except Exception:
                    return obj()
            if callable(obj):
                return _SimpleRunner(obj, name=workflow_cfg)
        except Exception:
            LOG.debug("Dotted-path workflow resolution failed for '%s'", workflow_cfg, exc_info=True)

    # 4) “friendly name” mapping (defensive imports)
    if isinstance(workflow_cfg, str):
        # Base flows (may not exist in every deployment)
        try:
            from individual.workflows.individual_upload import process_import_individuals_workflow  # noqa
        except Exception:
            process_import_individuals_workflow = None
        try:
            from individual.workflows.individual_update import process_update_individuals_workflow  # noqa
        except Exception:
            process_update_individuals_workflow = None

        # VALID approval flows (import each safely)
        try:
            from individual.workflows.individual_upload_valid import process_import_valid_individuals_workflow  # noqa
        except Exception:
            process_import_valid_individuals_workflow = None
        try:
            from individual.workflows.individual_upload_valid import process_update_valid_individuals_workflow  # noqa
        except Exception:
            process_update_valid_individuals_workflow = None

        wf_name = workflow_cfg
        if "." in workflow_cfg:
            _, wf_name = workflow_cfg.split(".", 1)
        key = wf_name.strip().lower()

        mapping = {}
        if process_import_individuals_workflow:
            mapping["python import individuals"] = process_import_individuals_workflow
        if process_update_individuals_workflow:
            mapping["python update individuals"] = process_update_individuals_workflow
        if process_import_valid_individuals_workflow:
            mapping["python valid upload individuals"] = process_import_valid_individuals_workflow
        if process_update_valid_individuals_workflow:
            mapping["python valid update individuals"] = process_update_valid_individuals_workflow

        fn = mapping.get(key)
        if fn:
            return _SimpleRunner(fn, name=wf_name)

    # 5) short-key scan across individual.workflows.*
    try:
        import individual.workflows as W
        key = str(workflow_cfg).lower().replace("-", "_")
        for _, modname, _ in pkgutil.iter_modules(W.__path__, prefix=W.__name__ + "."):
            try:
                m = importlib.import_module(modname)
            except Exception:
                continue
            for cls_name, cls in inspect.getmembers(m, inspect.isclass):
                if hasattr(cls, "run"):
                    cname = f"{modname}.{cls_name}".lower()
                    if key in cname or key in cls_name.lower():
                        try:
                            sig = inspect.signature(cls)
                            if "user" in sig.parameters:
                                return cls(user=user)
                            return cls()
                        except Exception:
                            try:
                                return cls()
                            except Exception:
                                continue
    except Exception:
        LOG.debug("Short-key scan failed for '%s'", workflow_cfg, exc_info=True)

    # 6) fallback: noop
    class _NoOp:
        def __init__(self, name="NOOP"):
            self.name = name
        def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
            LOG.warning("NOOP workflow used; nothing will be inserted. Name requested: %s", workflow_cfg)
            return {"success": True, "detail": "noop"}

    return _NoOp(name=str(workflow_cfg))


class IndividualImportSink(DataSink):
    """
    Push adapted Individual dicts into openIMIS via IndividualImportService.

    Supports BOTH service styles:
      - single: import_individual(obj, ...)
      - bulk:   import_individuals(import_file, workflow_obj, group_aggregation_column, ...)
    """

    def __init__(self, user, *, batch: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.user = user
        self.batch = batch
        self.config: Dict[str, Any] = config or C.__dict__

        self.svc = IndividualImportService(user=user)

        if batch and hasattr(self.svc, "batch"):
            try:
                setattr(self.svc, "batch", batch)
            except Exception:
                LOG.debug("Could not set svc.batch", exc_info=True)

        self.lookup_field: str = self.config.get("sink_model_lookup_field", "json_ext__external_id")
        self.update_existing: bool = bool(self.config.get("sink_update_existing", True))

        self.workflow_cfg: Any = self.config.get("sink_workflow", "individual.Python Valid Upload Individuals")
        self.group_aggregation_column: str = self.config.get("sink_group_aggregation_column", "group_code")

        self.csv_fields: Optional[List[str]] = self.config.get("sink_csv_fields")

        self.field_pad: Dict[str, int] = self.config.get("sink_field_pad", {
            "location_code": 9,
            "village_code": 9,
            "ward_code": 6,
            "school_ward_code": 6,
            "hfac_ward_code": 6,
            "school_district_code": 3,
            "hfac_district_code": 3,
        })

        self.trigger_after_upload: bool = bool(self.config.get("sink_trigger_workflow_after_upload", False))

        self._mode, self._method_name, self._method_sig = self._detect_import_method()

    # ---------- capability detection ----------
    def _detect_import_method(self):
        for name in ("import_individual", "import_one"):
            if hasattr(self.svc, name):
                try:
                    return "single", name, inspect.signature(getattr(self.svc, name))
                except Exception:
                    return "single", name, None
        for name in ("import_individuals", "import_bulk"):
            if hasattr(self.svc, name):
                try:
                    return "bulk", name, inspect.signature(getattr(self.svc, name))
                except Exception:
                    return "bulk", name, None
        raise RuntimeError("IndividualImportService exposes neither 'import_individual' nor 'import_individuals'/'import_bulk'.")

    # ---------- helpers ----------
    def _effective_csv_fields(self, objs: List[Dict[str, Any]]) -> List[str]:
        """
        Decide final CSV header list:
          - start from configured sink_csv_fields if provided
          - else discover from sample obj (excluding dict/list/json_ext)
          - filter out deny-listed debug/meta keys (e.g., 'raw')
          - ADD a 'json_ext' column so we can ship the raw keys compactly
          - Auto-include helpful extras (top-level or inside json_ext)
        """
        if self.csv_fields:
            fields = [f for f in self.csv_fields if f != ""]
        else:
            sample = objs[0]
            fields = [
                k for k, v in sample.items()
                if (not k.startswith("_"))
                and not isinstance(v, (dict, list))
            ]

        # drop deny-listed keys that can break validation
        fields = [f for f in fields if f not in CSV_FIELD_DENYLIST]

        # ensure json_ext column exists (we'll serialize it)
        if "json_ext" not in fields:
            fields.append("json_ext")

        # add extras we care about even if only inside json_ext
        desired_extras = set(AUTO_INCLUDE_EXTRAS) | {"pmt_score", "pmt_class"}
        for extra in desired_extras:
            if extra not in fields:
                fields.append(extra)

        # keep order stable-ish: move json_ext to the end
        if "json_ext" in fields:
            fields = [f for f in fields if f != "json_ext"] + ["json_ext"]

        return fields

    def _to_csv_file(self, objs: List[Dict[str, Any]], filename_hint: str = "individuals"):
        if not objs:
            raise ValueError("No objects to convert to CSV")

        fields = self._effective_csv_fields(objs)
        LOG.info("IndividualImportSink CSV header: %s", fields)

        def value_for_field(row: Dict[str, Any], field: str) -> Any:
            """
            Prefer top-level row[field]; if missing/None, look into row['json_ext'][field].
            Apply zero-padding as per config for code-like fields.
            For 'json_ext' field, serialize the entire adapter json_ext dict.
            """
            if field == "json_ext":
                jx = row.get("json_ext")
                if isinstance(jx, str):
                    # assume already-serialized; pass as-is
                    return jx
                if isinstance(jx, dict):
                    try:
                        return json.dumps(jx, ensure_ascii=False, separators=(",", ":"))
                    except Exception:
                        return ""
                return ""

            v = row.get(field)
            if (v is None) and isinstance(row.get("json_ext"), dict):
                v = row["json_ext"].get(field)

            if v is None:
                return ""

            s = str(v).strip()
            if field in self.field_pad and s.isdigit():
                return s.zfill(self.field_pad[field])
            return s

        rows: List[Dict[str, Any]] = []
        for o in objs:
            r = {k: "" for k in fields}
            for k in fields:
                r[k] = value_for_field(o, k)
            rows.append(r)

        f = data_to_file(rows, identifier=filename_hint)

        if os.getenv("API_ETL_DUMP_BULK_CSV"):
            try:
                p = f"/tmp/{filename_hint}.csv"
                if hasattr(f, "seek"):
                    f.seek(0)
                with open(p, "wb") as w:
                    w.write(f.read())
                LOG.info("Dumped bulk CSV to %s", p)
                if hasattr(f, "seek"):
                    f.seek(0)
            except Exception:
                LOG.exception("Failed to dump bulk CSV to /tmp")

        return f

    # ---------- push ----------
    def push(self, objs: Iterable[Dict[str, Any]], batch_identifier: Optional[str] = None) -> None:
        bid = batch_identifier or self.batch

        if bid and hasattr(self.svc, "batch"):
            try:
                setattr(self.svc, "batch", bid)
            except Exception:
                LOG.debug("Could not set svc.batch for call", exc_info=True)

        method = getattr(self.svc, self._method_name)

        if self._mode == "single":
            count = 0
            for obj in objs:
                method(obj)
                count += 1
            LOG.info("IndividualImportSink (single) pushed %s record(s).", count)
            return

        bulk: List[Dict[str, Any]] = list(objs)
        if not bulk:
            LOG.info("IndividualImportSink: nothing to push.")
            return

        csv_fields = self._effective_csv_fields(bulk)

        # fall back to location_code when available.
        group_col = self.group_aggregation_column or "group_code"
        if group_col not in csv_fields:
            if "location_code" in csv_fields:
                LOG.info("Group aggregation column '%s' not in CSV; using 'location_code' instead.", group_col)
                group_col = "location_code"
            else:
                LOG.info("Group aggregation column '%s' not in CSV; using 'group_code' instead.", group_col)
                group_col = "group_code"

        import_file = self._to_csv_file(bulk, filename_hint=bid or "bulk")
        workflow_obj = _resolve_workflow_arg(self.user, self.workflow_cfg)

        upload_result = None
        try:
            # IndividualImportService.import_individuals(import_file, workflow, group_aggregation_column)
            upload_result = method(import_file, workflow_obj, group_col)
        except Exception:
            LOG.exception("Individual bulk import failed (n=%s)", len(bulk))
            raise

        LOG.info("IndividualImportSink (bulk-file) pushed %s record(s).", len(bulk))

        # Optional auto-trigger (approval)
        if self.trigger_after_upload:
            upload_uuid = None
            try:
                if hasattr(upload_result, "uuid"):
                    upload_uuid = str(upload_result.uuid)
                elif isinstance(upload_result, dict):
                    data = upload_result.get("data") if isinstance(upload_result.get("data"), dict) else {}
                    upload_uuid = str(
                        data.get("upload_uuid")
                        or data.get("uuid")
                        or upload_result.get("upload_uuid")
                        or upload_result.get("uuid")
                        or upload_result.get("upload_id")
                        or ""
                    ) or None
                elif isinstance(upload_result, (_uuid.UUID, int, str)):
                    upload_uuid = str(upload_result)

                if upload_uuid:
                    LOG.info("Auto-trigger workflow on upload %s", upload_uuid)
                    wf = _resolve_workflow_arg(self.user, self.workflow_cfg)
                    payload = {"upload_uuid": str(upload_uuid)}
                    user_uuid = None
                    for attr in ("id", "uuid"):
                        if hasattr(self.user, attr):
                            user_uuid = getattr(self.user, attr)
                            break
                    if user_uuid:
                        payload["user_uuid"] = str(user_uuid)
                    out = wf.run(payload)
                    LOG.info("Workflow.run() returned: %s", out)
                else:
                    LOG.warning("Could not infer upload UUID from result; skipping auto-trigger.")
            except Exception:
                LOG.exception("Auto-triggering workflow after upload failed.")