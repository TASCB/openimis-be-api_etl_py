"""Stored, indexed sort key for the survey dashboard feed and leaderboard.

Replaces a 5-column COALESCE that made every dashboard and feed load sort the
whole interview cache.
"""
from django.db import migrations, models

BACKFILL = """
UPDATE api_etl_surveyinterviewcache
SET activity_at = COALESCE(last_entry_at_utc, server_updated_at_utc, updated_at, status_changed_at, first_seen_at)
WHERE activity_at IS NULL;
"""


class Migration(migrations.Migration):
    dependencies = [('api_etl', '0006_individual_external_id_exact_index')]

    operations = [
        migrations.AddField(
            model_name='surveyinterviewcache',
            name='activity_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.AddIndex(
            model_name='surveyinterviewcache',
            index=models.Index(fields=['-activity_at'], name='api_etl_sic_activity_idx'),
        ),
        migrations.AddIndex(
            model_name='surveyinterviewcache',
            index=models.Index(fields=['questionnaire_id', '-activity_at'], name='api_etl_sic_qid_activity_idx'),
        ),
    ]
