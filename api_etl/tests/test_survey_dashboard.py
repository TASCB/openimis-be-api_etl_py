from __future__ import annotations

from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from api_etl.services import survey_dashboard_service as svc
from api_etl.services import hq_client


def _resp(json_payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_payload
    m.raise_for_status.return_value = None
    return m


class SplitQidTests(SimpleTestCase):
    def test_identity_with_version(self):
        self.assertEqual(svc._split_qid("11aea4ff-9bbe-48e7-96d5-7f1413305fcf$3"),
                         ("11aea4ff9bbe48e796d57f1413305fcf", 3))

    def test_plain_guid_dashless(self):
        self.assertEqual(svc._split_qid("11aea4ff9bbe48e796d57f1413305fcf"),
                         ("11aea4ff9bbe48e796d57f1413305fcf", None))

    def test_dashed_guid_roundtrip(self):
        plain = "11aea4ff9bbe48e796d57f1413305fcf"
        self.assertEqual(svc._dashed_guid(plain), "11aea4ff-9bbe-48e7-96d5-7f1413305fcf")
        self.assertEqual(svc._dashed_guid("short"), "short")


class StatusVocabularyTests(SimpleTestCase):
    VALID = {  # InterviewStatus enum, verified against HQ 22.02.5
        "Restored", "Created", "SupervisorAssigned", "InterviewerAssigned",
        "RejectedBySupervisor", "ReadyForInterview", "SentToCapi", "Restarted",
        "Completed", "ApprovedBySupervisor", "RejectedByHeadquarters",
        "ApprovedByHeadquarters", "Deleted",
    }

    def test_all_statuses_are_valid_enum_values(self):
        for st in svc.ALL_STATUSES:
            self.assertIn(st, self.VALID)

    def test_restarted_and_senttocapi_counted(self):
        self.assertIn("Restarted", svc.ALL_STATUSES)
        self.assertIn("SentToCapi", svc.ALL_STATUSES)
        self.assertNotIn("SentToCapital", svc.ALL_STATUSES)
        self.assertIn("Restarted", svc.IN_PROGRESS_STATUSES)


class InterviewsRequestParamsTests(SimpleTestCase):
    @patch.object(svc, "requests")
    def test_total_count_uses_pagesize_page(self, mock_requests):
        mock_requests.get.return_value = _resp({"Interviews": [], "TotalCount": 42})
        with patch.object(hq_client, "endpoint_base", return_value="http://hq/ws/api/v1"), \
             patch.object(hq_client, "request_kwargs", return_value={}):
            total = svc.fetch_total_count("11aea4ff9bbe48e796d57f1413305fcf$1", status="Completed")
        self.assertEqual(total, 42)
        params = mock_requests.get.call_args.kwargs.get("params") or {}
        self.assertEqual(params.get("pageSize"), 1)
        self.assertEqual(params.get("page"), 1)
        self.assertNotIn("limit", params)
        self.assertNotIn("offset", params)
        self.assertEqual(params.get("status"), "Completed")


class NumericReportParserTests(SimpleTestCase):
    def test_array_rows_trailing_columns(self):
        payload = {
            "draw": 1, "recordsTotal": 2, "recordsFiltered": 2,
            "data": [
                ["Team A", 10, 4.0, 4, 40, 1, 1, 4, 8, 9],
                ["Team B", 5, 6.0, 6, 30, 2, 2, 6, 9, 10],
            ],
        }
        m = svc._parse_numeric_report(payload)
        self.assertEqual(m, {"total": 70, "average": 4.7, "count": 15})

    def test_expand_teams_extra_leading_column(self):
        payload = {"data": [["Team A", "enum1", 10, 4.0, 4, 40, 1, 1, 4, 8, 9]]}
        m = svc._parse_numeric_report(payload)
        self.assertEqual(m["count"], 10)
        self.assertEqual(m["total"], 40)

    def test_totals_row_fallback(self):
        payload = {"data": [], "totals": ["AllTeams", 15, 4.7, 5, 70, 1, 1, 5, 9, 10]}
        m = svc._parse_numeric_report(payload)
        self.assertEqual(m, {"total": 70, "average": 4.7, "count": 15})

    def test_empty_report(self):
        self.assertIsNone(svc._parse_numeric_report({"data": [], "recordsTotal": 0}))
        self.assertIsNone(svc._parse_numeric_report(None))
        self.assertIsNone(svc._parse_numeric_report({"data": [["Team", "only-labels"]]}))

    def test_dict_rows_other_builds(self):
        payload = {"data": [{"TeamLead": "A", "count": 4, "sum": 20}]}
        m = svc._parse_numeric_report(payload)
        self.assertEqual(m, {"total": 20, "average": 5.0, "count": 4})


class StatisticsRequestParamsTests(SimpleTestCase):
    @patch.object(svc, "_hq_json_get")
    def test_version_and_statuses_sent(self, mock_get):
        mock_get.return_value = {"data": [["T", 4, 5.0, 5, 20, 1, 1, 5, 9, 9]]}
        m = svc._fetch_statistics_metric("11aea4ff9bbe48e796d57f1413305fcf$2", "hh_size")
        self.assertEqual(m["total"], 20)
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "statistics")
        params = kwargs["params"]
        self.assertEqual(params["Version"], 2)
        self.assertEqual(params["Question"], "hh_size")
        self.assertEqual(params["QuestionnaireId"], "11aea4ff9bbe48e796d57f1413305fcf")
        self.assertIn("Completed", params["statuses[]"])
        self.assertIn("ApprovedBySupervisor", params["statuses[]"])


class GraphqlNodeMappingTests(SimpleTestCase):
    def test_node_to_brief_canonicalizes(self):
        node = {
            "id": "1742f0f5-7db8-4cd8-92fa-c0e32440a23b",
            "key": "74-34-00-70",
            "status": "APPROVEDBYSUPERVISOR",
            "responsibleName": "rm4_enum",
            "responsibleRole": "INTERVIEWER",
            "supervisorName": "rm4_sup",
            "errorsCount": 0,
            "notAnsweredCount": 2,
            "createdDate": "2026-05-24T13:36:22.115+03:00",
            "updateDateUtc": "2026-05-25T10:56:41.678Z",
            "questionnaireId": "11aea4ff-9bbe-48e7-96d5-7f1413305fcf",
            "questionnaireVersion": 1,
        }
        brief = svc._graphql_node_to_brief(node)
        self.assertEqual(brief["InterviewId"], "1742f0f57db84cd892fac0e32440a23b")
        self.assertEqual(brief["Status"], "ApprovedBySupervisor")
        self.assertEqual(brief["SupervisorName"], "rm4_sup")
        self.assertEqual(brief["Key"], "74-34-00-70")
        fields = svc._brief_to_fields(brief)
        self.assertEqual(fields["questionnaire_id"], "11aea4ff9bbe48e796d57f1413305fcf")
        self.assertEqual(fields["status"], "ApprovedBySupervisor")


class CapabilityProbeTests(SimpleTestCase):
    def test_status_literal_uses_introspected_enum(self):
        caps = {"status_enum": {"APPROVEDBYSUPERVISOR": "APPROVEDBYSUPERVISOR",
                                "SENTTOCAPI": "SENT_TO_CAPI"}}
        self.assertEqual(hq_client.status_literal("ApprovedBySupervisor", caps), "APPROVEDBYSUPERVISOR")
        self.assertEqual(hq_client.status_literal("SentToCapi", caps), "SENT_TO_CAPI")
        self.assertEqual(hq_client.status_literal("Completed", {"status_enum": {}}), "COMPLETED")

    @patch.object(hq_client, "graphql_post")
    def test_sequential_counts_when_alias_broken(self, mock_post):
        caps = {"graphql": True, "graphql_alias": False, "status_enum": {}}
        mock_post.return_value = {"interviews": {"filteredCount": 7}}
        with patch.object(hq_client, "probe_capabilities", return_value=caps):
            counts = hq_client.graphql_status_counts([], ["Completed", "Restarted"])
        self.assertEqual(counts, {"Completed": 7, "Restarted": 7})
        self.assertEqual(mock_post.call_count, 2)

    @patch.object(hq_client, "graphql_post")
    def test_aliased_counts_single_request(self, mock_post):
        caps = {"graphql": True, "graphql_alias": True, "status_enum": {}}
        mock_post.return_value = {"s0": {"filteredCount": 3}, "s1": {"filteredCount": 4}}
        with patch.object(hq_client, "probe_capabilities", return_value=caps):
            counts = hq_client.graphql_status_counts([], ["Completed", "Restarted"])
        self.assertEqual(counts, {"Completed": 3, "Restarted": 4})
        self.assertEqual(mock_post.call_count, 1)
