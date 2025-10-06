# api_etl/tests/test_adapter.py
from __future__ import annotations

import unittest
from api_etl.adapters.survey_solutions_targeting_adapter import SurveySolutionsTargetingAdapter


class SurveySolutionsTargetingAdapterTests(unittest.TestCase):
    def test_transform_roster_minimal_with_config_mapping(self):
        # Configure adapter to read roster-style columns and normalize DOB
        cfg = {
            "adapter_first_name_field": "name",
            "adapter_last_name_field": None,                 # not present in roster; expect None
            "adapter_dob_field": "DATEOFBIRTH",
            "adapter_gender_field": "SEX",                   # roster uses SEX; map via config
            "adapter_external_id_field": "interview__key",
            "adapter_relationship_to_head_field": "RELATIONSHIPTOHEAD",
            "adapter_current_grade_field": "GRADE",
            "adapter_school_district_code_field": "SCHOOL_DISTRICT_CODE",
            "adapter_school_ward_code_field": "SCHOOL_WARD_CODE",
            "adapter_school_facility_code_ps_field": "EDUFACILITYCODE_PS",
            "adapter_school_facility_code_ss_field": "EDUFACILITYCODE_SS",
        }
        adapter = SurveySolutionsTargetingAdapter(config=cfg)

        record = {
            "interview__key": "KEY-001",
            "name": "Alice",
            "RELATIONSHIPTOHEAD": "HEAD",
            "SEX": "2",                         # will be mapped later by service if gender map is configured
            "DATEOFBIRTH": "1990-05-06",
            "GRADE": "FORM4",
            "SCHOOL_DISTRICT_CODE": "0101",
            "SCHOOL_WARD_CODE": "010101",
            "EDUFACILITYCODE_PS": "PS-01",
            "EDUFACILITYCODE_SS": "SS-01",
        }

        x = adapter.transform(record)
        # core identity
        self.assertEqual(x["first_name"], "Alice")
        self.assertIsNone(x.get("last_name"))
        self.assertEqual(x["dob"], "1990-05-06")       # normalized to YYYY-MM-DD
        self.assertEqual(x["external_id"], "KEY-001")

        # gender is pulled from configured 'SEX' column (raw value "2" for now)
        self.assertEqual(x["gender"], "2")

        # schooling fields
        self.assertEqual(x["current_grade"], "FORM4")
        self.assertEqual(x["school_district_code"], "0101")
        self.assertEqual(x["school_ward_code"], "010101")
        self.assertEqual(x["school_facility_code_ps"], "PS-01")
        self.assertEqual(x["school_facility_code_ss"], "SS-01")

    def test_transform_household_bits_defaults_present(self):
        # Even if not provided, adapter returns the keys (mostly None)
        adapter = SurveySolutionsTargetingAdapter(config={})
        record = {"interview__key": "K-1"}  # minimal

        x = adapter.transform(record)
        # has a gps_point dict with expected keys set to None
        self.assertIn("gps_point", x)
        self.assertSetEqual(set(x["gps_point"].keys()), {"lat", "lon", "alt", "acc"})
        # external_id present
        self.assertEqual(x["external_id"], "K-1")


if __name__ == "__main__":
    unittest.main()
