from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api_etl", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SurveyInterviewCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("interview_id", models.CharField(db_index=True, max_length=64, unique=True)),
                ("interview_key", models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ("questionnaire_id", models.CharField(blank=True, db_index=True, max_length=200, null=True)),
                ("questionnaire_title", models.CharField(blank=True, max_length=500, null=True)),
                ("questionnaire_version", models.IntegerField(blank=True, null=True)),
                ("assignment_id", models.IntegerField(blank=True, null=True)),
                ("responsible_id", models.CharField(blank=True, max_length=64, null=True)),
                ("responsible_name", models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ("responsible_role", models.CharField(blank=True, max_length=50, null=True)),
                ("supervisor_name", models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ("status", models.CharField(blank=True, db_index=True, max_length=50, null=True)),
                ("errors_count", models.IntegerField(default=0)),
                ("not_answered_count", models.IntegerField(default=0)),
                ("created_at_utc", models.DateTimeField(blank=True, null=True)),
                ("last_entry_at_utc", models.DateTimeField(blank=True, null=True)),
                ("server_updated_at_utc", models.DateTimeField(blank=True, null=True)),
                ("status_changed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("duration_minutes", models.FloatField(blank=True, null=True)),
                ("json_ext", models.JSONField(blank=True, db_column="Json_ext", default=dict)),
            ],
            options={
                "verbose_name": "Survey Interview (cache)",
                "verbose_name_plural": "Survey Interviews (cache)",
                "ordering": ["-status_changed_at", "-updated_at"],
            },
        ),
        migrations.CreateModel(
            name="SurveyDashboardSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("snapshot_date", models.DateField(db_index=True)),
                ("questionnaire_id", models.CharField(blank=True, db_index=True, default="", max_length=200)),
                ("total_interviews", models.IntegerField(default=0)),
                ("in_progress", models.IntegerField(default=0)),
                ("completed", models.IntegerField(default=0)),
                ("approved_by_supervisor", models.IntegerField(default=0)),
                ("approved_by_hq", models.IntegerField(default=0)),
                ("sent_to_capital", models.IntegerField(default=0)),
                ("rejected_by_supervisor", models.IntegerField(default=0)),
                ("rejected_by_hq", models.IntegerField(default=0)),
                ("cumulative_completed", models.IntegerField(default=0)),
                ("active_enumerators", models.IntegerField(default=0)),
                ("json_ext", models.JSONField(blank=True, db_column="Json_ext", default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Survey Dashboard Snapshot",
                "verbose_name_plural": "Survey Dashboard Snapshots",
                "ordering": ["-snapshot_date"],
            },
        ),
        migrations.AddIndex(
            model_name="surveyinterviewcache",
            index=models.Index(fields=["status", "questionnaire_id"], name="api_etl_sic_status_qid_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyinterviewcache",
            index=models.Index(fields=["responsible_name"], name="api_etl_sic_resp_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyinterviewcache",
            index=models.Index(fields=["status_changed_at"], name="api_etl_sic_changed_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyinterviewcache",
            index=models.Index(fields=["last_entry_at_utc"], name="api_etl_sic_lastentry_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="surveydashboardsnapshot",
            unique_together={("snapshot_date", "questionnaire_id")},
        ),
    ]
