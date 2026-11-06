from __future__ import annotations

import logging

from individual.services import WorkflowHandler, IndividualImportService

LOG = logging.getLogger(__name__)


class TargetingWorkflow(WorkflowHandler):
    name = "TARGETING"

    def __init__(self, user=None):
        self.user = user

    def run(self, payload: dict):
        upload_uuid = payload.get("upload_uuid")
        if not upload_uuid:
            return {"success": False, "message": "Missing upload_uuid in workflow payload"}
        try:
            IndividualImportService(self.user).create_task_with_importing_valid_items(upload_uuid)
            return {"success": True, "message": "Workflow triggered"}
        except Exception as e:
            LOG.exception("TargetingWorkflow.run() failed: %s", e)
            return {"success": False, "message": str(e)}
