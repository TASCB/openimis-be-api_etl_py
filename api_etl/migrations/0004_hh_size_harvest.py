from django.db import migrations, models


def normalize_questionnaire_ids(apps, schema_editor):
    """Store GUIDs dash-less everywhere so cache rows (REST briefs return dashed
    UUIDs) match PulledHistory identities (dash-less). Duplicates that already
    exist in both spellings are merged in favour of the dash-less row."""
    SurveyInterviewCache = apps.get_model("api_etl", "SurveyInterviewCache")
    SurveyDashboardSnapshot = apps.get_model("api_etl", "SurveyDashboardSnapshot")

    for row in SurveyInterviewCache.objects.exclude(questionnaire_id__isnull=True).exclude(questionnaire_id="").iterator():
        plain = str(row.questionnaire_id).replace("-", "")
        if plain != row.questionnaire_id:
            row.questionnaire_id = plain
            row.save(update_fields=["questionnaire_id"])
    for row in SurveyInterviewCache.objects.filter(interview_id__contains="-").iterator():
        plain = str(row.interview_id).replace("-", "")
        if SurveyInterviewCache.objects.filter(interview_id=plain).exists():
            row.delete()
        else:
            row.interview_id = plain
            row.save(update_fields=["interview_id"])

    for row in SurveyDashboardSnapshot.objects.filter(questionnaire_id__contains="-").iterator():
        plain = str(row.questionnaire_id).replace("-", "")
        if SurveyDashboardSnapshot.objects.filter(snapshot_date=row.snapshot_date, questionnaire_id=plain).exists():
            row.delete()
        else:
            row.questionnaire_id = plain
            row.save(update_fields=["questionnaire_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("api_etl", "0003_alter_surveysolutionsconfig_hq_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="surveyinterviewcache",
            name="hh_size",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="surveyinterviewcache",
            name="hh_size_fetched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(normalize_questionnaire_ids, migrations.RunPython.noop),
    ]
