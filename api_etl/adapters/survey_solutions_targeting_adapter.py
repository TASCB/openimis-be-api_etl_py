from __future__ import annotations
from typing import Any, Dict, Optional

from api_etl.adapters.base import DataAdapter
from api_etl.utils import to_date_str  # normalize DOB to YYYY-MM-DD


class SurveySolutionsTargetingAdapter(DataAdapter):
    """
    Map one Survey Solutions interview row -> openIMIS Individual dict.

    - Emits canonical base fields as flat columns so the sink can write them into CSV.
    - Preserves "all other" scalar fields in json_ext (flat, no nested dicts) for no-loss behavior.
    - Adds household fields:
        - group_code
        - individual_role (string label, e.g. 'HEAD', 'SPOUSE', ...)
        - individual_role_code (numeric string from RTH, e.g. '1', '2', ...)
        - hhrep (numeric string that points to the member's role code who is the household representative)
    - Ensures PMT placeholders exist in json_ext:
        - json_ext['pmt_score'] (None until enriched in workflow)
        - json_ext['pmt_class'] (None until enriched in workflow)
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        try:
            super().__init__()
        except TypeError:
            # Some environments have a no-arg base
            pass
        self.config: Dict[str, Any] = config or {}

    @staticmethod
    def _get(record: Optional[Dict[str, Any]], *keys: Optional[str]) -> Any:
        """
        Case/UTF BOM tolerant getter for dict-like 'record'.
        Checks exact key, then case-insensitive map.
        """
        if not record:
            return None
        lower_map = {(k or "").lstrip("\ufeff").lower(): v for k, v in record.items()}
        for k in keys:
            if not k:
                continue
            k1 = (k or "").lstrip("\ufeff")
            if k1 in record:
                return record[k1]
            v = lower_map.get(k1.lower())
            if v is not None:
                return v
        return None

    @staticmethod
    def _digits(val: Any) -> str:
        return "".join(ch for ch in str(val or "") if ch.isdigit())

    # ----- Role mapper (Survey Solutions RELATIONSHIPTOHEAD → GroupIndividual.Role) -----
    def _map_role(self, rth: Optional[str], gender: Optional[str]) -> Optional[str]:
        c = (str(rth or "").strip())
        g = (str(gender or "").strip().upper())

        if not c:
            return None
        if c == "1":   # Head
            return "HEAD"
        if c == "2":   # Spouse
            return "SPOUSE"

        if c in ("3", "4"):  # Son/Daughter or Stepchild
            if g == "M": return "SON"
            if g == "F": return "DAUGHTER"
            return "OTHER RELATIVE"

        if c == "5":  # Sibling
            if g == "M": return "BROTHER"
            if g == "F": return "SISTER"
            return "OTHER RELATIVE"

        if c == "6":  # Grandchild
            if g == "M": return "GRANDSON"
            if g == "F": return "GRANDDAUGHTER"
            return "OTHER RELATIVE"

        if c == "7":  # Parent
            if g == "M": return "FATHER"
            if g == "F": return "MOTHER"
            return "OTHER RELATIVE"

        if c == "12":  # Co-wife
            return "SPOUSE"

        if c in ("8", "10", "11", "13"):
            return "OTHER RELATIVE"
        if c in ("14", "15"):
            return "NOT RELATED" if c == "14" else "OTHER RELATIVE"

        return "OTHER RELATIVE"

    def transform(self, record: Dict[str, Any], tag: Optional[str] = None) -> Dict[str, Any]:
        cfg = self.config or {}

        # ---- Configured field keys (defaults should match your headers) ----
        first_name_key   = cfg.get("adapter_first_name_field", "firstname")
        last_name_key    = cfg.get("adapter_last_name_field", "lastname")
        dob_key          = cfg.get("adapter_dob_field", "dob")
        gender_key       = cfg.get("adapter_gender_field", "gender")

        loc_name_key     = cfg.get("adapter_location_name_field", "locationName")
        loc_name_fields  = cfg.get("adapter_location_name_fields")
        loc_code_key     = cfg.get("adapter_location_code_field", "locationCode")
        phone_key        = cfg.get("adapter_phone_field", "phoneNumber")
        email_key        = cfg.get("adapter_email_field", "email")
        external_id_key  = cfg.get("adapter_external_id_field", "interview__key")
        interview_key_key = cfg.get("adapter_interview_key_field", "interview__key")

        # Relationship-to-Head & HH Representative numeric codes (strings like '1','2',...)
        rth_key   = cfg.get("adapter_role_to_head_field",
                     cfg.get("adapter_relationship_to_head_field", "RTH"))
        hhrep_key = cfg.get("adapter_hhrep_field", "HHREP")

        # Optional recipient_info field name
        recipient_info_field = cfg.get("adapter_recipient_info_field", "recipient_info")

        # Location parts (optional) for code composition
        region_code_key   = cfg.get("adapter_region_code_field", "region_code")
        district_code_key = cfg.get("adapter_district_code_field", "district_code")
        ward_code_key     = cfg.get("adapter_ward_code_field", "ward_code")
        village_code_key  = cfg.get("adapter_village_code_field", "village_code")

        # Household code builder (TF4 number segment, optional)
        tf4_no_key  = cfg.get("adapter_tf4_no_field", "tf4_no")

        # ---- Normalize DOB & Gender ----
        dob_raw  = self._get(record, dob_key, "dateOfBirth", "dob")
        dob_norm = to_date_str(dob_raw)

        gender_raw  = self._get(record, gender_key, "Gender", "gender", "SEX")
        gmap        = cfg.get("adapter_gender_map") or {}
        gender_norm = gmap.get(str(gender_raw).strip(), gender_raw) if gender_raw is not None else None
        gender_norm = (str(gender_norm or "").strip().upper() or None)
        if gender_norm not in ("M", "F"):
            gender_norm = None

        # ---- Build location_name from configured parts; fallback to single field ----
        parts: list[str] = []
        if isinstance(loc_name_fields, (list, tuple)) and loc_name_fields:
            for f in loc_name_fields:
                v = self._get(record, f)
                if v:
                    s = str(v).strip()
                    if s:
                        parts.append(s)
        location_name_val = parts[-1] if parts else None
        if not location_name_val:
            location_name_val = self._get(
                record, loc_name_key, "locationname", "LocationName",
                "VILLAGENAME", "WardName", "WARDNAME"
            )

        # ---- Build/pad location_code (prefer direct, else compose) ----
        loc_code_val = self._get(record, loc_code_key, "locationcode", "LocationCode", "VILLAGE_CODE")
        loc_code_str = (str(loc_code_val or "")).strip()

        if not loc_code_str:
            rc = self._digits(self._get(record, region_code_key, "REGION_CODE"))
            dc = self._digits(self._get(record, district_code_key, "DISTRICT_CODE"))
            wc = self._digits(self._get(record, ward_code_key, "WARD_CODE"))
            vc = self._digits(self._get(record, village_code_key, "VILLAGE_CODE"))
            if wc and vc:
                if len(wc) == 6 and wc.isdigit():
                    loc_code_str = wc + vc.zfill(3)
                else:
                    if rc or dc:
                        loc_code_str = rc.zfill(2) + dc.zfill(2) + wc.zfill(2) + vc.zfill(3)
                    else:
                        loc_code_str = wc.zfill(2) + vc.zfill(3)
            elif rc and dc and vc:
                w2 = self._digits(self._get(record, ward_code_key, "WARD_CODE"))
                if w2:
                    loc_code_str = rc.zfill(2) + dc.zfill(2) + w2.zfill(2) + vc.zfill(3)

        if loc_code_str.isdigit():
            loc_code_str = loc_code_str.zfill(9)  # RRDDWWVVV

        # ---- Fix location_name from DB if missing or numeric-like, best effort ----
        try:
            looks_numeric = (str(location_name_val or "").strip().isdigit())
            if (not location_name_val or looks_numeric) and loc_code_str:
                from location.models import Location
                loc_obj = Location.objects.filter(code=loc_code_str).first()
                if loc_obj:
                    location_name_val = loc_obj.name
        except Exception:
            pass

        # ---- Derive group_code ----
        tf4_raw    = self._get(record, tf4_no_key, "TF4_NO")
        tf4_digits = self._digits(tf4_raw)
        tf4_part   = tf4_digits.zfill(4) if tf4_digits else ""
        prefix     = (self.config or {}).get("group_code_prefix", "P3")
        midfix     = (self.config or {}).get("group_code_midfix", "000")
        group_code_val = (
            f"{prefix}{loc_code_str}{midfix}{tf4_part}"
            if (loc_code_str and tf4_part)
            else (f"{prefix}{loc_code_str}" if loc_code_str else None)
        )

        # ---- Relationship-to-head (numeric code) and role label ----
        rth_val  = str(self._get(record, rth_key, "RELATIONSHIPTOHEAD") or "").strip()
        role_val = self._map_role(rth_val, gender_norm)

        # ---- HH Representative numeric code (string) ----
        hhrep_val = str(self._get(record, hhrep_key, "HHREP") or "").strip()

        # ---- Names (robust 'name' fallback) ----
        first_val = self._get(record, first_name_key, "FirstName", "firstname", "FIRSTNAME")
        last_val  = self._get(record,  last_name_key,  "LastName",  "lastname",  "LASTNAME")
        if (not first_val) and (not last_val):
            full_name = self._get(record, "name", "NAME")
            if isinstance(full_name, str):
                full_name = full_name.strip()
                if full_name:
                    if "," in full_name:
                        p = [p.strip() for p in full_name.split(",", 1)]
                        if len(p) == 2:
                            last_val, first_val = p[0] or None, p[1] or None
                    else:
                        tokens = full_name.split()
                        if len(tokens) == 1:
                            first_val, last_val = tokens[0], ""
                        else:
                            first_val, last_val = tokens[0], " ".join(tokens[1:])

        # ---- Identifiers ----
        interview_key_val = self._get(record, interview_key_key, "Interview__Key", "interview__key")
        external_id_val   = self._get(record, external_id_key,   "Interview__Key", "interview__key")
        external_id_val   = str(external_id_val).strip() if external_id_val else None

        # ---- Canonical base fields (flat) ----
        base: Dict[str, Any] = {
            "first_name":     first_val,
            "last_name":      last_val,
            "dob":            dob_norm,
            "gender":         gender_norm,
            "location_name":  location_name_val,
            "location_code":  loc_code_str,  # already zfilled(9)
            "phone":          self._get(record, phone_key, "phonenumber", "PhoneNumber"),
            "email":          self._get(record, email_key, "Email", "email"),
            "interview_key":  interview_key_val,
            "external_id":    external_id_val,
            # household:
            "group_code":            group_code_val,
            "individual_role":       role_val,        # string label
            "individual_role_code":  rth_val,         # numeric string ('1','2',...)
            "hhrep":                 hhrep_val,       # numeric string ('1','2',...)
        }

        # --------------------------------------------------------------------
        # Build a FLAT json_ext (SCALAR-ONLY):
        # - First copy all scalar raw values so we don't lose anything
        # - Then overwrite with normalized values for authoritative fields-
        json_ext: Dict[str, Any] = {}
        for k, v in record.items():
            # keep only scalars (str, int, float, bool, None); skip dict/list
            if isinstance(v, (dict, list)):
                continue
            kl = str(k).lstrip("\ufeff")
            json_ext[kl] = v

        # Overwrite with normalized/derived values so json_ext carries the clean values
        if loc_code_str is not None:
            json_ext["location_code"] = loc_code_str
        if location_name_val is not None:
            json_ext["location_name"] = location_name_val
        if group_code_val is not None:
            json_ext["group_code"] = group_code_val
        if interview_key_val is not None:
            json_ext["interview_key"] = interview_key_val
        if external_id_val is not None:
            json_ext["external_id"] = external_id_val
        if role_val is not None:
            json_ext["individual_role"] = role_val
        if rth_val is not None:
            json_ext["individual_role_code"] = rth_val
        if hhrep_val is not None:
            json_ext["hhrep"] = hhrep_val

        # PMT placeholders (kept inside json_ext)
        json_ext.setdefault("pmt_score", None)
        json_ext.setdefault("pmt_class", None)

        # ETL Batch Tagging - Add ss_batch for history tracking following openIMIS patterns
        if tag:
            json_ext["ss_batch"] = tag

        # Attach to base
        base["json_ext"] = json_ext
        return base
