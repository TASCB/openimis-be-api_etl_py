# api_etl/tests/test_pmt.py
from __future__ import annotations

import unittest
from api_etl.workflows.pmt import (
    URBAN_COEF,
    compute_household_pmt_score,
    enrich_rows_with_pmt,
    household_pmt_inputs,
)


class PMTTests(unittest.TestCase):
    def test_compute_household_pmt_score_basic(self):
        # HH of size 2, urban, with assets [1,4] (both present in formula)
        hh = {"household_size": 2, "assets_owned": '["1","4"]', "settlement_type": "Urban"}
        members = [
            {"dob": "1990-01-01"},  # age in [15,64]
            {"dob": "2015-01-01"},  # likely <15
        ]
        # n_15_64 should be 1
        expected = (
            11.688
            + (-0.10) * 2
            + 0.367 * 1  # asset 1
            + 0.259 * 1  # asset 4
            + URBAN_COEF * 1
            + (-0.043) * 1
        )
        self.assertAlmostEqual(compute_household_pmt_score(hh, members), round(expected, 3))

    def test_enrich_rows_with_pmt_groups_by_interview_key(self):
        rows = [
            {"interview_key": "A", "dob": "1990-01-01", "household_size": 2, "assets_owned": "[]", "settlement_type": "Rural"},
            {"interview_key": "A", "dob": "2010-01-01"},
            {"interview_key": "B", "dob": "1980-01-01", "household_size": 1, "assets_owned": "[\"12\"]", "settlement_type": "Urban"},
        ]
        out = enrich_rows_with_pmt(rows, hh_key="interview_key")
        # all rows in a household share the same pmt_score
        a_scores = {r["pmt_score"] for r in out if r["interview_key"] == "A"}
        b_scores = {r["pmt_score"] for r in out if r["interview_key"] == "B"}
        self.assertEqual(len(a_scores), 1)
        self.assertEqual(len(b_scores), 1)


class PmtInputsFromSurveyAnswersTests(unittest.TestCase):
    RAW = {"hh_size": "6", "hh_asset__1": "1", "hh_asset__4": 1, "hh_asset__9": "0", "hh_location": "1"}

    def test_reads_nested_adapter_row(self):
        row = {"interview_key": "A", "json_ext": {"raw": dict(self.RAW)}}
        inputs = household_pmt_inputs(row)
        self.assertEqual(inputs["household_size"], 6)
        self.assertEqual(sorted(inputs["assets_owned"]), ["1", "4"])
        self.assertEqual(inputs["settlement_type"], "urban")

    def test_reads_flat_raw_and_rural(self):
        inputs = household_pmt_inputs({"raw": {**self.RAW, "hh_location": "2"}}, members_count=3)
        self.assertEqual(inputs["household_size"], 3)
        self.assertEqual(inputs["settlement_type"], "rural")

    def test_explicit_keys_win(self):
        inputs = household_pmt_inputs({
            "household_size": 2, "assets_owned": ["12"], "settlement_type": "Urban", "raw": dict(self.RAW),
        })
        self.assertEqual(inputs, {"household_size": 2, "assets_owned": ["12"], "settlement_type": "Urban"})

    def test_no_answers_defaults_rural_and_empty(self):
        self.assertEqual(
            household_pmt_inputs({}, members_count=0),
            {"household_size": 0, "assets_owned": [], "settlement_type": "rural"},
        )

    def test_score_uses_survey_answers(self):
        coeffs = {"intercept": 10.0, "household_size_coef": -0.1, "working_age_coef": 0.0,
                  "urban_coef": 0.5, "assets": {"1": 0.2, "4": 0.3, "9": 5.0}}
        hh = {"json_ext": {"raw": dict(self.RAW)}}
        members = [{"dob": None}] * 6
        self.assertAlmostEqual(compute_household_pmt_score(hh, members, coeffs), 10.4)


if __name__ == "__main__":
    unittest.main()
