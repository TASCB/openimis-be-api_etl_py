"""
api_etl/converters/targeting_converter.py

Tanzania-Specific Targeting Questionnaire Converter

==============================================================================
TANZANIA-SPECIFIC MODULE - TF4 Household Targeting Survey
==============================================================================

This converter transforms Survey Solutions targeting questionnaire data
into openIMIS Individual model instances for Tanzania's social protection
targeting program.

QUESTIONNAIRE: TF4 Household Targeting Survey
DATA SOURCE: Survey Solutions CAPI platform
TARGET: openIMIS Individual + Group modules

FEATURES:
- 70+ field mappings from TZ targeting questionnaire
- Household code generation (P3 pattern)
- Location code normalization (9-digit RRDDWWVVV)
- Relationship-to-head mapping
- Batch validation and filtering
- Configuration-driven field mappings

CONFIGURATION (via ModuleConfiguration):
- All adapter_*_field parameters from apps.py
- group_code_prefix, group_code_midfix
- adapter_gender_map, etc.

OTHER COUNTRIES:
Create your own converter for your questionnaire structure:
1. Extend BaseConverter
2. Implement to_individual(record, config)
3. Use config for field name mappings
4. Add country-specific validation

Example:
```python
class YourCountryConverter(BaseConverter):
    @classmethod
    def to_individual(cls, record, config):
        return Individual(
            first_name=record.get(config.get('adapter_first_name_field')),
            # ...
        )
```

Author: Tanzania Development Team
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import logging

from individual.models import Individual
from api_etl.converters.base import BaseConverter
from api_etl.utils import to_date_str  # Utility from existing api_etl

LOG = logging.getLogger(__name__)


class TargetingConverter(BaseConverter):
    """
    Convert TZ targeting questionnaire records to Individual instances.

    This converter:
    1. Maps 70+ TZ-specific fields to Individual model
    2. Builds household codes (P3 pattern)
    3. Normalizes location codes (9-digit)
    4. Maps relationship-to-head to individual roles
    5. Preserves all source data in json_ext
    """

    @classmethod
    def to_individual(
        cls,
        record: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None
    ) -> Optional[Individual]:
        """
        Convert one targeting questionnaire record to Individual.

        Args:
            record: Raw record from Survey Solutions export
            config: Configuration from ModuleConfiguration (ApiEtlConfig)

        Returns:
            Individual instance, or None if record should be skipped
        """
        cfg = config or {}

        # =================================================================
        # VALIDATION: Skip records without consent (TZ requirement)
        # =================================================================
        consent_field = cfg.get("adapter_consent_field", "consent")
        if not cls._get(record, consent_field):
            LOG.debug(f"Skipping record without consent: {record.get('interview__key')}")
            return None

        # =================================================================
        # CORE INDIVIDUAL FIELDS
        # =================================================================

        # Names
        first_name = cls._get_name_field(
            record,
            cfg.get("adapter_first_name_field", "firstname"),
            "FirstName", "firstname", "FIRSTNAME"
        )
        last_name = cls._get_name_field(
            record,
            cfg.get("adapter_last_name_field", "lastname"),
            "LastName", "lastname", "LASTNAME"
        )

        # Fallback: split full name if individual fields missing
        if not first_name and not last_name:
            first_name, last_name = cls._split_full_name(record)

        # Date of birth
        dob_field = cfg.get("adapter_dob_field", "dob")
        dob_raw = cls._get(record, dob_field, "dateOfBirth", "dob", "DOB")
        dob = to_date_str(dob_raw)

        # Gender (with mapping)
        gender_field = cfg.get("adapter_gender_field", "gender")
        gender_raw = cls._get(record, gender_field, "Gender", "gender", "SEX")
        gender_map = cfg.get("adapter_gender_map", {"1": "M", "2": "F"})
        gender = gender_map.get(str(gender_raw).strip(), gender_raw) if gender_raw else None
        gender = (str(gender or "").strip().upper() or None)
        if gender not in ("M", "F"):
            gender = None

        # =================================================================
        # LOCATION FIELDS
        # =================================================================

        location_code, location_name = cls._build_location(record, cfg)

        # =================================================================
        # HOUSEHOLD FIELDS (TZ-SPECIFIC)
        # =================================================================

        group_code = cls._build_group_code(record, cfg, location_code)
        individual_role, individual_role_code = cls._map_relationship_to_head(record, cfg, gender)
        hhrep = cls._get_household_representative(record, cfg)

        # =================================================================
        # CONTACT FIELDS
        # =================================================================

        phone = cls._get(record, cfg.get("adapter_phone_field", "phoneNumber"))
        email = cls._get(record, cfg.get("adapter_email_field", "email"))

        # =================================================================
        # IDENTIFIERS
        # =================================================================

        interview_key = cls._get(record, cfg.get("adapter_interview_key_field", "interview__key"))
        external_id = cls._get(record, cfg.get("adapter_external_id_field", "interview__key"))

        # =================================================================
        # BUILD JSON_EXT (NO-LOSS: All source data)
        # =================================================================

        json_ext = cls._build_json_ext(record, cfg, {
            "location_code": location_code,
            "location_name": location_name,
            "group_code": group_code,
            "individual_role": individual_role,
            "individual_role_code": individual_role_code,
            "hhrep": hhrep,
            "interview_key": interview_key,
            "external_id": external_id,
            # PMT placeholders (enriched later by workflow)
            "pmt_score": None,
            "pmt_class": None,
        })

        # =================================================================
        # CREATE INDIVIDUAL INSTANCE
        # =================================================================

        # Note: We return unsaved instances - sink handles saving
        individual = Individual(
            first_name=first_name,
            last_name=last_name,
            dob=dob,
            head=gender == "M",  # Simplified head detection
            json_ext=json_ext,
        )

        return individual

    # =====================================================================
    # HELPER METHODS (TZ-SPECIFIC LOGIC)
    # =====================================================================

    @staticmethod
    def _get(record: Dict[str, Any], *keys: str) -> Any:
        """
        Case-insensitive, BOM-tolerant dict getter.

        Checks exact key first, then case-insensitive map.
        """
        if not record:
            return None

        lower_map = {(k or "").lstrip("\ufeff").lower(): v for k, v in record.items()}

        for k in keys:
            if not k:
                continue

            k1 = (k or "").lstrip("\ufeff")

            # Try exact match first
            if k1 in record:
                return record[k1]

            # Try case-insensitive
            v = lower_map.get(k1.lower())
            if v is not None:
                return v

        return None

    @classmethod
    def _get_name_field(cls, record: Dict[str, Any], *keys: str) -> Optional[str]:
        """Get name field and clean it."""
        value = cls._get(record, *keys)
        if not value:
            return None
        return str(value).strip() or None

    @classmethod
    def _split_full_name(cls, record: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """Split full name into first/last if individual fields missing."""
        full_name = cls._get(record, "name", "NAME", "full_name")
        if not isinstance(full_name, str):
            return None, None

        full_name = full_name.strip()
        if not full_name:
            return None, None

        # Handle "Last, First" format
        if "," in full_name:
            parts = [p.strip() for p in full_name.split(",", 1)]
            if len(parts) == 2:
                return parts[1] or None, parts[0] or None

        # Handle "First Last" format
        tokens = full_name.split()
        if len(tokens) == 1:
            return tokens[0], ""
        else:
            return tokens[0], " ".join(tokens[1:])

    @classmethod
    def _build_location(
        cls,
        record: Dict[str, Any],
        config: Dict[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Build TZ location code (9-digit RRDDWWVVV) and name.

        Returns:
            (location_code, location_name) tuple
        """
        # Try direct location_code field
        loc_code_field = config.get("adapter_location_code_field", "locationCode")
        loc_code = cls._get(record, loc_code_field, "locationcode", "LocationCode", "VILLAGE_CODE")
        loc_code_str = (str(loc_code or "")).strip()

        # If no direct code, build from parts
        if not loc_code_str:
            rc = cls._digits(cls._get(record, config.get("adapter_region_code_field", "region_code"), "REGION_CODE"))
            dc = cls._digits(cls._get(record, config.get("adapter_district_code_field", "district_code"), "DISTRICT_CODE"))
            wc = cls._digits(cls._get(record, config.get("adapter_ward_code_field", "ward_code"), "WARD_CODE"))
            vc = cls._digits(cls._get(record, config.get("adapter_village_code_field", "village_code"), "VILLAGE_CODE"))

            if wc and vc:
                if len(wc) == 6 and wc.isdigit():
                    loc_code_str = wc + vc.zfill(3)
                else:
                    if rc or dc:
                        loc_code_str = rc.zfill(2) + dc.zfill(2) + wc.zfill(2) + vc.zfill(3)
                    else:
                        loc_code_str = wc.zfill(2) + vc.zfill(3)

        # Pad to 9 digits if numeric
        if loc_code_str and loc_code_str.isdigit():
            loc_code_str = loc_code_str.zfill(9)

        # Location name
        loc_name_field = config.get("adapter_location_name_field", "locationName")
        loc_name = cls._get(record, loc_name_field, "locationname", "LocationName", "VILLAGENAME")

        # Try to resolve name from database if missing/numeric
        if (not loc_name or str(loc_name).isdigit()) and loc_code_str:
            try:
                from location.models import Location
                loc_obj = Location.objects.filter(code=loc_code_str).first()
                if loc_obj:
                    loc_name = loc_obj.name
            except Exception:
                pass

        return loc_code_str or None, loc_name

    @classmethod
    def _build_group_code(
        cls,
        record: Dict[str, Any],
        config: Dict[str, Any],
        location_code: Optional[str]
    ) -> Optional[str]:
        """
        Build TZ household code: P3 + location + 000 + TF4_NO

        Format: P3{9-digit-location}000{4-digit-TF4}
        Example: P3010101001000120001
        """
        tf4_field = config.get("adapter_tf4_no_field", "tf4_no")
        tf4_raw = cls._get(record, tf4_field, "TF4_NO")
        tf4_digits = cls._digits(tf4_raw)
        tf4_part = tf4_digits.zfill(4) if tf4_digits else ""

        prefix = config.get("group_code_prefix", "P3")
        midfix = config.get("group_code_midfix", "000")

        if location_code and tf4_part:
            return f"{prefix}{location_code}{midfix}{tf4_part}"
        elif location_code:
            return f"{prefix}{location_code}"
        else:
            return None

    @classmethod
    def _map_relationship_to_head(
        cls,
        record: Dict[str, Any],
        config: Dict[str, Any],
        gender: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Map TZ relationship-to-head codes to openIMIS roles.

        Returns:
            (role_label, role_code) tuple

        TZ Codes:
        1 = HEAD
        2 = SPOUSE
        3 = SON/DAUGHTER
        4 = STEPCHILD
        5 = SIBLING
        6 = GRANDCHILD
        7 = PARENT
        12 = CO-WIFE
        etc.
        """
        rth_field = config.get("adapter_relationship_to_head_field", "RTH")
        rth_code = str(cls._get(record, rth_field, "RELATIONSHIPTOHEAD", "relationship_to_head") or "").strip()

        if not rth_code:
            return None, None

        # Map to role label
        g = (gender or "").upper()
        role_label = None

        if rth_code == "1":
            role_label = "HEAD"
        elif rth_code == "2":
            role_label = "SPOUSE"
        elif rth_code in ("3", "4"):  # Son/Daughter/Stepchild
            if g == "M":
                role_label = "SON"
            elif g == "F":
                role_label = "DAUGHTER"
            else:
                role_label = "OTHER RELATIVE"
        elif rth_code == "5":  # Sibling
            if g == "M":
                role_label = "BROTHER"
            elif g == "F":
                role_label = "SISTER"
            else:
                role_label = "OTHER RELATIVE"
        elif rth_code == "6":  # Grandchild
            if g == "M":
                role_label = "GRANDSON"
            elif g == "F":
                role_label = "GRANDDAUGHTER"
            else:
                role_label = "OTHER RELATIVE"
        elif rth_code == "7":  # Parent
            if g == "M":
                role_label = "FATHER"
            elif g == "F":
                role_label = "MOTHER"
            else:
                role_label = "OTHER RELATIVE"
        elif rth_code == "12":  # Co-wife
            role_label = "SPOUSE"
        elif rth_code in ("8", "10", "11", "13"):
            role_label = "OTHER RELATIVE"
        elif rth_code == "14":
            role_label = "NOT RELATED"
        else:
            role_label = "OTHER RELATIVE"

        return role_label, rth_code

    @classmethod
    def _get_household_representative(
        cls,
        record: Dict[str, Any],
        config: Dict[str, Any]
    ) -> Optional[str]:
        """Get HH representative code."""
        hhrep_field = config.get("adapter_hhrep_field", "HHREP")
        hhrep = cls._get(record, hhrep_field, "HHREP", "household_representative")
        return str(hhrep).strip() if hhrep else None

    @classmethod
    def _build_json_ext(
        cls,
        record: Dict[str, Any],
        config: Dict[str, Any],
        core_fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Build json_ext with no-loss preservation of source data.

        Strategy:
        1. Copy all scalar fields from source (no nested dicts/lists)
        2. Overwrite with normalized core fields
        3. Add PMT placeholders
        """
        json_ext: Dict[str, Any] = {}

        # Copy all scalar source fields
        for k, v in record.items():
            if isinstance(v, (dict, list)):
                continue  # Skip nested structures
            kl = str(k).lstrip("\ufeff")
            json_ext[kl] = v

        # Overwrite with normalized values
        json_ext.update(core_fields)

        return json_ext

    @staticmethod
    def _digits(val: Any) -> str:
        """Extract only digits from value."""
        return "".join(ch for ch in str(val or "") if ch.isdigit())

    # =====================================================================
    # VALIDATION
    # =====================================================================

    @classmethod
    def validate_record(cls, record: Dict[str, Any]) -> bool:
        """
        Validate TZ targeting questionnaire record.

        Returns False to skip record.
        """
        # Must have interview key
        if not cls._get(record, "interview__key", "Interview__Key"):
            LOG.warning("Record missing interview__key, skipping")
            return False

        # Must have at least first or last name
        if not (cls._get(record, "firstname", "FirstName") or cls._get(record, "lastname", "LastName")):
            if not cls._get(record, "name", "NAME"):
                LOG.warning("Record missing name fields, skipping")
                return False

        return True


# Backwards compatibility alias
SurveySolutionsTargetingConverter = TargetingConverter
