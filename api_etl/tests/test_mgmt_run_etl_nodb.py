from __future__ import annotations
import importlib
import io
from unittest.mock import patch, MagicMock
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


def _import_cmd_module():
    """
    Locate the management command module. Prefer the api_etl location;
    fall back to developer_tools if that's where you've placed it.
    """
    for modpath in (
        "api_etl.management.commands.run_survey_solution_etl",
        "developer_tools.management.commands.run_survey_solution_etl",
    ):
        try:
            return importlib.import_module(modpath), modpath
        except Exception:
            continue
    return None, None


class RunSurveySolutionsEtlCommandNoDBTests(SimpleTestCase):
    def setUp(self):
        self.cmdmod, self.modpath = _import_cmd_module()
        if not self.cmdmod:
            self.skipTest("run_survey_solution_etl command module not found")

    @patch.dict("os.environ", {}, clear=False)
    def test_runs_with_overrides_and_args(self):
        # Patch symbols as used in the command module
        with patch(f"{self.modpath}.get_user_model") as get_user_model, \
             patch(f"{self.modpath}.SurveySolutionService") as Svc:

            # Fake user
            user = type("U", (), {"id": "u-1", "username": "Admin"})()
            U = MagicMock()
            U.objects.filter.return_value.first.return_value = user
            get_user_model.return_value = U

            # Fake service result
            svc_inst = MagicMock()
            svc_inst.run.return_value = {"summary": {"ok": True, "rows": 2}}
            Svc.return_value = svc_inst

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                call_command(
                    "run_survey_solution_etl",
                    "--user", "Admin",
                    "--dry-run",
                    "--from-dt", "2025-09-01",
                    "--to-dt", "2025-09-02",
                    "--qid", "Q$1",
                    "--qid", "Q$2",
                    "--tab", "hhroster",
                    "--workspace", "openimis",
                    "--base-url", "http://hq.example:9700",
                )

        # Service constructed with the resolved user
        Svc.assert_called_once()
        assert Svc.call_args.kwargs.get("user") is user

        # Verify kwargs passed to run()
        svc_inst.run.assert_called_once()
        rk = svc_inst.run.call_args.kwargs
        assert rk["dry_run"] is True
        assert rk["from_dt"] == "2025-09-01"
        assert rk["to_dt"] == "2025-09-02"
        assert rk["questionnaire_ids"] == ["Q$1", "Q$2"]
        # Command passes tab_name_contains (what the service expects)
        assert rk["tab_name_contains"] == "hhroster"
        # Command forwards endpoint overrides directly
        assert rk["workspace"] == "openimis"
        assert rk["base_url"] == "http://hq.example:9700"

        out = buf.getvalue()
        assert "SUMMARY:" in out
        assert "'rows': 2" in out

    def test_user_not_found_raises(self):
        with patch(f"{self.modpath}.get_user_model") as get_user_model:
            U = MagicMock()
            U.objects.filter.return_value.first.return_value = None
            get_user_model.return_value = U

            with self.assertRaises(CommandError):
                call_command("run_survey_solution_etl", "--user", "ghost")
