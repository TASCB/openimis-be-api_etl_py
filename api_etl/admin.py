from django.contrib import admin, messages
from django.contrib.admin.sites import AlreadyRegistered
from .models import SurveySolutionsConfig, SurveyInterviewCache, SurveyDashboardSnapshot


def _get_source():
    from api_etl.sources.survey_solutions_export_source import (
        SurveySolutionsExportSource,
    )

    return SurveySolutionsExportSource()


class SurveySolutionsConfigAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "hq_url",
        "questionnaire_id",
        "questionnaire_title",  # cached title
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "questionnaire_id", "questionnaire_title")
    actions = ["fetch_questionnaires", "refresh_titles"]

    def fetch_questionnaires(self, request, queryset):
        """List all available questionnaires from HQ."""
        for config in queryset:
            try:
                source = _get_source()
                questionnaires = source.list_questionnaires(
                    base_url=config.hq_url,
                    username=config.username,
                    password=config.password,
                )

                if not questionnaires:
                    self.message_user(
                        request,
                        f"No questionnaires returned for {config.name}",
                        messages.WARNING,
                    )
                    continue

                msg = "\n".join(
                    f"- {q['Title']} (v{q['Version']}) → {q['Id']}${q['Version']}"
                    for q in questionnaires
                )
                self.message_user(
                    request,
                    f"Available questionnaires for {config.name}:\n{msg}",
                    messages.INFO,
                )
            except Exception as e:
                self.message_user(
                    request,
                    f"Failed to fetch questionnaires for {config.name}: {e}",
                    messages.ERROR,
                )

    fetch_questionnaires.short_description = "Fetch available questionnaires from HQ"

    def refresh_titles(self, request, queryset):
        """Refresh cached questionnaire_title for selected configs."""
        updated = 0
        for config in queryset:
            try:
                source = _get_source()
                qlist = source.list_questionnaires(
                    base_url=config.hq_url,
                    username=config.username,
                    password=config.password,
                )

                match = None
                for q in qlist:
                    qid = f"{q['Id']}${q['Version']}"
                    if qid == config.questionnaire_id:
                        match = f"{q['Title']} (v{q['Version']})"
                        break

                if match:
                    config.questionnaire_title = match
                    config.save(update_fields=["questionnaire_title"])
                    updated += 1
                else:
                    self.message_user(
                        request,
                        f"{config.name}: ID {config.questionnaire_id} not found in HQ",
                        messages.WARNING,
                    )
            except Exception as e:
                self.message_user(
                    request,
                    f"Error refreshing title for {config.name}: {e}",
                    messages.ERROR,
                )

        if updated:
            self.message_user(request, f"Refreshed {updated} titles", messages.SUCCESS)

    refresh_titles.short_description = "Refresh cached questionnaire titles"


class SurveyInterviewCacheAdmin(admin.ModelAdmin):
    list_display = (
        "interview_key", "status", "responsible_name", "supervisor_name",
        "questionnaire_title", "status_changed_at", "updated_at",
    )
    list_filter = ("status", "responsible_role")
    search_fields = ("interview_key", "interview_id", "responsible_name", "supervisor_name")
    readonly_fields = ("first_seen_at", "updated_at")


class SurveyDashboardSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "snapshot_date", "questionnaire_id", "total_interviews", "completed",
        "approved_by_supervisor", "approved_by_hq", "cumulative_completed", "active_enumerators",
    )
    list_filter = ("questionnaire_id",)
    ordering = ("-snapshot_date",)


# --- Register on the default admin site ---
try:
    admin.site.register(SurveySolutionsConfig, SurveySolutionsConfigAdmin)
except AlreadyRegistered:
    pass
for _model, _admin in (
    (SurveyInterviewCache, SurveyInterviewCacheAdmin),
    (SurveyDashboardSnapshot, SurveyDashboardSnapshotAdmin),
):
    try:
        admin.site.register(_model, _admin)
    except AlreadyRegistered:
        pass

# --- ALSO register on common custom AdminSite instances used by openIMIS ---
_CANDIDATE_SITES = [
    ("core.admin", "admin_site"),
    ("openIMIS.admin", "admin_site"),
    ("backend.admin", "admin_site"),
    ("core.admin", "core_admin_site"),
    ("openIMIS.admin", "openimis_admin_site"),
]

for module_path, attr in _CANDIDATE_SITES:
    try:
        mod = __import__(module_path, fromlist=[attr])
        site = getattr(mod, attr, None)
        if site:
            try:
                site.register(SurveySolutionsConfig, SurveySolutionsConfigAdmin)
            except AlreadyRegistered:
                pass
    except Exception:

        continue
