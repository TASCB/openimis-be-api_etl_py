import unittest
from unittest.mock import MagicMock, patch
from api_etl.sinks.individual_import_sink import IndividualImportSink


class ApprovalBoundaryTests(unittest.TestCase):
    def test_staged_import_and_update_create_tasks_without_running_workflow(self):
        for update, task_method in [(False, 'create_task_with_importing_valid_items'),
                                    (True, 'create_task_with_update_valid_items')]:
            with self.subTest(update=update):
                sink = object.__new__(IndividualImportSink)
                sink.user = MagicMock()
                sink.svc = MagicMock()
                sink.svc._save_sources.return_value.uuid = 'upload-uuid'
                with patch('individual.services.IndividualTaskCreatorService') as factory:
                    result = sink._stage_and_create_task('file', 'workflow', 'group_code', update=update)
                    getattr(factory.return_value, task_method).assert_called_once_with('upload-uuid')
                sink.svc._trigger_workflow.assert_not_called()
                self.assertTrue(result['maker_checker'])

    def test_promoted_external_identifier_is_used_for_update_detection(self):
        sink = object.__new__(IndividualImportSink)
        self.assertEqual(sink._get_data_id({'external_id':'existing', 'json_ext':{}},
                                         'json_ext__external_id'), 'existing')
        self.assertEqual(sink._get_data_id({'external_id':'flat', 'json_ext':{'external_id':'nested'}},
                                         'json_ext__external_id'), 'nested')
