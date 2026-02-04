# api_etl/tests/test_sink.py
from __future__ import annotations

import unittest
from unittest.mock import patch

from api_etl.sinks.individual_import_sink import IndividualImportSink


class _DummyWorkflow:
    name = "TARGETING"
    def run(self, payload):  # pragma: no cover
        return {"ok": True, "payload": payload}


class SinkBulkModeTests(unittest.TestCase):
    @patch("api_etl.sinks.individual_import_sink._resolve_workflow_arg", lambda user, cfg: _DummyWorkflow())
    def test_bulk_mode_calls_import_individuals_with_csv(self):
        # Fake IndividualImportService that exposes BULK method with explicit params
        class _FakeSvc:
            def __init__(self, user=None): self.user = user; self.captured = None
            def import_individuals(
                self,
                import_file,
                workflow_obj,
                group_aggregation_column,
                *,
                update_existing=False,
                lookup_field=None,
                batch_identifier=None,
            ):
                self.captured = {
                    "file_name": getattr(import_file, "name", ""),
                    "workflow": getattr(workflow_obj, "name", None),
                    "group_col": group_aggregation_column,
                    "update_existing": update_existing,
                    "lookup_field": lookup_field,
                    "batch_identifier": batch_identifier,
                }

        with patch("api_etl.sinks.individual_import_sink.IndividualImportService", _FakeSvc):
            cfg = {
                "sink_model_lookup_field": "json_ext__external_id",
                "sink_update_existing": True,
                "sink_workflow": "api_etl.workflows.targeting.TargetingWorkflow",
                "sink_group_aggregation_column": "location_code",
                "sink_csv_fields": [
                    "first_name","last_name","dob","gender",
                    "location_name","location_code","phone_number","email","external_id"
                ],
            }
            sink = IndividualImportSink(user="u1", config=cfg)
            objs = [
                {"first_name":"Alice","last_name":"A","dob":"1990-01-01","gender":"F","location_name":"L","location_code":"001","phone_number":None,"email":None,"external_id":"K1"},
                {"first_name":"Bob","last_name":"B","dob":"1992-01-01","gender":"M","location_name":"L","location_code":"001","phone_number":None,"email":None,"external_id":"K2"},
            ]
            sink.push(objs, batch_identifier="BID-1")

            cap = sink.svc.captured
            self.assertIsNotNone(cap)
            self.assertIn("BID-1", cap["file_name"])
            self.assertEqual(cap["workflow"], "TARGETING")
            self.assertEqual(cap["group_col"], "location_code")
            self.assertEqual(cap["update_existing"], True)
            self.assertEqual(cap["lookup_field"], "json_ext__external_id")
            self.assertEqual(cap["batch_identifier"], "BID-1")


class SinkSingleModeTests(unittest.TestCase):
    @patch("api_etl.sinks.individual_import_sink._resolve_workflow_arg", lambda user, cfg: _DummyWorkflow())
    def test_single_mode_calls_import_individual_for_each_obj(self):
        # Fake service exposing only single-record method
        class _FakeSvc:
            def __init__(self, user=None): self.user = user; self.calls = []
            def import_individual(self, obj, **kwargs):
                self.calls.append({"obj": obj, "kwargs": kwargs})

        with patch("api_etl.sinks.individual_import_sink.IndividualImportService", _FakeSvc):
            cfg = {"sink_model_lookup_field": "json_ext__external_id", "sink_update_existing": False}
            sink = IndividualImportSink(user="uX", config=cfg)
            objs = [{"external_id":"E1"}, {"external_id":"E2"}]
            sink.push(objs, batch_identifier="B-77")
            self.assertEqual(len(sink.svc.calls), 2)
            self.assertEqual(sink.svc.calls[0]["obj"]["external_id"], "E1")
            self.assertEqual(sink.svc.calls[1]["obj"]["external_id"], "E2")


if __name__ == "__main__":
    unittest.main()
