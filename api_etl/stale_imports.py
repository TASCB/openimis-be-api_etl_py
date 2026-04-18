from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from api_etl.apps import ApiEtlConfig
from api_etl.models import PulledHistory


def fail_stale_running_imports(*, district_code=None, user=None) -> int:
    timeout_hours = getattr(ApiEtlConfig, "paa_etl_running_timeout_hours", 3)
    try:
        timeout_hours = float(timeout_hours)
    except (TypeError, ValueError):
        timeout_hours = 3

    if timeout_hours <= 0:
        return 0

    cutoff = timezone.now() - timedelta(hours=timeout_hours)
    queryset = PulledHistory.objects.filter(status="running", date_pulled__lt=cutoff)
    if district_code:
        queryset = queryset.filter(district_code=district_code)

    updated = 0
    for history in queryset.iterator():
        history.status = "failed"
        history.error_message = (
            history.error_message
            or f"Import marked as failed after running for more than {timeout_hours:g} hour(s)."
        )
        history.json_ext = history.json_ext or {}
        history.json_ext.update(
            {
                "task_status": "failed",
                "failed_reason": "stale_running_timeout",
                "stale_timeout_hours": timeout_hours,
                "marked_failed_at": timezone.now().isoformat(),
            }
        )

        update_fields = ["status", "error_message", "json_ext"]
        audit_user = user or history.user_initiated
        if audit_user:
            history.save(user=audit_user, update_fields=update_fields)
        else:
            PulledHistory.objects.filter(id=history.id).update(
                status=history.status,
                error_message=history.error_message,
                json_ext=history.json_ext,
            )
        updated += 1

    return updated
