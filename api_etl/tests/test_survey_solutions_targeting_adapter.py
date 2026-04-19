# api_etl/tests/test_survey_solutions_targeting_adapter.py

from django.test import TestCase
from api_etl.adapters.survey_solutions_targeting_adapter import SurveySolutionsTargetingAdapter
from location.models import Location


class SurveySolutionsTargetingAdapterTest(TestCase):
    def setUp(self):
        # Minimal config – adapter currently mostly uses hard-coded column names
        self.adapter = SurveySolutionsTargetingAdapter(config={})

        # Create a minimal Location entry that matches our expected code.
        # Pattern: RRDDWWWVV (9 digits) – example: "070405101"
        # Adjust fields if your Location model requires more.
        self.location = Location.objects.create(
            code="070405101",
            name="Kibugumo",
            type="V",         # village
        )

    def _build_record(self, overrides=None):
        """
        Helper to build a fake Survey Solutions row.
        `overrides` lets each test tweak specific fields.
        """
        base = {
            "firstname": "ZAI",
            "lastname": "SABI",
            "dob": "2000-10-04",
            "SEX": "F",
            "VILLAGE_CODE": "70405101",        # 8 digits -> zfill(9) => "070405101"
            "WARD_CODE": "704051",             # present but unused if village exists
            "TF4_NO": "23",
            "RELATIONSHIPTOHEAD": "5",         # maps to BROTHER/SISTER depending on gender
            "HHREP": "1",
            "interview__key": "93-84-46-41",
        }
        if overrides:
            base.update(overrides)
        return base

    def test_location_code_from_village_code_rrddwwwvv(self):
        """
        VILLAGE_CODE 70405101 --> location_code 070405101 (RRDDWWWVV, 9 digits).
        """
        record = self._build_record()
        result = self.adapter.transform(record)

        self.assertEqual(result["location_code"], "070405101")

    def test_location_name_resolved_from_location_model(self):
        """
        Adapter should look up Location by code and populate location_name.
        """
        record = self._build_record()
        result = self.adapter.transform(record)

        self.assertEqual(result["location_code"], "070405101")
        self.assertEqual(result["location_name"], "Kibugumo")

    def test_group_code_uses_p3_prefix_location_code_and_tf4(self):
        """
        group_code should be P3-<location_code>-<last 8 interview key digits>.
        Example: P3-070405101-93844641
        """
        record = self._build_record()
        result = self.adapter.transform(record)

        self.assertEqual(result["group_code"], "P3-070405101-93844641")

    def test_external_id_uses_group_role_and_member_ordinal(self):
        record = self._build_record(overrides={"RELATIONSHIPTOHEAD": "1", "_member_ordinal": "1"})
        result = self.adapter.transform(record)

        self.assertEqual(result["external_id"], "P3-070405101-93844641-1-01")
        self.assertEqual(result["member_ordinal"], "01")

    def test_external_id_supports_repeated_relationship_occurrences(self):
        record = self._build_record(overrides={"RELATIONSHIPTOHEAD": "3", "_member_ordinal": "2"})
        result = self.adapter.transform(record)

        self.assertEqual(result["external_id"], "P3-070405101-93844641-3-02")
        self.assertEqual(result["individual_role_code"], "3")

    def test_role_and_hhrep_mapping(self):
        """
        RELATIONSHIPTOHEAD + gender should map to individual_role.
        HHREP should be passed through as hhrep.
        """
        record = self._build_record()
        result = self.adapter.transform(record)

        # RELATIONSHIPTOHEAD = "5", SEX = "2" (F) → "SISTER"
        self.assertEqual(result["individual_role_code"], "5")
        self.assertEqual(result["individual_role"], "SISTER")
        self.assertEqual(result["hhrep"], "1")

    def test_json_ext_structure_and_raw_payload(self):
        """
        json_ext should:
          - contain pmt_score, pmt_class, raw
          - NOT duplicate authoritative fields like location_code, group_code, etc.
          - keep original interview__key and raw survey fields under json_ext["raw"].
        """
        record = self._build_record()
        result = self.adapter.transform(record)

        json_ext = result["json_ext"]
        self.assertIn("pmt_score", json_ext)
        self.assertIn("pmt_class", json_ext)
        self.assertIn("raw", json_ext)
        self.assertIsInstance(json_ext["raw"], dict)

        # Authoritative fields live at top level, not duplicated inside json_ext
        self.assertNotIn("location_code", json_ext)
        self.assertNotIn("location_name", json_ext)
        self.assertNotIn("group_code", json_ext)

        # But the original survey fields still appear under raw
        raw = json_ext["raw"]
        self.assertIn("VILLAGE_CODE", raw)
        self.assertIn("WARD_CODE", raw)
        self.assertIn("RELATIONSHIPTOHEAD", raw)
        self.assertIn("HHREP", raw)
        self.assertIn("interview__key", raw)

    def test_tag_is_added_as_ss_batch_in_json_ext(self):
        """
        When a tag is provided, adapter should store it in json_ext['ss_batch'].
        """
        record = self._build_record()
        result = self.adapter.transform(record, tag="ss_individuals_20250101")

        self.assertIn("ss_batch", result["json_ext"])
        self.assertEqual(result["json_ext"]["ss_batch"], "ss_individuals_20250101")

    def test_missing_village_code_falls_back_to_ward_code(self):
        """
        If VILLAGE_CODE is missing but WARD_CODE is present,
        adapter should still produce a 9-digit code from WARD_CODE.
        """
        record = self._build_record(overrides={
            "VILLAGE_CODE": None,
            "WARD_CODE": "704051",  # 6 digits -> zfill(9) => "000704051"
        })
        # Create matching Location for the fallback code
        Location.objects.create(
            code="000704051",
            name="WardOnlyLocation",
            type="W",
        )

        result = self.adapter.transform(record)
        self.assertEqual(result["location_code"], "000704051")
        self.assertEqual(result["location_name"], "WardOnlyLocation")
