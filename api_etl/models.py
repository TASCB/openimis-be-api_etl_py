# api_etl/models.py
from django.db import models
from django.conf import settings
from django.utils.dateparse import parse_datetime
from datetime import datetime
from core.models import User, HistoryModel


def parse_datetime_value(value):
    """Accept an ISO string or a datetime; return a datetime or None."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return parse_datetime(value)
    return None


class SurveySolutionsConfig(models.Model):
    """
    Stores Survey Solutions HQ connection details and questionnaire metadata.
    Used to configure ETL without hardcoding questionnaire IDs.
    """

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Friendly name for this configuration (used in GraphQL and Admin)",
    )
    hq_url = models.URLField(
        help_text="Base URL of Survey Solutions HQ (e.g., http://192.xxx.x.15:9700)"
    )
    username = models.CharField(max_length=100)
    password = models.CharField(
        max_length=100
    )  # NOTE: plain text, later can be encrypted
    questionnaire_id = models.CharField(
        max_length=200,
        help_text="Questionnaire identity including version (e.g., GUID$3)",
    )
    questionnaire_title = models.CharField(  # NEW: cached human-friendly label
        max_length=255,
        blank=True,
        null=True,
        help_text="Cached title from HQ (e.g., Household Survey v3)",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Survey Solutions Config"
        verbose_name_plural = "Survey Solutions Configs"

    def __str__(self):
        return f"{self.name} ({'active' if self.is_active else 'inactive'})"


class PulledHistory(HistoryModel):
    USE_CACHE = False
    """
    Tracks history of PAA-based questionnaire pulls and ETL runs.
    """

    # PAA (Planning Area Administrative) information
    paa_name = models.CharField(
        max_length=255,
        help_text="District display name (e.g., 'Kinondoni Municipal Council')",
    )
    district_code = models.CharField(
        max_length=20, help_text="District code used for matching (e.g., '0101')"
    )
    region_code = models.CharField(
        max_length=20, help_text="Region code used for matching (e.g., '01')"
    )

    # Questionnaire information
    questionnaire_id = models.CharField(
        max_length=200, help_text="Questionnaire identity (e.g., 'GUID$3')"
    )
    questionnaire_title = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Human-readable questionnaire title",
    )
    questionnaire_version = models.IntegerField(
        blank=True, null=True, help_text="Questionnaire version number"
    )

    # Export/ETL execution information
    export_job_id = models.CharField(
        max_length=50, blank=True, null=True, help_text="Survey Solutions export job ID"
    )
    export_started_at = models.DateTimeField(
        blank=True, null=True, help_text="When the HQ export job was initiated"
    )
    export_completed_at = models.DateTimeField(
        blank=True, null=True, help_text="When the HQ export job completed"
    )

    # Import counts and results
    n_households_inserted = models.IntegerField(
        default=0, help_text="Number of new households (groups) inserted"
    )
    n_households_updated = models.IntegerField(
        default=0, help_text="Number of existing households (groups) updated"
    )
    n_individuals_inserted = models.IntegerField(
        default=0, help_text="Number of new individuals inserted"
    )
    n_individuals_updated = models.IntegerField(
        default=0, help_text="Number of existing individuals updated"
    )

    # Derived fields for UI display
    number_of_households = models.IntegerField(
        default=0, help_text="Total unique households affected (inserted + updated)"
    )

    # Matching and workflow information
    matching_strategy = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="How questionnaire was matched (e.g., 'code+name', 'name-only', 'manual-override')",
    )

    # Execution metadata
    date_pulled = models.DateTimeField(
        auto_now_add=True, help_text="When this ETL run was initiated"
    )
    user_initiated = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        help_text="User who triggered this ETL run",
    )

    # Status and error tracking
    status = models.CharField(
        max_length=20,
        choices=[
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        default="running",
        help_text="Status of the ETL run",
    )
    error_message = models.TextField(
        blank=True, null=True, help_text="Error message if the run failed"
    )

    # Additional metadata in JSON format
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    class Meta:
        verbose_name = "Pulled Questionnaire History"
        verbose_name_plural = "Pulled Questionnaire History"
        ordering = ["-date_pulled"]
        indexes = [
            models.Index(fields=["paa_name"]),
            models.Index(fields=["district_code"]),
            models.Index(fields=["date_pulled"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.paa_name} - {self.date_pulled.strftime('%Y-%m-%d %H:%M')} ({self.status})"

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()

        if not settings.ROW_SECURITY:
            return queryset

        if user.is_anonymous:
            return queryset.filter(id=-1)

        if not user.is_imis_admin:
            return queryset.filter(id=-1)

        return queryset

    @property
    def total_records_affected(self) -> int:
        """Total number of records (individuals) affected in this run."""
        return self.n_individuals_inserted + self.n_individuals_updated

    @property
    def number_of_members(self) -> int:
        """Total individuals (members) affected in this run (inserted + updated)."""
        return self.n_individuals_inserted + self.n_individuals_updated

    def apply_timings(self, timings: dict) -> None:
        """
        Populate the export window from a run's phase timings and keep the raw
        breakdown in json_ext. Does not save.
        """
        if not timings:
            return

        started = parse_datetime_value(timings.get("export_started_at"))
        completed = parse_datetime_value(timings.get("export_completed_at"))
        if started:
            self.export_started_at = started
        if completed:
            self.export_completed_at = completed

        if not self.json_ext:
            self.json_ext = {}
        self.json_ext["timings"] = timings

    def update_counts_from_etl_result(self, etl_result: dict, user=None):
        """
        Update counts from ETL result summary.

        Args:
            etl_result: Result dict from SurveySolutionService.run()
            user: User object for audit logging (optional)
        """
        summary = etl_result.get("summary", {})
        rows = etl_result.get("rows", [])

        # Calculate unique households (prefer interview_key, fallback to group_code)
        unique_hh_keys = set()
        for row in rows:
            ik = row.get("interview_key") or row.get("external_id")
            if ik:
                unique_hh_keys.add(str(ik).strip())
                continue
            gc = row.get("group_code")
            if gc:
                unique_hh_keys.add(str(gc).strip())

        self.number_of_households = len(unique_hh_keys)

        # In practice, the sink should provide proper insert/update counts
        total_individuals = summary.get("rows_pushed", 0)
        self.n_individuals_inserted = total_individuals
        self.n_individuals_updated = 0

        # For now, assume all households were inserted (simplified)
        self.n_households_inserted = self.number_of_households
        self.n_households_updated = 0

        self.apply_timings(summary.get("timings") or {})

        # Store run metadata in json_ext
        if not self.json_ext:
            self.json_ext = {}
        self.json_ext.update(
            {
                "run_metadata": {
                    "batch_identifier": summary.get("batch_identifier"),
                    "rows_raw": summary.get("rows_raw"),
                    "rows_transformed": summary.get("rows_transformed"),
                    "rows_pushed": summary.get("rows_pushed"),
                    "per_questionnaire_counts": summary.get("per_questionnaire_counts"),
                    "etl_result_summary": summary,
                    "note": "counts are simplified: inserted assumed, updated=0",
                }
            }
        )

        # Save with user for audit logging
        if user:
            self.save(username=user.username)
        else:
            self.save()

    @classmethod
    def create_from_paa_etl_run(
        cls,
        paa_name: str,
        district_code: str,
        region_code: str,
        questionnaire_match: dict,
        user=None,
        **kwargs,
    ):
        """
        Factory method to create a PulledHistory record from PAA ETL run.

        Args:
            paa_name: District display name
            district_code: District code
            region_code: Region code
            questionnaire_match: Dict with questionnaire matching info
            user: User who initiated the run
            **kwargs: Additional fields
        """
        return cls.objects.create(
            paa_name=paa_name,
            district_code=district_code,
            region_code=region_code,
            questionnaire_id=questionnaire_match.get("questionnaire_id"),
            questionnaire_title=questionnaire_match.get("questionnaire_title"),
            questionnaire_version=questionnaire_match.get("questionnaire_version"),
            matching_strategy=questionnaire_match.get("matching_strategy"),
            user_initiated=user,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Real-time Survey Monitoring Dashboard
# ---------------------------------------------------------------------------
class SurveyInterviewCache(models.Model):
    """
    Local cache of interview "briefs" pulled from the Survey Solutions HQ
    ``/api/v1/interviews`` endpoint by the dashboard poller.

    One row per interview. The poller upserts on ``interview_id`` and records
    ``status_changed_at`` whenever the HQ status differs from what we last saw,
    so the dashboard can show a live activity feed and detect bottlenecks
    without Survey Solutions having to push webhooks.
    """

    interview_id = models.CharField(max_length=64, unique=True, db_index=True)
    interview_key = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    questionnaire_id = models.CharField(max_length=200, blank=True, null=True, db_index=True)
    questionnaire_title = models.CharField(max_length=500, blank=True, null=True)
    questionnaire_version = models.IntegerField(blank=True, null=True)
    assignment_id = models.IntegerField(blank=True, null=True)

    responsible_id = models.CharField(max_length=64, blank=True, null=True)
    responsible_name = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    responsible_role = models.CharField(max_length=50, blank=True, null=True)
    supervisor_name = models.CharField(max_length=255, blank=True, null=True, db_index=True)

    status = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    errors_count = models.IntegerField(default=0)
    not_answered_count = models.IntegerField(default=0)

    created_at_utc = models.DateTimeField(blank=True, null=True)
    last_entry_at_utc = models.DateTimeField(blank=True, null=True)
    server_updated_at_utc = models.DateTimeField(blank=True, null=True)

    # When *we* first observed the current status (used for the live feed).
    status_changed_at = models.DateTimeField(blank=True, null=True, db_index=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Rough proxy: minutes between creation and last entry.
    duration_minutes = models.FloatField(blank=True, null=True)

    # Harvested household-size answer; re-fetched only when the interview
    # changes after hh_size_fetched_at.
    hh_size = models.FloatField(blank=True, null=True)
    hh_size_fetched_at = models.DateTimeField(blank=True, null=True)

    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    class Meta:
        verbose_name = "Survey Interview (cache)"
        verbose_name_plural = "Survey Interviews (cache)"
        ordering = ["-status_changed_at", "-updated_at"]
        indexes = [
            models.Index(fields=["status", "questionnaire_id"], name="api_etl_sic_status_qid_idx"),
            models.Index(fields=["responsible_name"], name="api_etl_sic_resp_idx"),
            models.Index(fields=["status_changed_at"], name="api_etl_sic_changed_idx"),
            models.Index(fields=["last_entry_at_utc"], name="api_etl_sic_lastentry_idx"),
        ]

    def __str__(self):
        return f"{self.interview_key or self.interview_id} [{self.status}]"


class SurveyDashboardSnapshot(models.Model):
    """
    Daily aggregate snapshot used to draw trend / S-curve charts. One row per
    (date, questionnaire_id). ``questionnaire_id`` is the empty string for the
    "all questionnaires" rollup so the unique constraint behaves across DBs.
    """

    snapshot_date = models.DateField(db_index=True)
    questionnaire_id = models.CharField(max_length=200, blank=True, default="", db_index=True)

    total_interviews = models.IntegerField(default=0)
    in_progress = models.IntegerField(default=0)
    completed = models.IntegerField(default=0)
    approved_by_supervisor = models.IntegerField(default=0)
    approved_by_hq = models.IntegerField(default=0)
    sent_to_capital = models.IntegerField(default=0)
    rejected_by_supervisor = models.IntegerField(default=0)
    rejected_by_hq = models.IntegerField(default=0)

    # Cumulative "completed or beyond" (used directly for the S-curve).
    cumulative_completed = models.IntegerField(default=0)

    active_enumerators = models.IntegerField(default=0)
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Survey Dashboard Snapshot"
        verbose_name_plural = "Survey Dashboard Snapshots"
        ordering = ["-snapshot_date"]
        unique_together = [("snapshot_date", "questionnaire_id")]

    def __str__(self):
        return f"{self.snapshot_date} ({self.questionnaire_id or 'ALL'})"
