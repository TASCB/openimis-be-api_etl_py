from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List
import json
import ast
import logging

from api_etl.apps import ApiEtlConfig as C

LOG = logging.getLogger(__name__)

# ---- Config knobs (read from ModuleConfiguration('api_etl')) ----
URBAN_COEF: float = float(getattr(C, "pmt_urban_coef", 0.0))
DEFAULT_HH_KEY: str = str(getattr(C, "pmt_household_key", "interview_key") or "interview_key")
PMT_CUTOFF: float | None = float(getattr(C, "pmt_cutoff", 11.01)) if getattr(C, "pmt_cutoff", 11.01) != "" else None
PMT_CUTOFF_SCHEDULE: list[dict] = list(getattr(C, "pmt_cutoff_schedule", []) or [])


# ---- Default formula (used when no PmtGlobalFormula is configured) ----
# These are the historical hard-coded coefficients. They remain the fallback so
# behaviour is identical until an admin configures + a checker approves a formula.
DEFAULT_COEFFS: Dict[str, Any] = {
    "intercept": 11.688,
    "household_size_coef": -0.10,
    "working_age_coef": -0.043,
    "urban_coef": float(URBAN_COEF),
    "cutoff": float(PMT_CUTOFF) if PMT_CUTOFF is not None else 11.01,
    "assets": {
        "9": 0.250, "5": 0.179, "1": 0.367, "22": 0.190, "38": 0.055,
        "18": 0.104, "27": 0.045, "25": 0.220, "4": 0.259, "37": 0.053,
        "28": 0.029, "41": 0.058, "42": 0.013, "43": 0.078, "44": 0.050,
        "12": 0.032,
    },
}


def get_active_coeffs() -> Dict[str, Any]:
    """
    Resolve the effective formula: the active ``PmtGlobalFormula`` if one exists,
    otherwise ``DEFAULT_COEFFS``. Best-effort — any failure (no DB, individual not
    installed, no row) falls back to the defaults so scoring never breaks.

    Note: this performs a DB read. Callers in hot loops should resolve it ONCE and
    pass the dict into ``compute_household_pmt_score(..., coeffs=...)``.
    """
    try:
        from individual.models import PmtGlobalFormula
        active = PmtGlobalFormula.get_active()
        if active is not None:
            return active.as_coeffs()
    except Exception:
        pass
    return DEFAULT_COEFFS


def _age_years(dob: Any) -> int | None:
    if not dob:
        return None
    try:
        if isinstance(dob, date):
            y, m, d = dob.year, dob.month, dob.day
        else:
            y, m, d = map(int, str(dob)[:10].split("-"))
        today = date.today()
        return today.year - y - ((today.month, today.day) < (m, d))
    except Exception:
        return None


def _to_list(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val]
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return [str(x).strip() for x in json.loads(s)]
            except Exception:
                try:
                    return [str(x).strip() for x in ast.literal_eval(s)]
                except Exception:
                    pass
        return [s]
    return [str(val).strip()]


def _ind(assets_owned: Any, k: int | str) -> int:
    items = {x.lower() for x in _to_list(assets_owned)}
    return 1 if str(k).lower() in items else 0


def _parse_date(d: Any) -> date | None:
    if isinstance(d, date):
        return d
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _current_cutoff(today: date | None = None, coeffs: Dict[str, Any] | None = None) -> float | None:
    # The active global formula's cutoff wins when configured.
    src = coeffs if coeffs is not None else get_active_coeffs()
    cutoff = src.get("cutoff") if isinstance(src, dict) else None
    if cutoff is not None:
        try:
            return float(cutoff)
        except (TypeError, ValueError):
            pass
    if PMT_CUTOFF is not None:
        return PMT_CUTOFF
    if not PMT_CUTOFF_SCHEDULE:
        return None
    t = today or date.today()
    effective: list[tuple[date, float]] = []
    for item in PMT_CUTOFF_SCHEDULE:
        d = _parse_date(item.get("start_date"))
        c = item.get("cutoff")
        try:
            c = float(c)
        except Exception:
            c = None
        if d and c is not None and d <= t:
            effective.append((d, c))
    if not effective:
        return None
    effective.sort(key=lambda x: x[0])
    return effective[-1][1]


def classify_pmt(
    score: float | int | str | None,
    *,
    today: date | None = None,
    coeffs: Dict[str, Any] | None = None,
) -> str | None:
    if score is None:
        return None
    cut = _current_cutoff(today, coeffs=coeffs)
    if cut is None:
        return None
    try:
        s = float(score)
    except Exception:
        return None
    return "POOR" if s <= float(cut) else "NON_POOR"


def survey_answers(source: Dict[str, Any]) -> Dict[str, Any]:
    raw = source.get("raw")
    if isinstance(raw, dict):
        return raw
    nested = source.get("json_ext")
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except Exception:
            nested = None
    if isinstance(nested, dict) and isinstance(nested.get("raw"), dict):
        return nested["raw"]
    return {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def household_pmt_inputs(source: Dict[str, Any], members_count: int | None = None) -> Dict[str, Any]:
    source = source or {}
    raw = survey_answers(source)

    size = _int_or_none(source.get("household_size")) or members_count
    if not size:
        size = _int_or_none(raw.get(getattr(C, "pmt_household_size_field", "hh_size"))) or 0

    assets = source.get("assets_owned") or raw.get("assets_owned")
    if not assets:
        prefix = getattr(C, "pmt_asset_field_prefix", "hh_asset__")
        assets = [
            key[len(prefix):] for key, value in raw.items()
            if key.startswith(prefix) and _int_or_none(value) == 1
        ]

    settlement = source.get("settlement_type") or raw.get("settlement_type")
    if not settlement:
        location = raw.get(getattr(C, "pmt_location_field", "hh_location"))
        if location not in (None, ""):
            urban_values = {str(v).strip() for v in getattr(C, "pmt_urban_location_values", ["1"]) or []}
            settlement = "urban" if str(location).strip() in urban_values else "rural"

    return {"household_size": size, "assets_owned": assets, "settlement_type": settlement or "rural"}


def compute_household_pmt_score(
    hh: Dict[str, Any],
    members: List[Dict[str, Any]],
    coeffs: Dict[str, Any] | None = None,
) -> float:
    """
    Compute the household PMT score from the configured coefficients.

    ``coeffs`` is the dict returned by ``get_active_coeffs`` (or
    ``PmtGlobalFormula.as_coeffs``). When omitted it is resolved on demand — fine
    for one-off calls, but hot loops should resolve it once and pass it in.
    """
    if coeffs is None:
        coeffs = get_active_coeffs()

    inputs = household_pmt_inputs(hh, members_count=len(members or []))
    hsize = inputs["household_size"]
    assets = inputs["assets_owned"]
    urban = 1 if str(inputs["settlement_type"]).strip().lower() == "urban" else 0

    n_15_64 = 0
    for m in members or []:
        a = _age_years(m.get("dob"))
        if a is not None and 15 <= a <= 64:
            n_15_64 += 1

    def _f(key: str) -> float:
        try:
            return float(coeffs.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    pmt = _f("intercept")
    pmt += _f("household_size_coef") * hsize
    for code, coef in (coeffs.get("assets") or {}).items():
        try:
            pmt += float(coef) * _ind(assets, code)
        except (TypeError, ValueError):
            continue
    pmt += _f("urban_coef") * urban
    pmt += _f("working_age_coef") * n_15_64

    return round(float(pmt), 3)


def enrich_rows_with_pmt(
    rows: List[Dict[str, Any]],
    * ,
    hh_key: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Group by household key (default: 'interview_key'), compute a single PMT per household,
    and attach to EVERY member:
      - json_ext['pmt_score'] (float)
      - json_ext['pmt_class'] ('POOR' / 'NON_POOR')
      - ALSO emit top-level columns 'pmt_score' / 'pmt_class' for CSV (these go into Json_ext automatically)
    """
    if not rows:
        return rows

    key_name = (hh_key or DEFAULT_HH_KEY or "interview_key")
    # Resolve the active formula once for the whole batch (avoids a query per household).
    coeffs = get_active_coeffs()
    cutoff = _current_cutoff(coeffs=coeffs)

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        k = None
        for nm in (key_name, "interview_key", "group_code"):
            v = r.get(nm)
            if v:
                s = str(v).strip()
                if s:
                    k = s
                    break
        if not k:
            continue
        groups.setdefault(k, []).append(r)

    for _, members in groups.items():
        if not members:
            continue
        hh_row = members[0]
        score = compute_household_pmt_score(hh_row, members, coeffs)
        cls = classify_pmt(score, coeffs=coeffs)

        for r in members:
            jx = r.get("json_ext")
            if isinstance(jx, str):
                try:
                    jx = json.loads(jx) if jx.strip() else {}
                except Exception:
                    jx = {}
            if not isinstance(jx, dict):
                jx = {}

            jx["pmt_score"] = score
            if cls is not None:
                jx["pmt_class"] = cls
            if cutoff is not None:
                jx["pmt_cutoff_used"] = float(cutoff)
            r["json_ext"] = jx

            # Emit top-level too (preferred for CSV → Json_ext ingestion)
            r["pmt_score"] = score
            if cls is not None:
                r["pmt_class"] = cls
            if cutoff is not None:
                r["pmt_cutoff_used"] = float(cutoff)

    return rows
