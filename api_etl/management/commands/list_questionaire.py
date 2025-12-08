import json
import os
import requests

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "List Survey Solutions questionnaires via /api/v1/questionnaires."

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            help=(
                "HQ base URL, e.g. http://hq:9700/openimis. "
                "If omitted, will use SURVEY_SOLUTIONS_BASE_URL env var if set."
            ),
        )
        parser.add_argument(
            "--workspace",
            help=(
                "HQ workspace slug, e.g. openimis. "
                "If omitted, will use SURVEY_SOLUTIONS_WORKSPACE env var if set."
            ),
        )
        parser.add_argument(
            "--username",
            help=(
                "HQ basic auth username. "
                "If omitted, will use SURVEY_SOLUTIONS_USERNAME env var if set."
            ),
        )
        parser.add_argument(
            "--password",
            help=(
                "HQ basic auth password. "
                "If omitted, will use SURVEY_SOLUTIONS_PASSWORD env var if set."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum number of questionnaires to list (default: 20).",
        )
        parser.add_argument(
            "--raw",
            action="store_true",
            help="Print raw JSON from HQ instead of a formatted table.",
        )

    def handle(self, *args, **opts):
        # 1) Resolve connection details, preferring CLI, then environment.
        base_url = opts.get("base_url") or os.getenv("SURVEY_SOLUTIONS_BASE_URL")
        workspace = opts.get("workspace") or os.getenv("SURVEY_SOLUTIONS_WORKSPACE")
        username = opts.get("username") or os.getenv("SURVEY_SOLUTIONS_USERNAME")
        password = opts.get("password") or os.getenv("SURVEY_SOLUTIONS_PASSWORD")

        if not base_url:
            raise CommandError(
                "HQ base URL not provided.\n"
                "Either pass --base-url or set SURVEY_SOLUTIONS_BASE_URL env var."
            )
        if not (username and password):
            raise CommandError(
                "HQ basic auth credentials not provided.\n"
                "Either pass --username/--password or set "
                "SURVEY_SOLUTIONS_USERNAME and SURVEY_SOLUTIONS_PASSWORD env vars."
            )

        base_url = base_url.rstrip("/")
        endpoint = f"{base_url}/api/v1/questionnaires"

        headers = {"Accept": "application/json"}
        if workspace:
            headers["X-Workspace"] = workspace

        params = {"Limit": opts["limit"]}

        self.stdout.write(
            f"Requesting questionnaires from {endpoint} "
            f"(workspace={workspace!r}, limit={opts['limit']})"
        )

        try:
            r = requests.get(
                endpoint,
                headers=headers,
                params=params,
                auth=(username, password),
                timeout=20,
            )
            if r.status_code == 401:
                raise CommandError(
                    "Unauthorized (401) from HQ. "
                    "Check credentials / workspace or env vars."
                )
            r.raise_for_status()
        except requests.RequestException as e:
            raise CommandError(f"HTTP error while calling HQ: {e}")

        try:
            payload = r.json()
        except ValueError:
            raise CommandError(
                "Failed to parse JSON from HQ response. "
                f"Status={r.status_code}, body={r.text[:500]!r}"
            )

        if opts["raw"]:
            self.stdout.write(json.dumps(payload, indent=2, default=str))
            return

        questionnaires = payload.get("Questionnaires") or []
        if not questionnaires:
            self.stdout.write("No questionnaires returned.")
            return

        # Build table
        rows = []
        for q in questionnaires:
            rows.append(
                {
                    "identity": q.get("QuestionnaireIdentity") or "",
                    "ver": q.get("Version"),
                    "variable": q.get("Variable") or "",
                    "title": q.get("Title") or "",
                    "last_entry": q.get("LastEntryDate") or "",
                }
            )

        id_width = max(len("Identity"), max(len(r["identity"]) for r in rows))
        var_width = max(len("Variable"), max(len(r["variable"]) for r in rows))
        title_width = max(len("Title"), max(len(r["title"]) for r in rows))

        header = (
            f"{'Identity'.ljust(id_width)}  "
            f"{'Ver'.rjust(3)}  "
            f"{'Variable'.ljust(var_width)}  "
            f"{'Title'.ljust(title_width)}  "
            f"LastEntryDate"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for rrow in rows:
            line = (
                f"{rrow['identity'].ljust(id_width)}  "
                f"{str(rrow['ver']).rjust(3)}  "
                f"{rrow['variable'].ljust(var_width)}  "
                f"{rrow['title'].ljust(title_width)}  "
                f"{rrow['last_entry']}"
            )
            self.stdout.write(line)




# import json
# import requests

# from django.core.management.base import BaseCommand, CommandError


# class Command(BaseCommand):
#     help = "List Survey Solutions questionnaires via /api/v1/questionnaires"

#     def add_arguments(self, parser):
#         parser.add_argument(
#             "--base-url",
#             required=True,
#             help="HQ base URL (e.g. http://192.168.0.15:9700/openimis)",
#         )
#         parser.add_argument(
#             "--workspace",
#             type=str,
#             default=None,
#             help="HQ workspace slug (e.g. openimis). "
#                  "If base-url already includes workspace, still pass this to send X-Workspace header.",
#         )
#         parser.add_argument(
#             "--username",
#             required=True,
#             help="HQ basic auth username (e.g. feedMis)",
#         )
#         parser.add_argument(
#             "--password",
#             required=True,
#             help="HQ basic auth password",
#         )
#         parser.add_argument(
#             "--limit",
#             type=int,
#             default=20,
#             help="Maximum number of questionnaires to list (default: 20)",
#         )
#         parser.add_argument(
#             "--raw",
#             action="store_true",
#             help="Print raw JSON from HQ instead of a formatted table",
#         )

#     def handle(self, *args, **opts):
#         base_url = opts["base_url"].rstrip("/")
#         workspace = opts.get("workspace")
#         username = opts["username"]
#         password = opts["password"]
#         limit = opts["limit"]

#         endpoint = f"{base_url}/api/v1/questionnaires"

#         headers = {"Accept": "application/json"}
#         if workspace:
#             headers["X-Workspace"] = workspace

#         params = {"Limit": limit}

#         self.stdout.write(
#             f"Requesting questionnaires from {endpoint} "
#             f"(workspace={workspace!r}, limit={limit})"
#         )

#         try:
#             r = requests.get(
#                 endpoint,
#                 headers=headers,
#                 params=params,
#                 auth=(username, password),
#                 timeout=20,
#             )
#             if r.status_code == 401:
#                 raise CommandError(
#                     "Unauthorized (401) from HQ. "
#                     "Check username/password and workspace (X-Workspace header)."
#                 )
#             r.raise_for_status()
#         except requests.RequestException as e:
#             raise CommandError(f"HTTP error while calling HQ: {e}")

#         try:
#             payload = r.json()
#         except ValueError:
#             raise CommandError(
#                 "Failed to parse JSON from HQ response. "
#                 f"Status={r.status_code}, body={r.text[:500]!r}"
#             )

#         if opts["raw"]:
#             self.stdout.write(json.dumps(payload, indent=2, default=str))
#             return

#         questionnaires = payload.get("Questionnaires") or []

#         if not questionnaires:
#             self.stdout.write("No questionnaires returned.")
#             return

#         # Build simple table rows
#         rows = []
#         for q in questionnaires:
#             rows.append(
#                 {
#                     "identity": q.get("QuestionnaireIdentity") or "",
#                     "ver": q.get("Version"),
#                     "variable": q.get("Variable") or "",
#                     "title": q.get("Title") or "",
#                     "last_entry": q.get("LastEntryDate") or "",
#                 }
#             )

#         # Column widths
#         id_width = max(len("Identity"), max(len(r["identity"]) for r in rows))
#         var_width = max(len("Variable"), max(len(r["variable"]) for r in rows))
#         title_width = max(len("Title"), max(len(r["title"]) for r in rows))

#         header = (
#             f"{'Identity'.ljust(id_width)}  "
#             f"{'Ver'.rjust(3)}  "
#             f"{'Variable'.ljust(var_width)}  "
#             f"{'Title'.ljust(title_width)}  "
#             f"LastEntryDate"
#         )
#         self.stdout.write(header)
#         self.stdout.write("-" * len(header))

#         for rrow in rows:
#             line = (
#                 f"{rrow['identity'].ljust(id_width)}  "
#                 f"{str(rrow['ver']).rjust(3)}  "
#                 f"{rrow['variable'].ljust(var_width)}  "
#                 f"{rrow['title'].ljust(title_width)}  "
#                 f"{rrow['last_entry']}"
#             )
#             self.stdout.write(line)
