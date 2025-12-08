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


def _current_cutoff(today: date | None = None) -> float | None:
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


def classify_pmt(score: float | int | str | None, *, today: date | None = None) -> str | None:
    if score is None:
        return None
    cut = _current_cutoff(today)
    if cut is None:
        return None
    try:
        s = float(score)
    except Exception:
        return None
    return "POOR" if s <= float(cut) else "NON_POOR"


def compute_household_pmt_score(hh: Dict[str, Any], members: List[Dict[str, Any]]) -> float:
    hsize = int(hh.get("household_size") or 0)
    assets = hh.get("assets_owned") or []
    urban = 1 if (str(hh.get("settlement_type") or "").strip().lower() == "urban") else 0

    n_15_64 = 0
    for m in members or []:
        a = _age_years(m.get("dob"))
        if a is not None and 15 <= a <= 64:
            n_15_64 += 1

    pmt = (
        11.688
        + (-0.10) * hsize
        + 0.250 * _ind(assets, 9)
        + 0.179 * _ind(assets, 5)
        + 0.367 * _ind(assets, 1)
        + 0.190 * _ind(assets, 22)
        + 0.055 * _ind(assets, 38)
        + 0.104 * _ind(assets, 18)
        + 0.045 * _ind(assets, 27)
        + 0.220 * _ind(assets, 25)
        + 0.259 * _ind(assets, 4)
        + 0.053 * _ind(assets, 37)
        + 0.029 * _ind(assets, 28)
        + 0.058 * _ind(assets, 41)
        + 0.013 * _ind(assets, 42)
        + 0.078 * _ind(assets, 43)
        + 0.050 * _ind(assets, 44)
        + 0.032 * _ind(assets, 12)
        + URBAN_COEF * urban
        + (-0.043) * n_15_64
    )
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
        score = compute_household_pmt_score(hh_row, members)
        cls = classify_pmt(score)

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
            r["json_ext"] = jx

            # Emit top-level too (preferred for CSV → Json_ext ingestion)
            r["pmt_score"] = score
            if cls is not None:
                r["pmt_class"] = cls

    return rows
