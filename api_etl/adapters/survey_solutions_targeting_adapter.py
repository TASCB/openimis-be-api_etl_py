from __future__ import annotations
from typing import Any, Dict, Optional

from api_etl.adapters.base import DataAdapter
from api_etl.utils import to_date_str


class SurveySolutionsTargetingAdapter(DataAdapter):

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        try:
            super().__init__()
        except TypeError:
            pass
        self.config: Dict[str, Any] = config or {}

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------
    @staticmethod
    def _get(record: Optional[Dict[str, Any]], *keys: Optional[str]) -> Any:
        if not record:
            return None
        lower_map = {(k or "").lstrip("\ufeff").lower(): v for k, v in record.items()}
        for k in keys:
            if not k:
                continue
            cleaned = (k or "").lstrip("\ufeff")
            if cleaned in record:
                return record[cleaned]
            v = lower_map.get(cleaned.lower())
            if v is not None:
                return v
        return None

    @staticmethod
    def _digits(val: Any) -> str:
        return "".join(ch for ch in str(val or "") if ch.isdigit())

    # --------------------------------------------------------
    # Relationship-to-head mapper
    # --------------------------------------------------------
    def _map_role(self, rth: Optional[str], gender: Optional[str]) -> Optional[str]:
        c = str(rth or "").strip()
        g = str(gender or "").strip().upper()

        if c == "1": return "HEAD"
        if c == "2": return "SPOUSE"

        if c in ("3", "4"):
            return "SON" if g == "M" else "DAUGHTER" if g == "F" else "OTHER RELATIVE"

        if c == "5":
            return "BROTHER" if g == "M" else "SISTER" if g == "F" else "OTHER RELATIVE"

        if c == "6":
            return "GRANDSON" if g == "M" else "GRANDDAUGHTER" if g == "F" else "OTHER RELATIVE"

        if c == "7":
            return "FATHER" if g == "M" else "MOTHER" if g == "F" else "OTHER RELATIVE"

        if c == "12":
            return "SPOUSE"

        return "OTHER RELATIVE"

    # --------------------------------------------------------
    # Transform record -> openIMIS Individual
    # --------------------------------------------------------
    def transform(self, record: Dict[str, Any], tag: Optional[str] = None) -> Dict[str, Any]:

        # ------------------- Names -------------------
        first_val = self._get(record, "firstname", "FirstName")
        last_val = self._get(record, "lastname", "LastName")

        if not first_val and not last_val:
            full = self._get(record, "name", "NAME")
            if isinstance(full, str):
                parts = full.strip().split()
                if len(parts) == 1:
                    first_val, last_val = parts[0], ""
                else:
                    first_val, last_val = parts[0], " ".join(parts[1:])

        # ------------------- DOB ---------------------
        dob_norm = to_date_str(self._get(record, "dob", "DATEOFBIRTH"))

        # ------------------- Gender ------------------
        g_raw = self._get(record, "gender", "SEX")
        gender_norm = str(g_raw or "").strip().upper()
        if gender_norm not in ("M", "F"):
            gender_norm = None

        # ------------------- Location Code (RRDDWWWVV) ------------------- 
        # Canonical code is 9 digits: RRDDWWWVV → pad with leading zero
        vc = self._digits(self._get(record, "VILLAGE_CODE"))
        wc = self._digits(self._get(record, "WARD_CODE"))

        loc_code_str = None

        if vc:
            # Main path: use village code, pad to 9 digits → RRDDWWWVV
            loc_code_str = vc.zfill(9)
        elif wc:
            # Fallback: if village missing but ward present, still keep 9-digit format
            # (Can be adjusted for a better rule for ward-only records)
            loc_code_str = wc.zfill(9)

        # ------------------- Location Name from DB --------------
        location_name_val = None
        if loc_code_str:
            try:
                from location.models import Location
                loc = Location.objects.filter(code=loc_code_str).first()
                if loc:
                    location_name_val = loc.name
            except Exception:
                pass

        # ------------------- Group Code -------------------
        tf4_raw = self._get(record, "TF4_NO")
        tf4_digits = self._digits(tf4_raw).zfill(4) if tf4_raw else None

        group_code_val = None
        if loc_code_str and tf4_digits:
            group_code_val = f"P3-{loc_code_str}{tf4_digits}"

        # ------------------- Role & HHREP -------------------
        rth_val = str(self._get(record, "RELATIONSHIPTOHEAD", "RTH") or "").strip()
        role_val = self._map_role(rth_val, gender_norm)
        hhrep_val = str(self._get(record, "HHREP") or "").strip()

        # ------------------- BASE (Authoritative Fields) -------------------
        base: Dict[str, Any] = {
            "first_name": first_val,
            "last_name": last_val,
            "dob": dob_norm,
            "gender": gender_norm,
            "location_code": loc_code_str,
            "location_name": location_name_val,
            "external_id": self._get(record, "interview__key"),
            "interview_key": self._get(record, "interview__key"),
            "group_code": group_code_val,
            "individual_role": role_val,
            "individual_role_code": rth_val,
            "hhrep": hhrep_val,
        }

        # =======================================
        # NO authoritative fields in json_ext • Only raw data + PMT placeholders + ss_batch
        # ==================================================================
        json_ext: Dict[str, Any] = {
            "pmt_score": None,
            "pmt_class": None,
            "raw": {}
        }

        # Copy ONLY non-authoritative raw scalar fields
        forbidden = {
            "location_code", "location_name", "group_code",
            "individual_role", "individual_role_code",
            "hhrep", "interview__key", "external_id"
        }

        for k, v in record.items():
            key = str(k).lstrip("\ufeff")
            if key not in forbidden:
                json_ext["raw"][key] = v

        if tag:
            json_ext["ss_batch"] = tag

        base["json_ext"] = json_ext
        return base
   
    
