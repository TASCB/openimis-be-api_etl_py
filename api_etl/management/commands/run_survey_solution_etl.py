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
