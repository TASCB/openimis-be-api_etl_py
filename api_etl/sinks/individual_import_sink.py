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
from individual.models import Individual
from workflow.services import WorkflowService

LOG = logging.getLogger(__name__)

# Hard exclude these keys from CSV headers (never as CSV columns)
CSV_FIELD_DENYLIST = {"raw", "_source", "phone", "email", "gender", "consent_res", "record_type", "pssn_wave"}

DEFAULT_DOB_SENTINEL = "1900-07-01"


class _SimpleRunner:
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
    if hasattr(workflow_cfg, "run"):
        return workflow_cfg

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

    # Friendly names (defensive)
    if isinstance(workflow_cfg, str):
        try:
            from individual.workflows.individual_upload_valid import process_import_valid_individuals_workflow  # noqa
        except Exception:
            process_import_valid_individuals_workflow = None

        wf_name = workflow_cfg.split(".", 1)[-1].strip()
        key = wf_name.lower()

        if process_import_valid_individuals_workflow and key == "python valid upload individuals":
            return _SimpleRunner(process_import_valid_individuals_workflow, name=wf_name)

    class _NoOp:
        def __init__(self, name="NOOP"):
            self.name = name

        def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
            LOG.warning("NOOP workflow used; requested: %s", workflow_cfg)
            return {"success": True, "detail": "noop"}

    return _NoOp(name=str(workflow_cfg))


class IndividualImportSink(DataSink):
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

        self.workflow_cfg: Any = self.config.get("sink_workflow", "Python Valid Upload Individuals")
        self.import_workflow_cfg: Any = self.config.get("sink_import_workflow", self.workflow_cfg)
        self.update_workflow_cfg: Any = self.config.get("sink_update_workflow", "Python Valid Update Individuals")

        self.group_aggregation_column: str = self.config.get("sink_group_aggregation_column", "group_code")
        self.csv_fields: Optional[List[str]] = self.config.get("sink_csv_fields")

        self.field_pad: Dict[str, int] = self.config.get(
            "sink_field_pad",
            {
                "location_code": 9,
                "village_code": 9,
                "ward_code": 6,
                "school_ward_code": 6,
                "hfac_ward_code": 6,
                "school_district_code": 3,
                "hfac_district_code": 3,
            },
        )

        self.trigger_after_upload: bool = bool(self.config.get("sink_trigger_workflow_after_upload", False))
        self._mode, self._method_name, self._method_sig = self._detect_import_method()

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
        raise RuntimeError("IndividualImportService exposes neither single nor bulk import method.")

    def _safe_json_loads(self, value: Any) -> Dict[str, Any]:
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                out = json.loads(value)
                return out if isinstance(out, dict) else {}
            except Exception:
                return {}
        return {}

    def _normalize_record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        # json_ext must be dict (sink writes it as JSON string into CSV)
        jx = self._safe_json_loads(rec.get("json_ext"))
        rec["json_ext"] = jx

        # Ensure dob not blank
        dob = rec.get("dob")
        if dob is None or str(dob).strip() == "":
            rec["dob"] = DEFAULT_DOB_SENTINEL
            jx["dob_missing"] = True
        else:
            jx.pop("dob_missing", None)

        # Never accidentally emit Json_ext (wrong header)
        rec.pop("Json_ext", None)
        return rec

    def _get_data_id(self, data: Dict[str, Any], key: str) -> Any:
        if not data or not key:
            return None
        parts = key.split("__")
        if parts[0] == "json_ext":
            jx = data.get("json_ext")
            if isinstance(jx, str):
                try:
                    jx = json.loads(jx)
                except Exception:
                    jx = {}
            if not isinstance(jx, dict):
                jx = {}
            return jx.get(parts[-1])
        return data.get(parts[-1])

    def _get_existing_individual_ids(self, data_ids: List[Any], model_lookup_field: str) -> Dict[Any, int]:
        clean_ids = [i for i in data_ids if i not in (None, "")]
        if not clean_ids:
            return {}
        filter_kwargs = {f"{model_lookup_field}__in": clean_ids}
        queryset = Individual.objects.filter(**filter_kwargs)
        results = queryset.values_list(model_lookup_field, "id")
        return dict(results) if results else {}

    def _split_existing_and_new(self, data: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        model_lookup_field = self.lookup_field
        data_ids = [self._get_data_id(record, model_lookup_field) for record in data]
        existing_map = self._get_existing_individual_ids(data_ids, model_lookup_field)

        existing_records: List[Dict[str, Any]] = []
        new_records: List[Dict[str, Any]] = []

        for record in data:
            data_id = self._get_data_id(record, model_lookup_field)
            if data_id in existing_map:
                record["ID"] = existing_map[data_id]
                existing_records.append(record)
            else:
                new_records.append(record)

        return existing_records, new_records

    @staticmethod
    def get_workflow(name: str) -> Dict[str, Any]:
        result = WorkflowService.get_workflows(name, "individual")
        if not result.get("success"):
            raise DataSink.Error(f"{result.get('message')}: {result.get('details')}")
        workflows = result.get("data", {}).get("workflows")
        if not workflows:
            raise DataSink.Error(f"Workflow not found: group=individual name={name}")
        if len(workflows) > 1:
            raise DataSink.Error(f"Multiple workflows found: group=individual name={name}")
        return workflows[0]

    def _resolve_workflow(self, cfg: Any) -> Any:
        if isinstance(cfg, dict) and "name" in cfg:
            return cfg
        if isinstance(cfg, str):
            try:
                return self.get_workflow(cfg)
            except Exception:
                LOG.warning("WorkflowService could not resolve '%s'; falling back to Python resolver", cfg)
        return _resolve_workflow_arg(self.user, cfg)

    def _effective_csv_fields(self, objs: List[Dict[str, Any]]) -> List[str]:
        if self.csv_fields:
            fields = [f for f in self.csv_fields if f]
        else:
            sample = objs[0]
            fields = [
                k for k, v in sample.items()
                if (not k.startswith("_")) and not isinstance(v, (dict, list))
            ]

        # Ensure lowercase json_ext ONLY
        fields = [("json_ext" if f == "Json_ext" else f) for f in fields]

        # Remove unwanted columns
        fields = [f for f in fields if f not in CSV_FIELD_DENYLIST]

        # Always include json_ext column (this is where raw lives)
        if "json_ext" not in fields:
            fields.append("json_ext")

        # Keep json_ext at end
        fields = [f for f in fields if f != "json_ext"] + ["json_ext"]
        return fields

    def _choose_group_col(self, csv_fields: List[str]) -> str:
        group_col = self.group_aggregation_column or "group_code"
        if group_col not in csv_fields:
            if "location_code" in csv_fields:
                return "location_code"
            return "group_code"
        return group_col

    def _to_csv_file(self, objs: List[Dict[str, Any]], filename_hint: str = "individuals"):
        if not objs:
            raise ValueError("No objects to convert to CSV")

        fields = self._effective_csv_fields(objs)
        LOG.info("IndividualImportSink CSV header: %s", fields)

        def value_for_field(row: Dict[str, Any], field: str) -> Any:
            if field == "json_ext":
                jx = row.get("json_ext")
                if isinstance(jx, str):
                    return jx
                if isinstance(jx, dict):
                    try:
                        return json.dumps(jx, ensure_ascii=False, separators=(",", ":"))
                    except Exception:
                        return ""
                return ""

            v = row.get(field)
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

        return data_to_file(rows, identifier=filename_hint)

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

        bulk: List[Dict[str, Any]] = [self._normalize_record(dict(o)) for o in objs]
        if not bulk:
            LOG.info("IndividualImportSink: nothing to push.")
            return

        existing_records, new_records = self._split_existing_and_new(bulk)

        # NEW RECORDS
        if new_records:
            new_fields = self._effective_csv_fields(new_records)
            new_group_col = self._choose_group_col(new_fields)
            import_file = self._to_csv_file(new_records, filename_hint=f"{bid or 'bulk'}_new")
            import_wf = self._resolve_workflow(self.import_workflow_cfg)
            method(import_file, import_wf, new_group_col)
            LOG.info("IndividualImportSink pushed %s new record(s).", len(new_records))

        # EXISTING RECORDS
        if existing_records and self.update_existing:
            upd_fields = self._effective_csv_fields(existing_records)
            upd_group_col = self._choose_group_col(upd_fields)
            update_file = self._to_csv_file(existing_records, filename_hint=f"{bid or 'bulk'}_update")
            update_wf = self._resolve_workflow(self.update_workflow_cfg)
            method(update_file, update_wf, upd_group_col)
            LOG.info("IndividualImportSink updated %s existing record(s).", len(existing_records))


# # api_etl/sinks/individual_import_sink.py
# from __future__ import annotations

# import importlib
# import inspect
# import json
# import logging
# import os
# import pkgutil
# import uuid as _uuid
# from typing import Any, Callable, Dict, Iterable, List, Optional

# from api_etl.apps import ApiEtlConfig as C
# from api_etl.sinks.base import DataSink
# from api_etl.utils import data_to_file
# from individual.models import Individual
# from individual.services import IndividualImportService
# from workflow.services import WorkflowService

# LOG = logging.getLogger(__name__)

# # These are safe to be CSV columns (the valid workflow accepts them)
# AUTO_INCLUDE_EXTRAS = [
#     "external_id",
#     "pmt_score",
#     "pmt_class",
#     "interview_key",
#     "hhrep",
#     "individual_role_code",
#     "group_code",
# ]

# # These must live ONLY inside Json_ext (jsonb). Do NOT send as CSV headers.
# JSONEXT_ONLY_KEYS = {"record_type", "consent_res", "pssn_wave"}

# # Hard exclude these keys from CSV headers
# CSV_FIELD_DENYLIST = {"raw", "_source", "phone", "gender", "email"} | JSONEXT_ONLY_KEYS

# # Default DOB used ONLY for stub/non-consented records where dob is missing (DB NOT NULL)
# DEFAULT_DOB_SENTINEL = "1900-07-01"

# # The Python Valid Upload Individuals workflow rejects these headers if present
# INVALID_CSV_HEADERS_FOR_VALID_UPLOAD = {"record_type", "consent_res", "pssn_wave"}


# class _SimpleRunner:
#     """Minimal wrapper exposing .run(ctx) so IndividualImportService can call it."""

#     def __init__(self, fn: Callable[..., Any], name: str = "wrapped"):
#         self._fn = fn
#         self.name = name

#     def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
#         try:
#             user_uuid = ctx.get("user_uuid")
#             upload_uuid = ctx.get("upload_uuid")
#             accepted = ctx.get("accepted")

#             sig = inspect.signature(self._fn)
#             params = sig.parameters

#             if "accepted" in params:
#                 self._fn(user_uuid, upload_uuid, accepted)
#             elif any(p.kind == p.VAR_KEYWORD for p in params.values()):
#                 self._fn(user_uuid, upload_uuid, accepted=accepted)
#             else:
#                 self._fn(user_uuid, upload_uuid)

#             return {"success": True, "detail": f"{self.name} executed"}
#         except Exception as e:
#             LOG.exception("Workflow runner failed")
#             return {"success": False, "message": str(e)}


# def _resolve_workflow_arg(user, workflow_cfg):
#     """
#     Python-side workflow resolver.

#     Supported forms:
#       1) Existing instance with .run -> return as-is
#       2) Dotted CLASS path (has .run) -> instantiate (prefers user=)
#       3) Dotted FUNCTION path -> wrap in _SimpleRunner
#       4) Registered Python workflow name from Individual module (defensive import)
#       5) Short key: scan individual.workflows.* for classes with .run
#       6) Fallback: NOOP runner
#     """
#     # 1) already a runner
#     if hasattr(workflow_cfg, "run"):
#         return workflow_cfg

#     # 2/3) dotted path class/function
#     if isinstance(workflow_cfg, str) and "." in workflow_cfg and " " not in workflow_cfg:
#         mod_path, _, name = workflow_cfg.rpartition(".")
#         try:
#             mod = importlib.import_module(mod_path)
#             obj = getattr(mod, name)
#             if inspect.isclass(obj):
#                 try:
#                     sig = inspect.signature(obj)
#                     if "user" in sig.parameters:
#                         return obj(user=user)
#                     return obj()
#                 except Exception:
#                     return obj()
#             if callable(obj):
#                 return _SimpleRunner(obj, name=workflow_cfg)
#         except Exception:
#             LOG.debug(
#                 "Dotted-path workflow resolution failed for '%s'",
#                 workflow_cfg,
#                 exc_info=True,
#             )

#     # 4) “friendly name” mapping (defensive imports)
#     if isinstance(workflow_cfg, str):
#         try:
#             from individual.workflows.individual_upload import process_import_individuals_workflow  # noqa
#         except Exception:
#             process_import_individuals_workflow = None

#         try:
#             from individual.workflows.individual_update import process_update_individuals_workflow  # noqa
#         except Exception:
#             process_update_individuals_workflow = None

#         try:
#             from individual.workflows.individual_upload_valid import process_import_valid_individuals_workflow  # noqa
#         except Exception:
#             process_import_valid_individuals_workflow = None

#         try:
#             from individual.workflows.individual_upload_valid import process_update_valid_individuals_workflow  # noqa
#         except Exception:
#             process_update_valid_individuals_workflow = None

#         wf_name = workflow_cfg
#         if "." in workflow_cfg:
#             _, wf_name = workflow_cfg.split(".", 1)
#         key = wf_name.strip().lower()

#         mapping = {}
#         if process_import_individuals_workflow:
#             mapping["python import individuals"] = process_import_individuals_workflow
#         if process_update_individuals_workflow:
#             mapping["python update individuals"] = process_update_individuals_workflow
#         if process_import_valid_individuals_workflow:
#             mapping["python valid upload individuals"] = process_import_valid_individuals_workflow
#         if process_update_valid_individuals_workflow:
#             mapping["python valid update individuals"] = process_update_valid_individuals_workflow

#         fn = mapping.get(key)
#         if fn:
#             return _SimpleRunner(fn, name=wf_name)

#     # 5) short-key scan across individual.workflows.*
#     try:
#         import individual.workflows as W

#         key = str(workflow_cfg).lower().replace("-", "_")
#         for _, modname, _ in pkgutil.iter_modules(W.__path__, prefix=W.__name__ + "."):
#             try:
#                 m = importlib.import_module(modname)
#             except Exception:
#                 continue
#             for cls_name, cls in inspect.getmembers(m, inspect.isclass):
#                 if hasattr(cls, "run"):
#                     cname = f"{modname}.{cls_name}".lower()
#                     if key in cname or key in cls_name.lower():
#                         try:
#                             sig = inspect.signature(cls)
#                             if "user" in sig.parameters:
#                                 return cls(user=user)
#                             return cls()
#                         except Exception:
#                             try:
#                                 return cls()
#                             except Exception:
#                                 continue
#     except Exception:
#         LOG.debug("Short-key scan failed for '%s'", workflow_cfg, exc_info=True)

#     # 6) fallback: noop
#     class _NoOp:
#         def __init__(self, name="NOOP"):
#             self.name = name

#         def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
#             LOG.warning(
#                 "NOOP workflow used; nothing will be inserted. Name requested: %s",
#                 workflow_cfg,
#             )
#             return {"success": True, "detail": "noop"}

#     return _NoOp(name=str(workflow_cfg))


# class IndividualImportSink(DataSink):
#     """
#     Push adapted Individual dicts into openIMIS via IndividualImportService.

#     Bulk mode:
#       - Writes CSV file
#       - NEW vs EXISTING split using `sink_model_lookup_field`
#       - Uses workflows

#     Single mode:
#       - import_individual/import_one per record
#     """

#     def __init__(
#         self,
#         user,
#         *,
#         batch: Optional[str] = None,
#         config: Optional[Dict[str, Any]] = None,
#     ):
#         super().__init__()
#         self.user = user
#         self.batch = batch
#         self.config: Dict[str, Any] = config or C.__dict__

#         self.svc = IndividualImportService(user=user)

#         if batch and hasattr(self.svc, "batch"):
#             try:
#                 setattr(self.svc, "batch", batch)
#             except Exception:
#                 LOG.debug("Could not set svc.batch", exc_info=True)

#         # IMPORTANT:
#         # - default to "external_id" because adapter provides it at top-level
#         # - you can still override via ApiEtlConfig.sink_model_lookup_field
#         self.lookup_field: str = self.config.get("sink_model_lookup_field", "external_id")
#         self.update_existing: bool = bool(self.config.get("sink_update_existing", True))

#         self.workflow_cfg: Any = self.config.get("sink_workflow", "Python Import Individuals")
#         self.import_workflow_cfg: Any = self.config.get("sink_import_workflow", self.workflow_cfg)
#         self.update_workflow_cfg: Any = self.config.get("sink_update_workflow", "Python Update Individuals")

#         self.group_aggregation_column: str = self.config.get("sink_group_aggregation_column", "group_code")
#         self.csv_fields: Optional[List[str]] = self.config.get("sink_csv_fields")

#         self.field_pad: Dict[str, int] = self.config.get(
#             "sink_field_pad",
#             {
#                 "location_code": 9,
#                 "village_code": 9,
#                 "ward_code": 6,
#                 "school_ward_code": 6,
#                 "hfac_ward_code": 6,
#                 "school_district_code": 3,
#                 "hfac_district_code": 3,
#             },
#         )

#         self.trigger_after_upload: bool = bool(self.config.get("sink_trigger_workflow_after_upload", False))

#         self._mode, self._method_name, self._method_sig = self._detect_import_method()

#     # ---------- capability detection ----------
#     def _detect_import_method(self):
#         for name in ("import_individual", "import_one"):
#             if hasattr(self.svc, name):
#                 try:
#                     return "single", name, inspect.signature(getattr(self.svc, name))
#                 except Exception:
#                     return "single", name, None
#         for name in ("import_individuals", "import_bulk"):
#             if hasattr(self.svc, name):
#                 try:
#                     return "bulk", name, inspect.signature(getattr(self.svc, name))
#                 except Exception:
#                     return "bulk", name, None
#         raise RuntimeError(
#             "IndividualImportService exposes neither 'import_individual' "
#             "nor 'import_individuals'/'import_bulk'."
#         )

#     # ---------- record normalization ----------
#     def _safe_json_loads(self, value: Any) -> Dict[str, Any]:
#         if value is None or value == "":
#             return {}
#         if isinstance(value, dict):
#             return value
#         if isinstance(value, str):
#             try:
#                 out = json.loads(value)
#                 return out if isinstance(out, dict) else {}
#             except Exception:
#                 return {}
#         return {}

#     def _normalize_record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
#         """
#         Ensure:
#         - rec["json_ext"] is a dict (string -> dict)
#         - if a legacy nested key jx["json_ext"] exists, MERGE it into top-level and DELETE it
#           (this prevents Json_ext->'json_ext' being a string in DB)
#         - promote consent_res / record_type / pssn_wave to top-level json_ext (GraphQL filter-ready)
#         - dob is never null (DB constraint)
#         """
#         # 1) normalize json_ext itself (string -> dict)
#         jx = self._safe_json_loads(rec.get("json_ext"))
#         rec["json_ext"] = jx

#         # 2) Legacy cleanup: unwrap nested "json_ext" if present
#         legacy = jx.get("json_ext")
#         legacy_obj: Dict[str, Any] = {}
#         if isinstance(legacy, str):
#             legacy_obj = self._safe_json_loads(legacy)
#         elif isinstance(legacy, dict):
#             legacy_obj = legacy

#         if legacy_obj:
#             for k, v in legacy_obj.items():
#                 if k not in jx:
#                     jx[k] = v

#         # always remove nested key so we never write it back again
#         if "json_ext" in jx:
#             jx.pop("json_ext", None)

#         # 3) raw dict (if present)
#         raw = jx.get("raw") if isinstance(jx.get("raw"), dict) else {}

#         # 4) promote keys (top-level json_ext)
#         def _pick(*keys):
#             for k in keys:
#                 if isinstance(raw, dict):
#                     v = raw.get(k)
#                     if v not in (None, ""):
#                         return v
#                 v2 = jx.get(k)
#                 if v2 not in (None, ""):
#                     return v2
#             return None

#         consent = _pick("consent_res", "ss_consent_res")
#         if consent is not None and str(consent).strip() != "":
#             jx["consent_res"] = str(consent).strip()

#         record_type = _pick("record_type", "ss_record_type")
#         if record_type is not None and str(record_type).strip() != "":
#             jx["record_type"] = str(record_type).strip()

#         wave = _pick("pssn_wave", "ss_pssn_wave")
#         if wave is not None and str(wave).strip() != "":
#             jx["pssn_wave"] = str(wave).strip()

#         # 5) ensure DOB
#         dob = rec.get("dob")
#         if dob is None or str(dob).strip() == "":
#             rec["dob"] = DEFAULT_DOB_SENTINEL
#             jx["dob_missing"] = True
#         else:
#             jx.pop("dob_missing", None)

#         return rec

#     # ---------- new/existing split ----------
#     def _get_data_id(self, data: Dict[str, Any], key: str) -> Any:
#         """
#         Supports:
#         - top-level field: "external_id"
#         - json_ext nested lookup: "json_ext__external_id"
#         """
#         if not data or not key:
#             return None

#         parts = key.split("__")
#         if parts[0] == "json_ext":
#             jx = data.get("json_ext")
#             if isinstance(jx, str):
#                 jx = self._safe_json_loads(jx)
#             if not isinstance(jx, dict):
#                 jx = {}
#             return jx.get(parts[-1])

#         return data.get(parts[-1])

#     def _get_existing_individual_ids(self, data_ids: List[Any], model_lookup_field: str) -> Dict[Any, int]:
#         clean_ids = [i for i in data_ids if i not in (None, "")]
#         if not clean_ids:
#             return {}

#         filter_kwargs = {f"{model_lookup_field}__in": clean_ids}
#         queryset = Individual.objects.filter(**filter_kwargs)
#         results = queryset.values_list(model_lookup_field, "id")
#         return dict(results) if results else {}

#     def _split_existing_and_new(self, data: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
#         model_lookup_field = self.lookup_field
#         data_ids = [self._get_data_id(record, model_lookup_field) for record in data]
#         existing_map = self._get_existing_individual_ids(data_ids, model_lookup_field)

#         existing_records: List[Dict[str, Any]] = []
#         new_records: List[Dict[str, Any]] = []

#         for record in data:
#             data_id = self._get_data_id(record, model_lookup_field)
#             if data_id in existing_map:
#                 record["ID"] = existing_map[data_id]
#                 existing_records.append(record)
#             else:
#                 new_records.append(record)

#         return existing_records, new_records

#     # ---------- workflow resolution ----------
#     @staticmethod
#     def get_workflow(name: str) -> Dict[str, Any]:
#         result = WorkflowService.get_workflows(name, "individual")
#         if not result.get("success"):
#             raise DataSink.Error(f"{result.get('message')}: {result.get('details')}")
#         workflows = result.get("data", {}).get("workflows")
#         if not workflows:
#             raise DataSink.Error(f"Workflow not found: group=individual name={name}")
#         if len(workflows) > 1:
#             raise DataSink.Error(f"Multiple workflows found: group=individual name={name}")
#         return workflows[0]

#     def _resolve_workflow(self, cfg: Any) -> Any:
#         if isinstance(cfg, dict) and "name" in cfg:
#             return cfg

#         if isinstance(cfg, str):
#             try:
#                 return self.get_workflow(cfg)
#             except Exception:
#                 LOG.warning(
#                     "WorkflowService could not resolve '%s'; falling back to Python workflow resolution",
#                     cfg,
#                 )

#         return _resolve_workflow_arg(self.user, cfg)

#     # ---------- CSV helpers ----------
#     def _effective_csv_fields(self, objs: List[Dict[str, Any]]) -> List[str]:
#         if self.csv_fields:
#             fields = [f for f in self.csv_fields if f != ""]
#         else:
#             sample = objs[0]
#             fields = [
#                 k
#                 for k, v in sample.items()
#                 if (not k.startswith("_")) and not isinstance(v, (dict, list))
#             ]

#         # remove denylisted keys
#         fields = [f for f in fields if f not in CSV_FIELD_DENYLIST]

#         # ensure json_ext is present
#         if "json_ext" not in fields:
#             fields.append("json_ext")

#         # add safe extras (but never re-add denylisted keys)
#         desired_extras = set(AUTO_INCLUDE_EXTRAS) | {"pmt_score", "pmt_class"}
#         for extra in desired_extras:
#             if extra in CSV_FIELD_DENYLIST:
#                 continue
#             if extra not in fields:
#                 fields.append(extra)

#         # move json_ext to end
#         if "json_ext" in fields:
#             fields = [f for f in fields if f != "json_ext"] + ["json_ext"]

#         return fields

#     def _choose_group_col(self, csv_fields: List[str]) -> str:
#         group_col = self.group_aggregation_column or "group_code"
#         if group_col not in csv_fields:
#             if "location_code" in csv_fields:
#                 LOG.info(
#                     "Group aggregation column '%s' not in CSV; using 'location_code' instead.",
#                     group_col,
#                 )
#                 return "location_code"
#             LOG.info(
#                 "Group aggregation column '%s' not in CSV; using 'group_code' instead.",
#                 group_col,
#             )
#             return "group_code"
#         return group_col

#     def _to_csv_file(self, objs: List[Dict[str, Any]], filename_hint: str = "individuals"):
#         if not objs:
#             raise ValueError("No objects to convert to CSV")

#         fields = self._effective_csv_fields(objs)

#         # HARD SAFETY: valid upload workflow rejects these headers
#         fields = [f for f in fields if f not in INVALID_CSV_HEADERS_FOR_VALID_UPLOAD]

#         # move json_ext to end (again, after filtering)
#         if "json_ext" in fields:
#             fields = [f for f in fields if f != "json_ext"] + ["json_ext"]

#         LOG.info("IndividualImportSink CSV header (final): %s", fields)

#         def value_for_field(row: Dict[str, Any], field: str) -> Any:
#             if field == "json_ext":
#                 jx = row.get("json_ext")
#                 if isinstance(jx, str):
#                     return jx
#                 if isinstance(jx, dict):
#                     try:
#                         return json.dumps(jx, ensure_ascii=False, separators=(",", ":"))
#                     except Exception:
#                         return ""
#                 return ""

#             v = row.get(field)
#             if (v is None) and isinstance(row.get("json_ext"), dict):
#                 v = row["json_ext"].get(field)

#             if v is None:
#                 return ""

#             s = str(v).strip()
#             if field in self.field_pad and s.isdigit():
#                 return s.zfill(self.field_pad[field])
#             return s

#         rows: List[Dict[str, Any]] = []
#         for o in objs:
#             r = {k: "" for k in fields}
#             for k in fields:
#                 r[k] = value_for_field(o, k)
#             rows.append(r)

#         f = data_to_file(rows, identifier=filename_hint)

#         if os.getenv("API_ETL_DUMP_BULK_CSV"):
#             try:
#                 p = f"/tmp/{filename_hint}.csv"
#                 if hasattr(f, "seek"):
#                     f.seek(0)
#                 with open(p, "wb") as w:
#                     w.write(f.read())
#                 LOG.info("Dumped bulk CSV to %s", p)
#                 if hasattr(f, "seek"):
#                     f.seek(0)
#             except Exception:
#                 LOG.exception("Failed to dump bulk CSV to /tmp")

#         return f

#     # ---------- push ----------
#     def push(self, objs: Iterable[Dict[str, Any]], batch_identifier: Optional[str] = None) -> None:
#         bid = batch_identifier or self.batch

#         if bid and hasattr(self.svc, "batch"):
#             try:
#                 setattr(self.svc, "batch", bid)
#             except Exception:
#                 LOG.debug("Could not set svc.batch for call", exc_info=True)

#         method = getattr(self.svc, self._method_name)

#         # SINGLE MODE
#         if self._mode == "single":
#             count = 0
#             for obj in objs:
#                 method(obj)
#                 count += 1
#             LOG.info("IndividualImportSink (single) pushed %s record(s).", count)
#             return

#         # BULK MODE: normalize -> split -> upload
#         bulk: List[Dict[str, Any]] = [self._normalize_record(dict(o)) for o in objs]
#         if not bulk:
#             LOG.info("IndividualImportSink: nothing to push.")
#             return

#         existing_records, new_records = self._split_existing_and_new(bulk)
#         upload_result = None

#         # NEW RECORDS (IMPORT)
#         if new_records:
#             new_fields = self._effective_csv_fields(new_records)
#             new_group_col = self._choose_group_col(new_fields)
#             import_file = self._to_csv_file(new_records, filename_hint=f"{bid or 'bulk'}_new")
#             import_wf = self._resolve_workflow(self.import_workflow_cfg)

#             try:
#                 upload_result = method(import_file, import_wf, new_group_col)
#                 LOG.info("IndividualImportSink (bulk-file/import) pushed %s new record(s).", len(new_records))
#             except Exception:
#                 LOG.exception("Individual bulk import (new records) failed (n=%s)", len(new_records))
#                 raise
#         else:
#             LOG.info("IndividualImportSink: no new records to import.")

#         # EXISTING RECORDS (UPDATE)
#         if existing_records:
#             if not self.update_existing:
#                 LOG.info("Skipping %s existing records due to sink_update_existing = False", len(existing_records))
#             else:
#                 upd_fields = self._effective_csv_fields(existing_records)
#                 upd_group_col = self._choose_group_col(upd_fields)
#                 update_file = self._to_csv_file(existing_records, filename_hint=f"{bid or 'bulk'}_update")
#                 update_wf = self._resolve_workflow(self.update_workflow_cfg)

#                 try:
#                     upload_result = method(update_file, update_wf, upd_group_col)
#                     LOG.info("IndividualImportSink (bulk-file/update) pushed %s existing record(s).", len(existing_records))
#                 except Exception:
#                     LOG.exception("Individual bulk import (existing records) failed (n=%s)", len(existing_records))
#                     raise
#         else:
#             LOG.info("IndividualImportSink: no existing records to update.")

#         # Optional auto-trigger workflow after upload
#         if self.trigger_after_upload and upload_result is not None:
#             upload_uuid = None
#             try:
#                 if hasattr(upload_result, "uuid"):
#                     upload_uuid = str(upload_result.uuid)
#                 elif isinstance(upload_result, dict):
#                     data = upload_result.get("data") if isinstance(upload_result.get("data"), dict) else {}
#                     upload_uuid = (
#                         str(
#                             data.get("upload_uuid")
#                             or data.get("uuid")
#                             or upload_result.get("upload_uuid")
#                             or upload_result.get("uuid")
#                             or upload_result.get("upload_id")
#                             or ""
#                         )
#                         or None
#                     )
#                 elif isinstance(upload_result, (_uuid.UUID, int, str)):
#                     upload_uuid = str(upload_result)

#                 if upload_uuid:
#                     LOG.info("Auto-trigger workflow on upload %s (sink_trigger_workflow_after_upload=True)", upload_uuid)
#                     wf = self._resolve_workflow(self.workflow_cfg)
#                     payload = {"upload_uuid": str(upload_uuid)}

#                     user_uuid = None
#                     for attr in ("id", "uuid"):
#                         if hasattr(self.user, attr):
#                             user_uuid = getattr(self.user, attr)
#                             break
#                     if user_uuid:
#                         payload["user_uuid"] = str(user_uuid)

#                     out = wf.run(payload)
#                     LOG.info("Workflow.run() returned: %s", out)
#                 else:
#                     LOG.warning("Could not infer upload UUID from result; skipping auto-trigger.")
#             except Exception:
#                 LOG.exception("Auto-triggering workflow after upload failed.")
