from django.db import migrations, models

DASHBOARD_RIGHT = 953101
REFERENCE_RIGHT = 953001


def grant_dashboard_right(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO "tblRoleRight" ("RoleID", "RightID", "ValidityFrom", "AuditUserId")
            SELECT DISTINCT rr."RoleID", %s, NOW(), 1
            FROM "tblRoleRight" rr
            WHERE rr."RightID" = %s AND rr."ValidityTo" IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM "tblRoleRight" x
                  WHERE x."RoleID" = rr."RoleID" AND x."RightID" = %s AND x."ValidityTo" IS NULL
              )
            """,
            [DASHBOARD_RIGHT, REFERENCE_RIGHT, DASHBOARD_RIGHT],
        )


def revoke_dashboard_right(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('DELETE FROM "tblRoleRight" WHERE "RightID" = %s', [DASHBOARD_RIGHT])


def _hq_url_field():
    return models.CharField(blank=True, db_index=True, default="", max_length=255)


class Migration(migrations.Migration):
    dependencies = [('api_etl', '0007_survey_interview_activity_at')]

    operations = [
        migrations.AddField(model_name='pulledhistory', name='hq_url', field=_hq_url_field()),
        migrations.AddField(model_name='historicalpulledhistory', name='hq_url', field=_hq_url_field()),
        migrations.AddField(model_name='surveyinterviewcache', name='hq_url', field=_hq_url_field()),
        migrations.AddField(model_name='surveydashboardsnapshot', name='hq_url', field=_hq_url_field()),
        migrations.AlterUniqueTogether(
            name='surveydashboardsnapshot',
            unique_together={('snapshot_date', 'questionnaire_id', 'hq_url')},
        ),
        migrations.RunPython(grant_dashboard_right, revoke_dashboard_right),
    ]
