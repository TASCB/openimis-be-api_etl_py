# api_etl/tests/test_service.py
from __future__ import annotations

import unittest

from api_etl.services.survey_solution_service import (
    SurveySolutionService,
    _score_questionnaire_match,
)


class _FakeSource:
    def pull(self, *, questionnaire_id=None, questionnaire_ids=None, tab_name_contains=None,
             include_meta=False, yield_source_meta=False, from_dt=None, to_dt=None, **kwargs):
        # Simulate the source yielding individual rows (not batches), like the real ExportSource does
        rows = [
            {"interview__key": "K1", "firstname": "A", "RELATIONSHIPTOHEAD": "1", "_source": {"questionnaire_id": "Q$1"}},
            {"interview__key": "K2", "firstname": "B", "RELATIONSHIPTOHEAD": "1", "_source": {"questionnaire_id": "Q$1"}},
        ]
        for r in rows:
            yield r


class _FakeAdapter:
    def __init__(self, config=None): self.config = config or {}
    def transform(self, record, tag=None):
        # Minimal transform: carry external_id and a gender code "1" (to be mapped by service)
        return {
            "external_id": record.get("interview__key"),
            "gender": record.get("gender", "1"),
            "_member_ordinal": record.get("_member_ordinal"),
            "RELATIONSHIPTOHEAD": record.get("RELATIONSHIPTOHEAD"),
        }


class _RecordingSink:
    def __init__(self, user=None): self.user = user; self.pushed = []
    def push(self, objs, batch_identifier=None):
        # Store push calls for assertions
        self.pushed.append({"batch": batch_identifier, "n": len(list(objs))})


class SurveySolutionServiceTests(unittest.TestCase):
    def test_direct_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Only ZIP export"):
            SurveySolutionService(config={"source_mode": "direct_api"})

    def test_runtime_config_overlays_module_defaults(self):
        svc = SurveySolutionService(
            user=None,
            source=_FakeSource(),
            sink=None,
            config={"questionnaire_id": "Q$1"},
        )

        self.assertEqual(svc.config["questionnaire_id"], "Q$1")
        self.assertIn("adapter_gender_map", svc.config)
        self.assertIn("paa_aliases", svc.config)

    def test_run_dry_run_with_gender_map_and_counts(self):
        cfg = {"adapter_gender_map": {"1": "M", "2": "F"}}
        svc = SurveySolutionService(
            user=None,
            source=_FakeSource(),
            adapter=_FakeAdapter(config=cfg),
            sink=None,               # no sink => dry_run True
            config=cfg,
            batch_size=1,
        )
        out = svc.run(questionnaire_id="Q$1", dry_run=True, yield_csv=True)

        s = out["summary"]
        self.assertEqual(s["rows_raw"], 2)
        self.assertEqual(s["rows_transformed"], 2)
        self.assertEqual(s["rows_pushed"], 0)
        self.assertEqual(s["per_questionnaire_counts"].get("Q$1"), 2)
        self.assertTrue(s["dry_run"])

        # file is produced when yield_csv=True
        self.assertIsNotNone(out["file"])

        # gender mapping applied (both become "M")
        self.assertEqual({r["gender"] for r in out["rows"]}, {"M"})

    def test_run_push_batches_when_sink_present(self):
        svc = SurveySolutionService(
            user="u1",
            source=_FakeSource(),
            adapter=_FakeAdapter(),
            sink=_RecordingSink(user="u1"),
            config={},
            batch_size=2,  # both rows in one batch
        )
        out = svc.run(questionnaire_id="Q$1", dry_run=False, yield_csv=False)

        self.assertEqual(out["summary"]["rows_pushed"], 2)
        self.assertEqual(len(svc.sink.pushed), 1)
        self.assertEqual(svc.sink.pushed[0]["n"], 2)

    def test_merge_household_roster_assigns_relationship_occurrence_ordinals(self):
        svc = SurveySolutionService(user=None, source=_FakeSource(), sink=None, config={})
        merged = svc._merge_household_roster([
            {"interview__key": "71-90-79-84", "village_code": "020415101"},
            {"interview__key": "71-90-79-84", "firstname": "Head", "RELATIONSHIPTOHEAD": "1"},
            {"interview__key": "71-90-79-84", "firstname": "Son One", "RELATIONSHIPTOHEAD": "3"},
            {"interview__key": "71-90-79-84", "firstname": "Son Two", "RELATIONSHIPTOHEAD": "3"},
        ])

        self.assertEqual([row["_member_ordinal"] for row in merged], ["01", "01", "02"])

    def _merge(self, rows):
        svc = SurveySolutionService(user=None, source=_FakeSource(), sink=None, config={})
        return svc._merge_household_roster(rows)

    def test_merge_drops_a_repeated_roster_row(self):
        tab = {"questionnaire_id": "Q$2", "tab_name": "hh_members"}
        head = {"interview__key": "K", "hh_members__id": "1", "firstname": "Head", "RELATIONSHIPTOHEAD": "1", "_source": tab}
        son = {"interview__key": "K", "hh_members__id": "2", "firstname": "Son", "RELATIONSHIPTOHEAD": "3", "_source": tab}
        merged = self._merge([head, son, dict(son), dict(head)])
        self.assertEqual([(r["firstname"], r["_member_ordinal"]) for r in merged], [("Head", "01"), ("Son", "01")])

    def test_merge_keeps_only_the_latest_questionnaire_version_of_an_interview(self):
        old = {"questionnaire_id": "Q$3", "tab_name": "hh_members"}
        new = {"questionnaire_id": "Q$10", "tab_name": "hh_members"}
        merged = self._merge([
            {"interview__key": "K", "hh_members__id": "1", "firstname": "Old head", "RELATIONSHIPTOHEAD": "1", "_source": old},
            {"interview__key": "K", "hh_members__id": "1", "firstname": "Head", "RELATIONSHIPTOHEAD": "1", "_source": new},
            {"interview__key": "K", "hh_members__id": "2", "firstname": "Son", "RELATIONSHIPTOHEAD": "3", "_source": new},
        ])
        self.assertEqual([r["firstname"] for r in merged], ["Head", "Son"])

    def test_merge_without_roster_ids_drops_only_identical_rows(self):
        a = {"interview__key": "K", "firstname": "A", "RELATIONSHIPTOHEAD": "3"}
        b = {"interview__key": "K", "firstname": "B", "RELATIONSHIPTOHEAD": "3"}
        merged = self._merge([a, dict(a), b])
        self.assertEqual([(r["firstname"], r["_member_ordinal"]) for r in merged], [("A", "01"), ("B", "02")])

    def test_questionnaire_match_uses_zanzibar_paa_aliases(self):
        q_pemba = {"Title": "DODOSO LA KAYA - RM4-PEMBA", "Version": 1}
        q_unguja = {"Title": "DODOSO LA KAYA - RM4-UNGUJA", "Version": 1}

        score, strategy = _score_questionnaire_match(q_pemba, "Kaskazini Pemba", "54", "", {})
        self.assertEqual(score, 6)
        self.assertEqual(strategy, "paa-alias")

        score, strategy = _score_questionnaire_match(q_pemba, "Kusini Pemba", "55", "", {})
        self.assertEqual(score, 6)
        self.assertEqual(strategy, "paa-alias")

        score, strategy = _score_questionnaire_match(q_unguja, "Kaskazini Unguja", "51", "", {})
        self.assertEqual(score, 6)
        self.assertEqual(strategy, "paa-alias")

        score, strategy = _score_questionnaire_match(q_unguja, "Kusini Unguja", "52", "", {})
        self.assertEqual(score, 6)
        self.assertEqual(strategy, "paa-alias")

        score, strategy = _score_questionnaire_match(q_unguja, "Mjini Magharibi", "53", "", {})
        self.assertEqual(score, 6)
        self.assertEqual(strategy, "paa-alias")


if __name__ == "__main__":
    unittest.main()
