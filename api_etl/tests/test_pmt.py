# api_etl/tests/test_pmt.py
from __future__ import annotations

import unittest
from api_etl.workflows.pmt import compute_household_pmt_score, enrich_rows_with_pmt, URBAN_COEF


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


if __name__ == "__main__":
    unittest.main()
