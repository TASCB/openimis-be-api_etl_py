from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, DefaultDict, Tuple
from collections import defaultdict

# PMT enrichment
from api_etl.workflows.pmt import enrich_rows_with_pmt

from api_etl.services.base import ETLService as _BaseService
from api_etl.apps import ApiEtlConfig as C

# Source / Adapter
from api_etl.sources.survey_solutions_export_source import (
    SurveySolutionsExportSource as Source,
)
from api_etl.adapters.survey_solutions_targeting_adapter import (
    SurveySolutionsTargetingAdapter as Adapter,
)

# Sink
try:
    from api_etl.sinks.individual_import_sink import IndividualImportSink as Sink
except Exception:
    try:
        from api_etl.sinks.survey_solutions_sink import IndividualImportSink as Sink
    except Exception:
        Sink = None

from api_etl.utils import data_to_file, get_timestamped_batch_identifier

LOG = logging.getLogger(__name__)


# --------------- PAA Questionnaire Matching Pipeline -----------------------------


def _normalize_text(text: str) -> str:
    """
    Normalize text for matching: lowercase, trim, collapse spaces, strip punctuation.
    """
    if not isinstance(text, str):
        return ""

    # Convert to lowercase and strip
    normalized = text.lower().strip()

    # Remove diacritics/accents
    normalized = unicodedata.normalize("NFD", normalized)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")

    # Replace multiple spaces/whitespace with single space
    normalized = re.sub(r"\s+", " ", normalized)

    # Strip punctuation but keep alphanumeric and spaces
    normalized = re.sub(r"[^\w\s]", " ", normalized)

    # Final cleanup of multiple spaces
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def _remove_stop_words(text: str, stop_words: List[str]) -> str:
    """
    Remove stop words from normalized text.
    """
    if not text or not stop_words:
        return text

    words = text.split()
    filtered_words = [word for word in words if word not in stop_words]
    return " ".join(filtered_words)


def _extract_code_tokens(text: str) -> List[str]:
    """
    Extract potential location codes from text (4+ digit sequences).
    """
    if not isinstance(text, str):
        return []

    # Find sequences of 4 or more digits
    code_pattern = r"\b\d{4,}\b"
    codes = re.findall(code_pattern, text)
    return codes


def _score_questionnaire_match(
    questionnaire: Dict[str, Any],
    district_name: str,
    district_code: str,
    region_code: str,
    config: Dict[str, Any],
) -> Tuple[int, str]:
    """
    Score a questionnaire against PAA criteria.

    Returns:
        (score, matching_strategy)

    Score meanings:
        0 = No match
        1 = Name-only match (weak)
        2 = Code-aware match (strong)
        3 = Code+name match (strongest)
        4 = Suffix match (district name at end after dash)
        5 = Suffix + code match (strongest)
    """
    title = questionnaire.get("Title", "")
    if not title:
        return 0, "no-title"

    # Get configuration
    stop_words = _cfg_get(
        config, "questionnaire_matching_stop_words", default=["district", "council"]
    )
    enable_code_matching = _cfg_get(
        config, "questionnaire_matching_enable_code_tokens", default=True
    )

    # Normalize inputs
    normalized_title = _normalize_text(title)
    normalized_district = _normalize_text(district_name)

    # Remove stop words if configured
    if stop_words:
        stop_words_normalized = [_normalize_text(word) for word in stop_words]
        normalized_title = _remove_stop_words(normalized_title, stop_words_normalized)
        normalized_district = _remove_stop_words(
            normalized_district, stop_words_normalized
        )

    # Extract codes from title
    title_codes = _extract_code_tokens(title)

    # Check for code matches
    has_district_code = False
    has_region_code = False

    if enable_code_matching and district_code:
        # Check if district code (or first 4 digits) appears in title
        district_prefix = (
            district_code[:4] if len(district_code) >= 4 else district_code
        )
        has_district_code = any(
            code.startswith(district_prefix) for code in title_codes
        )

    if enable_code_matching and region_code:
        # Check if region code (first 2 digits) appears in title codes
        region_prefix = region_code[:2] if len(region_code) >= 2 else region_code
        has_region_code = any(code.startswith(region_prefix) for code in title_codes)

    # Check for SUFFIX match (district name at END of title after last dash)
    has_suffix_match = False
    if normalized_district:
        # Extract the suffix (text after last dash or just the whole title)
        if "-" in title:
            suffix = title.split("-")[-1].strip()
            suffix_normalized = _normalize_text(suffix)
            # Remove stop words from suffix too
            if stop_words:
                stop_words_normalized = [_normalize_text(word) for word in stop_words]
                suffix_normalized = _remove_stop_words(
                    suffix_normalized, stop_words_normalized
                )

            # Check if district name matches the suffix (partial or full match)
            district_words = normalized_district.split()
            suffix_words = suffix_normalized.split()

            # Suffix match if:
            # 1. Any significant district word appears in the suffix, OR
            # 2. Suffix contains significant part of district name
            has_suffix_match = any(
                dword in suffix_normalized or suffix_normalized in dword
                for dword in district_words
                if len(dword) > 3  # ignore short words
            ) or any(
                sword in normalized_district or normalized_district in sword
                for sword in suffix_words
                if len(sword) > 3
            )

    # Check for general name matches (whole word contains - anywhere in title)
    has_name_match = False
    if normalized_district:
        # Split district name into words and check if all appear in title
        district_words = normalized_district.split()
        if district_words:
            title_words = normalized_title.split()
            has_name_match = all(
                any(word in title_word for title_word in title_words)
                for word in district_words
            )

    # Determine score and strategy (HIGHER scores are better!)
    if has_suffix_match and (has_district_code or has_region_code):
        return 5, "suffix+code"  # Best match: district at end + code
    elif has_suffix_match:
        return 4, "suffix-only"  # Strong match: district at end
    elif has_district_code and has_name_match:
        return 3, "code+name"
    elif has_district_code or has_region_code:
        return 2, "code-aware"
    elif has_name_match:
        return 1, "name-only"
    else:
        return 0, "no-match"


def find_matching_questionnaire(
    district_name: str,
    district_code: str,
    region_code: str,
    source,
    config: Dict[str, Any],
    manual_questionnaire_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Find the best matching questionnaire for a PAA (District).

    Returns:
        (questionnaire_id, match_info)

    match_info contains:
        - questionnaire_title
        - questionnaire_version
        - matching_strategy
        - candidates_count
        - error (if any)
    """
    if manual_questionnaire_id:
        # Manual override - skip matching
        return manual_questionnaire_id, {
            "questionnaire_title": f"Manual Override: {manual_questionnaire_id}",
            "questionnaire_version": None,
            "matching_strategy": "manual-override",
            "candidates_count": 0,
            "error": None,
        }

    try:
        # Fetch questionnaires from HQ
        questionnaires = source.list_questionnaires()

        if not questionnaires:
            return None, {
                "questionnaire_title": None,
                "questionnaire_version": None,
                "matching_strategy": None,
                "candidates_count": 0,
                "error": "No questionnaires found in HQ",
            }

        # Score all questionnaires
        scored_questionnaires = []
        for q in questionnaires:
            score, strategy = _score_questionnaire_match(
                q, district_name, district_code, region_code, config
            )
            if score > 0:  # Only keep matches
                scored_questionnaires.append((score, strategy, q))

        if not scored_questionnaires:
            return None, {
                "questionnaire_title": None,
                "questionnaire_version": None,
                "matching_strategy": None,
                "candidates_count": len(questionnaires),
                "error": f"No questionnaire found for PAA '{district_name} ({district_code})'",
            }

        # Sort by score (highest first), then by version (latest first), then by last modified
        scored_questionnaires.sort(
            key=lambda x: (
                -x[0],  # Higher score first
                -int(x[2].get("Version", 0) or 0),  # Higher version first
                -(
                    x[2].get("LastEntryDate") or ""
                ),  # More recent first (string comparison)
            )
        )

        # Take the best match
        best_score, best_strategy, best_questionnaire = scored_questionnaires[0]

        questionnaire_id = best_questionnaire.get("Identity")
        if not questionnaire_id:
            # Fallback to constructing ID
            qid = best_questionnaire.get("Id")
            version = best_questionnaire.get("Version", 1)
            questionnaire_id = f"{qid}${version}" if qid else None

        return questionnaire_id, {
            "questionnaire_title": best_questionnaire.get("Title"),
            "questionnaire_version": best_questionnaire.get("Version"),
            "matching_strategy": best_strategy,
            "candidates_count": len(scored_questionnaires),
            "error": None,
        }

    except Exception as e:
        LOG.exception(
            "Failed to find matching questionnaire for PAA %s (%s)",
            district_name,
            district_code,
        )
        return None, {
            "questionnaire_title": None,
            "questionnaire_version": None,
            "matching_strategy": None,
            "candidates_count": 0,
            "error": str(e),
        }


def _cfg_get(cfg: Dict[str, Any], *names, default=None):
    for n in names:
        if n in cfg and cfg[n] not in (None, "", []):
            return cfg[n]
    return default


def _bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on")
    try:
        return bool(val)
    except Exception:
        return default


class SurveySolutionService(_BaseService):
    """
    Survey Solutions Export API → Adapter → (optional PMT) → Sink

    - Robust mapping from config → Source (base_url, workspace, api_prefix, meta_api_prefix, basic auth)
    - Merges HH header rows with roster rows into per-person rows before adapting
    """

    def __init__(
        self,
        self_user=None,  # kept positional-compat; prefer `user=`
        *,
        user=None,
        source=None,
        adapter=None,
        sink=None,
        config: Optional[Dict[str, Any]] = None,
        batch_size: int = 200,
    ):
        # prefer explicit user kwarg
        user = user if user is not None else self_user

        # Config + wiring
        self.config: Dict[str, Any] = config or C.__dict__
        self.source = source or Source()
        self.adapter = adapter or Adapter(config=self.config)
        self.batch_size = max(1, int(batch_size))

        self.sink = sink
        if self.sink is None and user is not None and Sink is not None:
            self.sink = Sink(user)

        LOG.info(
            "SurveySolutionService initialized with adapter=%s, sink=%s, batch_size=%s",
            type(self.adapter).__name__,
            type(self.sink).__name__ if self.sink else None,
            self.batch_size,
        )

        # Wire Source: base_url / workspace / api_prefix / meta_api_prefix / session headers & auth
        self._configure_source()

        # Optional: reuse last Completed HQ export on 5xx (if Source supports it)
        try:
            setattr(
                self.source,
                "reuse_latest_completed",
                _bool(_cfg_get(self.config, "export_reuse_latest_on_5xx"), False),
            )
        except Exception:
            LOG.exception(
                "Failed to set reuse_latest_completed on Source; continuing with defaults"
            )

    # ----------------------------- helpers -----------------------------

    def _configure_source(self):
        """
        Map config keys to Source attributes and session.
        - base_url <- export_base_url | base_url
        - workspace <- export_workspace | workspace  (also sets X-Workspace header)
        - api_prefix <- export_api_prefix | '/api/v2'
        - meta_api_prefix <- meta_api_prefix | '/api/v1' (if supported by Source)
        - basic auth <- auth_type/basic + username/password
        """
        src = self.source
        cfg = self.config

        base_url = _cfg_get(cfg, "export_base_url", "base_url")
        workspace = _cfg_get(cfg, "export_workspace", "workspace")
        api_prefix = _cfg_get(cfg, "export_api_prefix", default="/api/v2")
        meta_prefix = _cfg_get(cfg, "meta_api_prefix", default="/api/v1")

        for name, val in (
            ("base_url", base_url),
            ("workspace", workspace),
            ("api_prefix", api_prefix),
            ("meta_api_prefix", meta_prefix),
        ):
            if val and hasattr(src, name):
                try:
                    setattr(src, name, val)
                except Exception:
                    LOG.debug("Failed to set Source.%s=%r", name, val, exc_info=True)

        # Session wiring
        try:
            sess = getattr(src, "session", None) or getattr(src, "_session", None)
            if sess is None:
                import requests

                sess = requests.Session()
                try:
                    setattr(src, "session", sess)
                except Exception:
                    setattr(src, "_session", sess)

            # Headers
            if isinstance(getattr(sess, "headers", {}), dict):
                if workspace:
                    sess.headers.setdefault("X-Workspace", str(workspace))
                sess.headers.setdefault("Accept", "application/json")
                sess.headers.setdefault("Content-Type", "application/json")

            # Basic auth if configured
            at = (str(_cfg_get(cfg, "auth_type", default="basic")) or "basic").lower()
            if at == "basic":
                u = _cfg_get(cfg, "auth_basic_username")
                p = _cfg_get(cfg, "auth_basic_password")
                if u or p:
                    import requests

                    sess.auth = requests.auth.HTTPBasicAuth(u or "", p or "")
        except Exception:
            LOG.exception(
                "Failed to wire Source session/auth; continuing with defaults"
            )

    def _maybe_map_gender(self, g: Any) -> Any:
        try:
            m = self.config.get("adapter_gender_map")
            if isinstance(m, dict):
                return m.get(str(g), g)
        except Exception:
            LOG.debug("Gender mapping failed for value %r", g, exc_info=True)
        return g

    def _summary_file(self, rows: List[Dict[str, Any]], identifier: str):
        if not rows:
            return None
        seen = set()
        header: List[str] = []
        for r in rows:
            for k in r.keys():
                if str(k).startswith("_"):
                    continue
                if k not in seen:
                    seen.add(k)
                    header.append(k)
        projected = [{k: r.get(k) for k in header} for r in rows]
        return data_to_file(projected, identifier=identifier)

    # -------- roster/household merge --------

    def _extract_interview_key(self, row: Dict[str, Any]) -> Optional[str]:
        if not isinstance(row, dict):
            return None
        mapped = self.config.get("adapter_interview_key_field")
        candidates = [mapped, "Interview__Key", "interview__key", "interview_key"]
        lower_map = {str(k).lower(): k for k in row.keys()}
        for c in candidates:
            if not c:
                continue
            if c in row and row[c]:
                return str(row[c]).strip()
            k = lower_map.get(str(c).lower())
            if k and row.get(k):
                return str(row[k]).strip()
        return None

    def _is_person_row(self, row):
        if not isinstance(row, dict):
            return False

        keys = {str(k).lstrip("\ufeff").lower() for k in row.keys()}

        # Strong hints = person row
        strong = {
            "firstname",
            "first_name",
            "lastname",
            "last_name",
            "dob",
            "dateofbirth",
            "relationshiptohead",
            "relationship_to_head",
            "sex",
            "gender",
        }
        if keys & strong:
            return True

        # Reject rows like name__0, name__1 etc. (household rows)
        if any(k.startswith("name__") for k in keys):
            return False

        # If "name" exists but no "name__X", assume person
        if "name" in keys and not any("__" in k for k in keys if k.startswith("name")):
            return True

        return False

    def _merge_household_roster(
        self, all_rows: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Merge household header rows with person roster rows using interview key.

        - Skips metadata files (interview__, export__, assignment__)
        - Selects the richest household row
        - Combines each person row with its matching HH row
        """
        bucket: DefaultDict[str, Dict[str, Any]] = defaultdict(
            lambda: {"hh": None, "persons": []}
        )

        def richness(d: Dict[str, Any]) -> int:
            """Count number of non-empty scalar fields."""
            n = 0
            for v in d.values():
                if isinstance(v, (dict, list)):
                    continue
                if v is None:
                    continue
                if str(v).strip():
                    n += 1
            return n

        for r in all_rows:

            # 1. SKIP METADATA / NON-DATA TABS
            src_name = r.get("_source", "")
            if isinstance(src_name, str) and (
                src_name.startswith("interview__")
                or src_name.startswith("export__")
                or src_name.startswith("assignment__")
            ):
                continue

            # 2. Extract interview key
            ik = self._extract_interview_key(r)
            if not ik:
                LOG.debug(
                    "Row without Interview Key skipped: keys=%s", list(r.keys())[:10]
                )
                continue

            # 3. Person vs Household row classification
            if self._is_person_row(r):
                bucket[ik]["persons"].append(r)
            else:
                hh = bucket[ik]["hh"]
                # Pick the richest HH row
                if hh is None or richness(r) > richness(hh):
                    bucket[ik]["hh"] = r

        # 4. Combine HH row + each person row

        merged: List[Dict[str, Any]] = []

        for ik, grp in bucket.items():
            hh = grp.get("hh") or {}
            persons = grp.get("persons") or []
            if not persons:
                continue

            for p in persons:
                merged.append({**hh, **p})

        LOG.info(
            "Roster merge: %s interviews → %s merged person rows",
            len(bucket),
            len(merged),
        )

        return merged

    def build_preview_payload(self, records, source_name=None):
        """Return a UI-safe preview (scalars only) to avoid React errors on dict/list values."""
        from individual.apps import IndividualConfig  # safe import

        preview_fields = getattr(
            IndividualConfig,
            "workflow_preview_fields",
            [
                "first_name",
                "last_name",
                "dob",
                "location_code",
                "pmt_score",
                "pmt_class",
                "external_id",
            ],
        )

        preview_records = []
        for rec in records or []:
            row = {}
            for field in preview_fields:
                val = rec.get(field)
                if isinstance(val, (str, int, float, bool)) or val is None:
                    row[field] = val
                elif isinstance(val, (list, dict)):
                    row[field] = (
                        f"[{type(val).__name__}]"  # don’t send objects to React table
                    )
                else:
                    row[field] = str(val) if val is not None else None
            row["_source"] = source_name
            preview_records.append(row)

        return {
            "records": preview_records,
            "count": len(preview_records),
            "preview_fields": preview_fields,
        }

    # ------------------------------ run -------------------------------

    def run(
        self,
        *,
        questionnaire_id: Optional[str] = None,
        questionnaire_ids: Optional[List[str]] = None,
        tab_name_contains: Optional[str] = None,  # legacy name
        tab_filter: Optional[str] = None,  # preferred name
        include_meta: bool = False,
        from_dt: Optional[Any] = None,
        to_dt: Optional[Any] = None,
        dry_run: bool = False,
        yield_csv: bool = True,
        max_rows: Optional[int] = None,
        enrich_pmt: bool = False,
        hh_key: str = "interview_key",
        **source_overrides,
    ) -> Dict[str, Any]:
        cfg = self.config

        # Resolve QIDs
        qids: List[str] = []
        if questionnaire_ids:
            qids = list(questionnaire_ids)
        elif questionnaire_id:
            qids = [questionnaire_id]
        else:
            admin_list = cfg.get("export_questionnaire_ids")
            if isinstance(admin_list, (list, tuple)) and admin_list:
                qids = list(admin_list)
            else:
                q = cfg.get("questionnaire_id") or ""
                if q:
                    qids = [q]
        if not qids:
            raise ValueError(
                "No questionnaire id(s). Provide questionnaire_id / questionnaire_ids or set them in ApiEtlConfig."
            )

        # TAB FILTERING (fixed logic)

        # 1. CLI flag --tab always overrides
        if tab_filter:
            effective_tab = tab_filter.strip()

        # 2. No CLI tab → we filter ONLY metadata OUT, but keep all survey tabs
        elif tab_name_contains:
            effective_tab = tab_name_contains.strip()

        else:
            # Auto mode:
            #   Load all .tab survey data EXCEPT interview__, export__, assignment__ files
            effective_tab = None  # Means "no filter" at source level

        include_meta = bool(cfg.get("export_include_meta", include_meta))

        # Build Source *attributes* from overrides (do not pass unknown kwargs to .pull/.rows)
        base_url = source_overrides.get("base_url") or _cfg_get(
            cfg, "export_base_url", "base_url"
        )
        workspace = source_overrides.get("workspace") or _cfg_get(
            cfg, "export_workspace", "workspace"
        )
        if base_url:
            try:
                setattr(self.source, "base_url", base_url)
            except Exception:
                LOG.debug(
                    "Could not override Source.base_url to %r", base_url, exc_info=True
                )
        if workspace:
            try:
                setattr(self.source, "workspace", workspace)
            except Exception:
                LOG.debug(
                    "Could not override Source.workspace to %r",
                    workspace,
                    exc_info=True,
                )
            # refresh header if session exists
            try:
                sess = getattr(self.source, "session", None) or getattr(
                    self.source, "_session", None
                )
                if sess and isinstance(getattr(sess, "headers", {}), dict):
                    sess.headers["X-Workspace"] = str(workspace)
            except Exception:
                LOG.debug(
                    "Failed to refresh X-Workspace header for workspace=%r",
                    workspace,
                    exc_info=True,
                )

        LOG.info(
            "SurveySolutionService.run starting: qids=%s, tab_filter=%r, include_meta=%s, base_url=%r, workspace=%r, dry_run=%s",
            qids,
            effective_tab,
            include_meta,
            base_url,
            workspace,
            dry_run,
        )

        # Prepare call into Source (support either .pull or .rows)
        src_has_pull = hasattr(self.source, "pull")
        src_has_rows = hasattr(self.source, "rows")

        pull_args = dict(
            questionnaire_ids=qids if len(qids) > 1 else None,
            questionnaire_id=None if len(qids) > 1 else qids[0],
            tab_name_contains=effective_tab,
            include_meta=include_meta,
            yield_source_meta=True,
            from_dt=from_dt,
            to_dt=to_dt,
        )

        pull_args = {k: v for k, v in pull_args.items() if v not in (None, "")}

        LOG.debug("Source call args: %s", pull_args)

        if src_has_pull:
            rows_iter: Iterable[Dict[str, Any]] = self.source.pull(**pull_args)  # type: ignore
        elif src_has_rows:
            rows_iter = self.source.rows(**pull_args)  # type: ignore
        else:
            raise RuntimeError("Source must expose .pull(...) or .rows(...)")

        # Collect rows (respect max_rows short-circuit)
        all_raw: List[Dict[str, Any]] = []
        for raw in rows_iter:
            all_raw.append(raw)
            if max_rows and len(all_raw) >= max_rows:
                break
        total_raw = len(all_raw)

        LOG.info("SurveySolutionsExport: pulled %s raw row(s) from HQ", total_raw)

        # Merge HH + roster
        merged_raw = self._merge_household_roster(all_raw)
        total_merged = len(merged_raw)

        LOG.info(
            "SurveySolutionService: roster merge produced %s merged person row(s) from %s raw row(s)",
            total_merged,
            total_raw,
        )
        if total_raw and not total_merged:
            sample_keys = list(all_raw[0].keys())[:20] if all_raw else []
            LOG.warning(
                "Roster merge yielded zero person rows. "
                "This usually means interview key columns or person hints are missing. "
                "Check export_include_meta=True and adapter_interview_key_field in ApiEtlConfig. "
                "Example keys from first raw row: %s",
                sample_keys,
            )

        # Transform to Individual payloads (base + json_ext)
        transformed: List[Dict[str, Any]] = []
        per_qid_counts: Dict[str, int] = {}
        for idx, r in enumerate(merged_raw, start=1):
            try:
                x = self.adapter.transform(r)
            except Exception:
                LOG.exception(
                    "Adapter.transform failed for merged record #%s (interview_key=%r)",
                    idx,
                    self._extract_interview_key(r),
                )
                continue

            if not isinstance(x, dict):
                LOG.warning(
                    "Adapter.transform returned non-dict for record #%s (type=%s); skipping",
                    idx,
                    type(x),
                )
                continue

            if "gender" in x and x.get("gender") is not None:
                x["gender"] = self._maybe_map_gender(x.get("gender"))
            transformed.append(x)

            meta = r.get("_source") if isinstance(r, dict) else None
            if isinstance(meta, dict):
                q = meta.get("questionnaire_id")
                if q:
                    per_qid_counts[q] = per_qid_counts.get(q, 0) + 1

        total_xform = len(transformed)
        if total_xform == 0:
            LOG.error(
                "No transformed rows. Likely no person rows matched. Check _is_person_row() and tab filtering."
            )

        LOG.info(
            "SurveySolutionService: transformed %s row(s) (after roster merge %s → transformed %s).",
            total_xform,
            total_merged,
            total_xform,
        )

        # PMT enrichment (config default OR flag)
        do_enrich = _bool(cfg.get("enrich_pmt_default"), False) or bool(enrich_pmt)
        if do_enrich and transformed:
            try:
                transformed = enrich_rows_with_pmt(transformed, hh_key=hh_key)
                LOG.info("PMT enrichment complete (hh_key=%s)", hh_key)
            except Exception:
                LOG.exception("PMT enrichment failed; continuing without PMT.")
        elif do_enrich and not transformed:
            LOG.info(
                "PMT enrichment enabled but there are no transformed rows; skipping enrichment step."
            )

        # Push (chunked) if sink available and not dry-run
        batch_id = get_timestamped_batch_identifier(prefix="ss_individuals_")
        total_pushed = 0
        if self.sink and not dry_run and transformed:
            if self.batch_size <= 1:
                self.sink.push(transformed, batch_identifier=batch_id)
                total_pushed = len(transformed)
            else:
                for i in range(0, len(transformed), self.batch_size):
                    self.sink.push(
                        transformed[i : i + self.batch_size], batch_identifier=batch_id
                    )
                    total_pushed += len(transformed[i : i + self.batch_size])

        summary = {
            "questionnaires": qids,
            "tab_filter": effective_tab or "",
            "dry_run": dry_run or (self.sink is None),
            "rows_raw": total_raw,
            "rows_transformed": total_xform,
            "rows_pushed": total_pushed,
            "per_questionnaire_counts": per_qid_counts,
            "batch_identifier": batch_id,
            "pmt_enriched": bool(do_enrich),
        }

        file_obj = (
            self._summary_file(transformed, identifier=batch_id)
            if (yield_csv and transformed)
            else None
        )

        # UI-safe preview (no nested objects)
        preview = self.build_preview_payload(
            transformed, source_name="survey_solutions"
        )

        LOG.info("SurveySolutionService finished: %s", summary)
        return {
            "summary": summary,
            "rows": transformed,
            "file": file_obj,
            "preview": preview,
        }

    def run_paa_based_etl(
        self,
        *,
        district_name: str,
        district_code: str,
        region_code: str,
        manual_questionnaire_id: Optional[str] = None,
        dry_run: bool = False,
        user=None,
        **run_kwargs,
    ) -> Dict[str, Any]:
        """
        Run PAA-based ETL: find questionnaire by district/region, then run ETL.
        Creates PulledHistory record to track the run.

        Args:
            district_name: Display name of the district (PAA)
            district_code: District code for matching
            region_code: Region code for matching
            manual_questionnaire_id: Optional manual override for questionnaire ID
            dry_run: If True, don't actually import data
            user: User who initiated the run
            **run_kwargs: Additional arguments passed to self.run()

        Returns:
            Dict with:
                - questionnaire_match: Dict with matching info
                - etl_result: Result from self.run() if questionnaire found
                - pulled_history_id: ID of the created PulledHistory record
                - error: Error message if any
        """
        from api_etl.models import PulledHistory

        cfg = self.config

        # Find matching questionnaire
        questionnaire_id, match_info = find_matching_questionnaire(
            district_name=district_name,
            district_code=district_code,
            region_code=region_code,
            source=self.source,
            config=cfg,
            manual_questionnaire_id=manual_questionnaire_id,
        )

        result = {
            "questionnaire_match": {
                "district_name": district_name,
                "district_code": district_code,
                "region_code": region_code,
                "questionnaire_id": questionnaire_id,
                **match_info,
            },
            "etl_result": None,
            "pulled_history_id": None,
            "error": None,
        }

        if not questionnaire_id:
            error_msg = match_info.get("error", "No matching questionnaire found")
            result["error"] = error_msg

            # Create a failed history record
            if not dry_run:
                try:
                    history = PulledHistory.create_from_paa_etl_run(
                        paa_name=district_name,
                        district_code=district_code,
                        region_code=region_code,
                        questionnaire_match=match_info,
                        user=user,
                        status="failed",
                        error_message=error_msg,
                    )
                    result["pulled_history_id"] = history.id
                except Exception as e:
                    LOG.warning(
                        "Failed to create PulledHistory record for failed run: %s", e
                    )

            return result

        # Create PulledHistory record (in 'running' state)
        history = None
        if not dry_run:
            try:
                history = PulledHistory.create_from_paa_etl_run(
                    paa_name=district_name,
                    district_code=district_code,
                    region_code=region_code,
                    questionnaire_match=match_info,
                    user=user,
                    status="running",
                )
                result["pulled_history_id"] = history.id
            except Exception as e:
                LOG.warning("Failed to create PulledHistory record: %s", e)

        try:
            # Run ETL with the found questionnaire
            etl_result = self.run(
                questionnaire_id=questionnaire_id, dry_run=dry_run, **run_kwargs
            )

            result["etl_result"] = etl_result

            # Update history record with results
            if history and not dry_run:
                try:
                    history.update_counts_from_etl_result(etl_result)
                    history.status = "completed"

                    # Extract export metadata if available
                    summary = etl_result.get("summary", {})
                    if "batch_identifier" in summary:
                        history.run_metadata["batch_identifier"] = summary[
                            "batch_identifier"
                        ]

                    history.save()
                except Exception as e:
                    LOG.warning(
                        "Failed to update PulledHistory record with results: %s", e
                    )

            # Log the successful run
            LOG.info(
                "PAA-based ETL completed: PAA=%s (%s), questionnaire=%s, strategy=%s, rows=%d",
                district_name,
                district_code,
                questionnaire_id,
                match_info.get("matching_strategy"),
                etl_result.get("summary", {}).get("rows_transformed", 0),
            )

        except Exception as e:
            LOG.exception(
                "PAA-based ETL failed: PAA=%s (%s), questionnaire=%s",
                district_name,
                district_code,
                questionnaire_id,
            )
            result["error"] = str(e)

            # Update history record with error
            if history and not dry_run:
                try:
                    history.status = "failed"
                    history.error_message = str(e)
                    history.save()
                except Exception as update_error:
                    LOG.warning(
                        "Failed to update PulledHistory record with error: %s",
                        update_error,
                    )

        return result

    def execute(self):
        out = self.run()
        ok = bool(out and out.get("summary"))
        return {"success": ok, "message": "ok" if ok else "failed", "detail": out}


# Module-level convenience
def run_survey_solution_etl(**kwargs) -> Dict[str, Any]:
    user = kwargs.pop("user", None)
    svc = SurveySolutionService(user=user)
    return svc.run(**kwargs)


def run(**kwargs) -> Dict[str, Any]:
    return run_survey_solution_etl(**kwargs)
