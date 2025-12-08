# api_etl/models.py
from django.db import models
from django.conf import settings
from datetime import datetime
from core.models import User, HistoryModel


class SurveySolutionsConfig(models.Model):
    """
    Stores Survey Solutions HQ connection details and questionnaire metadata.
    Used to configure ETL without hardcoding questionnaire IDs.
    """

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Friendly name for this configuration (used in GraphQL and Admin)"
    )
    hq_url = models.URLField(
        help_text="Base URL of Survey Solutions HQ (e.g., http://192.168.0.15:9700)"
    )
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=100)  # NOTE: plain text, later can be encrypted
    questionnaire_id = models.CharField(
        max_length=200,
        help_text="Questionnaire identity including version (e.g., GUID$3)"
    )
    questionnaire_title = models.CharField(   # NEW: cached human-friendly label
        max_length=255,
        blank=True,
        null=True,
        help_text="Cached title from HQ (e.g., Household Survey v3)"
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
        help_text="District display name (e.g., 'Kinondoni Municipal Council')"
    )
    district_code = models.CharField(
        max_length=20,
        help_text="District code used for matching (e.g., '0101')"
    )
    region_code = models.CharField(
        max_length=20,
        help_text="Region code used for matching (e.g., '01')"
    )

    # Questionnaire information
    questionnaire_id = models.CharField(
        max_length=200,
        help_text="Questionnaire identity (e.g., 'GUID$3')"
    )
    questionnaire_title = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Human-readable questionnaire title"
    )
    questionnaire_version = models.IntegerField(
        blank=True,
        null=True,
        help_text="Questionnaire version number"
    )

    # Export/ETL execution information
    export_job_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Survey Solutions export job ID"
    )
    export_started_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the HQ export job was initiated"
    )
    export_completed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the HQ export job completed"
    )

    # Import counts and results
    n_households_inserted = models.IntegerField(
        default=0,
        help_text="Number of new households (groups) inserted"
    )
    n_households_updated = models.IntegerField(
        default=0,
        help_text="Number of existing households (groups) updated"
    )
    n_individuals_inserted = models.IntegerField(
        default=0,
        help_text="Number of new individuals inserted"
    )
    n_individuals_updated = models.IntegerField(
        default=0,
        help_text="Number of existing individuals updated"
    )

    # Derived fields for UI display
    number_of_households = models.IntegerField(
        default=0,
        help_text="Total unique households affected (inserted + updated)"
    )

    # Matching and workflow information
    matching_strategy = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="How questionnaire was matched (e.g., 'code+name', 'name-only', 'manual-override')"
    )

    # Execution metadata
    date_pulled = models.DateTimeField(
        auto_now_add=True,
        help_text="When this ETL run was initiated"
    )
    user_initiated = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        help_text="User who triggered this ETL run"
    )

    # Status and error tracking
    status = models.CharField(
        max_length=20,
        choices=[
            ('running', 'Running'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='running',
        help_text="Status of the ETL run"
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text="Error message if the run failed"
    )

    # Additional metadata in JSON format
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    class Meta:
        verbose_name = "Pulled Questionnaire History"
        verbose_name_plural = "Pulled Questionnaire History"
        ordering = ['-date_pulled']
        indexes = [
            models.Index(fields=['paa_name']),
            models.Index(fields=['district_code']),
            models.Index(fields=['date_pulled']),
            models.Index(fields=['status']),
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

    @classmethod
    def create_from_paa_etl_run(cls, paa_name, district_code, region_code, questionnaire_id,
                               questionnaire_title, questionnaire_version, user, status, ss_batch):
        """
        Factory method to create a PulledHistory record for a PAA-based ETL run.
        """
        return cls.objects.create(
            paa_name=paa_name,
            district_code=district_code,
            region_code=region_code,
            questionnaire_id=questionnaire_id,
            questionnaire_title=questionnaire_title,
            questionnaire_version=questionnaire_version,
            status=status,
            user_created=user,
            user_updated=user,
            json_ext={
                'ss_batch': ss_batch,
                'etl_metadata': {
                    'run_type': 'paa_based',
                    'initiated_by': user.username if user else 'system',
                    'created_at': datetime.now().isoformat()
                }
            }
        )

    @property
    def total_records_affected(self) -> int:
        """Total number of records (individuals) affected in this run."""
        return self.n_individuals_inserted + self.n_individuals_updated

    def update_counts_from_etl_result(self, etl_result: dict):
        """
        Update counts from ETL result summary.

        Args:
            etl_result: Result dict from SurveySolutionService.run()
        """
        summary = etl_result.get('summary', {})
        rows = etl_result.get('rows', [])

        # Calculate unique households (groups) affected in this run
        unique_group_codes = set()
        for row in rows:
            group_code = row.get('group_code')
            if group_code:
                unique_group_codes.add(group_code)

        self.number_of_households = len(unique_group_codes)

        # In practice, the sink should provide proper insert/update counts
        total_individuals = summary.get('rows_pushed', 0)
        self.n_individuals_inserted = total_individuals
        self.n_individuals_updated = 0

        # For now, assume all households were inserted (simplified)
        self.n_households_inserted = self.number_of_households
        self.n_households_updated = 0

        # Store run metadata in json_ext following openIMIS patterns
        if not self.json_ext:
            self.json_ext = {}
        self.json_ext.update({
            'run_metadata': {
                'batch_id': summary.get('batch_id'),
                'config_ref': summary.get('config_ref'),
                'etl_result_summary': summary
            }
        })

        self.save()

    @classmethod
    def create_from_paa_etl_run(
        cls,
        paa_name: str,
        district_code: str,
        region_code: str,
        questionnaire_match: dict,
        user = None,
        **kwargs
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
            questionnaire_id=questionnaire_match.get('questionnaire_id'),
            questionnaire_title=questionnaire_match.get('questionnaire_title'),
            questionnaire_version=questionnaire_match.get('questionnaire_version'),
            matching_strategy=questionnaire_match.get('matching_strategy'),
            user_initiated=user,
            **kwargs
        )

