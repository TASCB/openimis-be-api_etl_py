# api_etl/tests/test_service.py
from __future__ import annotations

import unittest

from api_etl.services.survey_solution_service import SurveySolutionService


class _FakeSource:
    def pull(self, *, questionnaire_id=None, questionnaire_ids=None, tab_name_contains=None,
             include_meta=False, yield_source_meta=False, from_dt=None, to_dt=None, **kwargs):
        # Simulate the source yielding individual rows (not batches), like the real ExportSource does
        rows = [
            {"interview__key": "K1", "_source": {"questionnaire_id": "Q$1"}},
            {"interview__key": "K2", "_source": {"questionnaire_id": "Q$1"}},
        ]
        for r in rows:
            yield r


class _FakeAdapter:
    def __init__(self, config=None): self.config = config or {}
    def transform(self, record, tag=None):
        # Minimal transform: carry external_id and a gender code "1" (to be mapped by service)
        return {"external_id": record.get("interview__key"), "gender": record.get("gender", "1")}


class _RecordingSink:
    def __init__(self, user=None): self.user = user; self.pushed = []
    def push(self, objs, batch_identifier=None):
        # Store push calls for assertions
        self.pushed.append({"batch": batch_identifier, "n": len(list(objs))})


class SurveySolutionServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
