from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from api_etl.services.survey_solution_service import SurveySolutionService
from api_etl.utils import data_to_file

class Command(BaseCommand):
    help = "Run Survey Solutions → openIMIS ETL (Export API → Adapter → [optional PMT] → Sink)"

    def add_arguments(self, parser):
        parser.add_argument("--user", default="Admin", help="Username that owns the import (case-insensitive)")
        parser.add_argument("--dry-run", action="store_true", help="If set, do not push to DB (CSV only)")
        parser.add_argument("--out-csv", type=str, default=None, help="Write adapted (and PMT-enriched) CSV to this path")
        parser.add_argument("--enrich-pmt", action="store_true", help="Attach pmt_score per household before import")
        parser.add_argument("--hh-key", type=str, default="interview_key", help="Household key field for PMT (default: interview_key)")
        parser.add_argument("--max-rows", type=int, default=None, help="Stop after N rows")
        parser.add_argument("--from-dt", type=str, default=None, help="YYYY-MM-DD (inclusive)")
        parser.add_argument("--to-dt", type=str, default=None, help="YYYY-MM-DD (inclusive)")
        parser.add_argument("--qid", action="append", dest="qids", help="QuestionnaireId GUID$ver (repeatable)")
        parser.add_argument("--workspace", type=str, default=None, help="HQ workspace slug")
        parser.add_argument("--base-url", type=str, default=None, help="HQ base URL")
        parser.add_argument("--tab", type=str, default=None, help="Filter .tab files by substring")
        parser.add_argument("--reuse-latest", action="store_true", help="Reuse latest Completed HQ export job (best-effort)")

    def handle(self, *args, **opts):
        U = get_user_model()
        user = U.objects.filter(username__iexact=opts["user"]).first() or U.objects.order_by("id").first()
        if not user:
            raise CommandError(f"User {opts['user']!r} not found and no fallback user exists")

        svc = SurveySolutionService(user=user)

        # Best-effort enablement without passing unknown kwargs to the Source:
        if opts.get("reuse_latest"):
            try:
                # If the Source supports it as an attribute, set it.
                setattr(svc.source, "reuse_latest_completed", True)
                self.stdout.write("Reuse-latest mode: enabled on Source (attribute).")
            except Exception:
                self.stdout.write("Reuse-latest mode: Source does not expose 'reuse_latest_completed'; ignoring.")

        run_kwargs = {
            "dry_run": opts["dry_run"],
            "max_rows": opts["max_rows"],
            "from_dt": opts["from_dt"],
            "to_dt": opts["to_dt"],
            "enrich_pmt": opts["enrich_pmt"],
            "hh_key": opts["hh_key"],
            "tab_filter": opts.get("tab"),  # service maps this to tab_name_contains
        }
        if opts.get("qids"):
            run_kwargs["questionnaire_ids"] = opts["qids"]
        run_kwargs = {k: v for k, v in run_kwargs.items() if v is not None}

        # Source overrides (ONLY allowed keys for pull(); do NOT include reuse_latest here)
        source_overrides = {}
        if opts.get("workspace"):
            source_overrides["workspace"] = opts["workspace"]
        if opts.get("base_url"):
            source_overrides["base_url"] = opts["base_url"]

        out = svc.run(**run_kwargs, **source_overrides) or {}
        summary = out.get("summary") or out
        self.stdout.write(self.style.SUCCESS(f"SUMMARY: {summary}"))

        out_csv = opts.get("out_csv")
        if out_csv:
            f = out.get("file")
            if f:
                if hasattr(f, "seek"): f.seek(0)
                with open(out_csv, "wb") as w: w.write(f.read())
                self.stdout.write(f"CSV (written): {out_csv}")
            else:
                rows = out.get("rows") or out.get("data")
                if rows:
                    f = data_to_file(rows, identifier="run_survey_solution_etl")
                    if hasattr(f, "seek"): f.seek(0)
                    with open(out_csv, "wb") as w: w.write(f.read())
                    self.stdout.write(f"CSV (written): {out_csv}")
                else:
                    self.stdout.write("No rows available to write CSV.")



# from django.core.management.base import BaseCommand, CommandError
# from django.contrib.auth import get_user_model
# from api_etl.services.survey_solution_service import SurveySolutionService


# class Command(BaseCommand):
#     help = "Run Survey Solutions → openIMIS ETL (Export API → Adapter → Sink)"

#     def add_arguments(self, parser):
#         parser.add_argument("--user", default="Admin", help="Username that owns the import")
#         parser.add_argument("--dry-run", action="store_true", help="If set, do not push to DB")
#         parser.add_argument("--max-rows", type=int, default=None, help="Stop after N rows")
#         parser.add_argument("--from-dt", type=str, default=None, help="YYYY-MM-DD (inclusive)")
#         parser.add_argument("--to-dt", type=str, default=None, help="YYYY-MM-DD (inclusive)")
#         parser.add_argument("--qid", action="append", dest="qids", help="QuestionnaireId GUID$ver (repeatable)")
#         parser.add_argument("--workspace", type=str, default=None, help="Workspace segment in URL")
#         parser.add_argument("--base-url", type=str, default=None, help="Override HQ base URL")
#         parser.add_argument("--tab", type=str, default=None, help="Filter .tab files by substring")

#     def handle(self, *args, **opts):
#         U = get_user_model()
#         user = U.objects.filter(username=opts["user"]).first()
#         if not user:
#             raise CommandError(f"User {opts['user']} not found")

#         svc = SurveySolutionService(user=user)

#         run_kwargs = {
#             "dry_run": opts["dry_run"],
#             "max_rows": opts["max_rows"],
#             "from_dt": opts["from_dt"],
#             "to_dt": opts["to_dt"],
#             "workspace": opts.get("workspace"),
#             "base_url": opts.get("base_url"),
#         }
#         if opts["qids"]:
#             run_kwargs["questionnaire_ids"] = opts["qids"]
#         if opts["tab"]:
#             run_kwargs["tab_name_contains"] = opts["tab"]

#         # Drop None values
#         run_kwargs = {k: v for k, v in run_kwargs.items() if v is not None}

#         out = svc.run(**run_kwargs)
#         self.stdout.write(self.style.SUCCESS(f"SUMMARY: {out['summary']}"))
#         f = out.get("file")
#         if f:
#             self.stdout.write(f"CSV: {getattr(f, 'name', '(in-memory)')} {getattr(f, 'size', 0)} bytes")


# from __future__ import annotations
# import io
# from unittest.mock import patch, MagicMock
# from django.core.management import call_command
# from django.core.management.base import CommandError
# from django.test import SimpleTestCase


# class RunSurveySolutionsEtlCommandNoDBTests(SimpleTestCase):
#     # IMPORTANT: patch the symbols AS USED IN THE COMMAND MODULE
#     @patch("api_etl.management.commands.run_survey_solution_etl.SurveySolutionService")
#     @patch("api_etl.management.commands.run_survey_solution_etl.get_user_model")
#     def test_runs_with_overrides_and_args(self, get_user_model, Svc):
#         # Fake user from get_user_model()
#         user = type("U", (), {"id": "u-1", "username": "Admin"})()
#         U = MagicMock()
#         U.objects.filter.return_value.first.return_value = user
#         get_user_model.return_value = U

#         # Fake service
#         svc_inst = MagicMock()
#         svc_inst.run.return_value = {"summary": {"ok": True, "rows": 2}}
#         Svc.return_value = svc_inst

#         buf = io.StringIO()
#         with patch("sys.stdout", buf):
#             call_command(
#                 "run_survey_solution_etl",
#                 "--user", "Admin",
#                 "--dry-run",
#                 "--from-dt", "2025-09-01",
#                 "--to-dt", "2025-09-02",
#                 "--qid", "Q$1",
#                 "--qid", "Q$2",
#                 "--tab", "hhroster",
#                 "--workspace", "openimis",
#                 "--base-url", "http://hq.example:9700",
#             )

#         # Service constructed with the resolved user
#         Svc.assert_called_once()
#         self.assertIs(Svc.call_args.kwargs.get("user"), user)

#         # Verify the kwargs passed to run()
#         svc_inst.run.assert_called_once()
#         rk = svc_inst.run.call_args.kwargs
#         self.assertTrue(rk["dry_run"])
#         self.assertEqual(rk["from_dt"], "2025-09-01")
#         self.assertEqual(rk["to_dt"], "2025-09-02")
#         self.assertEqual(rk["questionnaire_ids"], ["Q$1", "Q$2"])
#         # NOTE: SurveySolutionService.run expects tab_name_contains
#         self.assertEqual(rk["tab_name_contains"], "hhroster")
#         self.assertEqual(rk["workspace"], "openimis")
#         self.assertEqual(rk["base_url"], "http://hq.example:9700")

#         out = buf.getvalue()
#         self.assertIn("SUMMARY:", out)
#         self.assertIn("'rows': 2", out)

#     @patch("api_etl.management.commands.run_survey_solution_etl.get_user_model")
#     def test_user_not_found_raises(self, get_user_model):
#         U = MagicMock()
#         U.objects.filter.return_value.first.return_value = None
#         get_user_model.return_value = U

#         with self.assertRaises(CommandError):
#             call_command("run_survey_solution_etl", "--user", "ghost")
