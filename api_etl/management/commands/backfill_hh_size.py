from django.core.management.base import BaseCommand

from api_etl.services.survey_dashboard_service import backfill_hh_size


class Command(BaseCommand):
    help = (
        "Seed the survey-dashboard interview cache with a questionnaire's interviews "
        "and harvest their household-size answers (bounded, throttled, resumable). "
        "Repeat until 'remaining' reaches 0, or pass --until-done."
    )

    def add_arguments(self, parser):
        parser.add_argument("questionnaire", help="Questionnaire identity, e.g. GUID$version")
        parser.add_argument("--budget", type=int, default=None, help="Detail fetches per pass")
        parser.add_argument("--throttle", type=float, default=None, help="Seconds between detail fetches")
        parser.add_argument("--until-done", action="store_true", help="Loop passes until remaining=0")

    def handle(self, *args, **options):
        questionnaire = options["questionnaire"]
        while True:
            result = backfill_hh_size(
                questionnaire, budget=options["budget"], throttle=options["throttle"]
            )
            self.stdout.write(str(result))
            if result.get("skipped"):
                return
            if not options["until_done"] or not result.get("remaining"):
                return
