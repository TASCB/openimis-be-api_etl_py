"""DB-backed tests for the survey dashboard (run with --keepdb on the test_imis clone)."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from api_etl.apps import ApiEtlConfig
from api_etl.models import PulledHistory, SurveyDashboardSnapshot, SurveyInterviewCache
from api_etl.services import survey_dashboard_service as svc

QID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
IDENT = f"{QID}$2"


def _brief(iid, status, *, minutes_ago=5, responsible="enum1"):
    ts = (timezone.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "InterviewId": iid, "Key": f"k-{iid}", "Status": status, "ResponsibleName": responsible,
        "SupervisorName": "sup1", "QuestionnaireId": QID, "QuestionnaireVersion": 2,
        "CreatedDate": ts, "LastEntryDate": ts, "ServerLastUpdate": ts,
    }


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        SurveyInterviewCache.objects.filter(questionnaire_id=QID).delete()
        SurveyDashboardSnapshot.objects.filter(questionnaire_id__startswith=QID).delete()
        p1 = patch.object(svc, "fetch_supervisor_roster", return_value={"interviewerMap": {}})
        p2 = patch.object(svc, "_questionnaire_title_map", return_value={QID: "Test PAA"})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def upsert(self, *briefs):
        return svc._upsert_briefs(list(briefs))


class UpsertTests(_Base):
    def test_activity_at_is_stored_and_status_change_is_bulk_updated(self):
        self.assertEqual(self.upsert(_brief("i1", "Completed", minutes_ago=30), _brief("i2", "Completed")), (2, 2, 0))
        row = SurveyInterviewCache.objects.get(interview_id="i1")
        self.assertIsNotNone(row.activity_at)
        self.assertEqual(row.activity_at, row.last_entry_at_utc)

        seen, created, changed = self.upsert(_brief("i1", "ApprovedBySupervisor", minutes_ago=1))
        self.assertEqual((seen, created, changed), (1, 0, 1))
        row.refresh_from_db()
        self.assertEqual(row.status, "ApprovedBySupervisor")
        self.assertIsNotNone(row.status_changed_at)

    def test_feed_is_ordered_by_activity(self):
        self.upsert(_brief("old", "Completed", minutes_ago=300), _brief("new", "Completed", minutes_ago=1))
        ids = [r.interview_id for r in svc.list_interviews(questionnaire_id=IDENT)]
        self.assertEqual(ids, ["new", "old"])


class ListInterviewsTests(_Base):
    def test_csv_status_filter_and_from_date(self):
        self.upsert(
            _brief("a", "Completed", minutes_ago=5),
            _brief("b", "RejectedBySupervisor", minutes_ago=5),
            _brief("c", "InterviewerAssigned", minutes_ago=5),
            _brief("d", "Completed", minutes_ago=60 * 24 * 10),
        )
        got = {r.interview_id for r in svc.list_interviews(questionnaire_id=IDENT, status="Completed, RejectedBySupervisor")}
        self.assertEqual(got, {"a", "b", "d"})
        since = (timezone.now() - timedelta(days=2)).date().isoformat()
        got = {r.interview_id for r in svc.list_interviews(questionnaire_id=IDENT, status="Completed", from_date=since)}
        self.assertEqual(got, {"a"})


class HonestLastSyncedTests(_Base):
    def test_hq_down_records_a_failure_not_a_sync(self):
        with patch.object(svc, "fetch_status_counts", return_value={}):
            result = svc.poll_interviews(IDENT, with_sample=False)
        self.assertIsNone(result["polled_at"])
        self.assertIsNone(svc.last_polled_at())
        self.assertIsNotNone(svc.last_failed_at())
        self.assertTrue(svc._in_fail_backoff())
        self.assertIsNotNone(svc.compute_metrics(IDENT)["lastFailedAt"])

    def test_hq_answer_stamps_sync_and_clears_failure(self):
        cache.set(svc._LAST_FAIL_CACHE_KEY, (timezone.now() - timedelta(minutes=1)).isoformat(), None)
        with patch.object(svc, "fetch_status_counts", return_value={"Completed": 3}):
            result = svc.poll_interviews(IDENT, with_sample=False)
        self.assertIsNotNone(result["polled_at"])
        self.assertIsNotNone(svc.last_polled_at())
        self.assertIsNone(svc.last_failed_at())
        m = svc.compute_metrics(IDENT)
        self.assertEqual(m["totalInterviews"], 3)
        self.assertIsNone(m["lastFailedAt"])

    def test_self_heal_does_not_pre_stamp_last_synced(self):
        with patch.object(svc, "_enqueue_refresh") as enq:
            self.assertTrue(svc.maybe_async_refresh(None))
        self.assertTrue(enq.called)
        self.assertIsNone(svc.last_polled_at())


class SyncNowTests(_Base):
    def test_second_click_does_not_queue_a_second_poll(self):
        with patch.object(svc, "_enqueue_refresh") as enq:
            self.assertTrue(svc.start_sync(IDENT))
            self.assertFalse(svc.start_sync(IDENT))
            self.assertTrue(svc.sync_running(IDENT))
        self.assertEqual(enq.call_count, 1)
        self.assertEqual(enq.call_args.args, (IDENT, True, svc._sync_lock_key(IDENT)))

    def test_lock_released_after_the_poll(self):
        lock = svc._sync_lock_key(IDENT)
        with patch.object(svc, "_enqueue_refresh"):
            svc.start_sync(IDENT)
        with patch.object(svc, "refresh_dashboard", return_value={"total_count": 1}):
            svc.run_refresh(IDENT, True, lock)
        self.assertFalse(svc.sync_running(IDENT))

    def test_full_poll_due_queues_a_full_sync(self):
        cache.set(svc._LAST_POLL_CACHE_KEY, timezone.now().isoformat(), None)
        cache.set(svc._status_counts_key(IDENT), {"Completed": 1}, None)
        with patch.object(svc, "_enqueue_refresh") as enq:
            svc.maybe_async_refresh(IDENT)
        self.assertTrue(enq.call_args.args[1])  # with_sample


class StatisticsFirstTests(_Base):
    def test_harvest_skipped_when_statistics_answers(self):
        with patch.object(svc, "_fetch_statistics_metric",
                          return_value={"total": 132303, "average": 4.2, "count": 31231}), \
             patch.object(svc, "_harvest_hh_sizes") as harvest:
            self.assertEqual(svc._refresh_household(IDENT), 0)
        harvest.assert_not_called()
        m = svc.compute_metrics(IDENT)
        self.assertEqual(m["householdMembersTotal"], 132303)
        self.assertEqual(m["householdSizeSource"], "statistics")
        self.assertEqual(m["householdSizeCoverage"], 100.0)

    def test_harvest_is_the_fallback_when_statistics_fails(self):
        with patch.object(svc, "_fetch_statistics_metric", side_effect=RuntimeError("500")), \
             patch.object(svc, "_harvest_hh_sizes", return_value=4) as harvest:
            self.assertEqual(svc._refresh_household(IDENT), 4)
        harvest.assert_called_once()


class SnapshotKeyTests(_Base):
    def test_versions_do_not_overwrite_each_other(self):
        svc.take_daily_snapshot(IDENT, {"Completed": 5})
        svc.take_daily_snapshot(f"{QID}$3", {"Completed": 9})
        self.assertEqual(svc.status_history(IDENT)[-1]["completed"], 5)
        self.assertEqual(svc.status_history(f"{QID}$3")[-1]["completed"], 9)

    def test_legacy_guid_rows_are_still_read(self):
        day = svc._local(timezone.now()).date() - timedelta(days=3)
        SurveyDashboardSnapshot.objects.create(snapshot_date=day, questionnaire_id=QID, completed=7,
                                               cumulative_completed=7, hq_url=svc.current_hq_url())
        svc.take_daily_snapshot(IDENT, {"Completed": 8})
        hist = svc.status_history(IDENT)
        self.assertEqual([h["completed"] for h in hist], [7, 8])


class CurrentHqScopeTests(_Base):
    OLD_HQ = "http://old-hq.example:9700"

    def setUp(self):
        super().setUp()
        PulledHistory.objects.filter(questionnaire_id__startswith=QID).delete()
        for name, value in (("export_base_url", "http://new-hq.example:9700/"), ("export_workspace", "")):
            p = patch.object(ApiEtlConfig, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def test_upsert_tags_rows_with_the_current_hq(self):
        self.upsert(_brief("i1", "Completed"))
        self.assertEqual(SurveyInterviewCache.objects.get(interview_id="i1").hq_url, "http://new-hq.example:9700")

    def test_rows_from_another_hq_are_hidden(self):
        self.upsert(_brief("mine", "Completed"))
        SurveyInterviewCache.objects.create(interview_id="theirs", questionnaire_id=QID, questionnaire_version=2,
                                            status="Completed", hq_url=self.OLD_HQ)
        ids = [r.interview_id for r in svc.list_interviews(questionnaire_id=IDENT)]
        self.assertEqual(ids, ["mine"])

    def test_questionnaire_picker_skips_another_hq(self):
        other = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        SurveyInterviewCache.objects.filter(questionnaire_id=other).delete()
        SurveyInterviewCache.objects.create(interview_id="theirs", questionnaire_id=other, questionnaire_version=1,
                                            status="Completed", hq_url=self.OLD_HQ)
        self.upsert(_brief("mine", "Completed"))
        ids = {q["id"] for q in svc.list_dashboard_questionnaires()}
        self.assertIn(QID, ids)
        self.assertNotIn(other, ids)

    def test_snapshots_from_another_hq_are_hidden(self):
        day = svc._local(timezone.now()).date() - timedelta(days=2)
        SurveyDashboardSnapshot.objects.create(snapshot_date=day, questionnaire_id=IDENT, completed=99,
                                               hq_url=self.OLD_HQ)
        svc.take_daily_snapshot(IDENT, {"Completed": 4})
        self.assertEqual([h["completed"] for h in svc.status_history(IDENT)], [4])

    def test_cursor_is_kept_per_hq(self):
        key = svc._cursor_key(IDENT)
        with patch.object(ApiEtlConfig, "export_base_url", self.OLD_HQ):
            self.assertNotEqual(svc._cursor_key(IDENT), key)


class EagerCeleryTests(_Base):
    def test_eager_mode_runs_the_poll_in_a_thread_not_the_request(self):
        with patch.object(svc, "_celery_worker_available", return_value=False), \
             patch("threading.Thread") as thread, \
             patch("api_etl.tasks.poll_survey_dashboard_task.delay") as delay:
            svc._enqueue_refresh(IDENT, True, "k")
        delay.assert_not_called()
        thread.return_value.start.assert_called_once()

    def test_no_auto_backfill_without_a_worker(self):
        with patch.object(svc, "_cfg", side_effect=lambda n, d=None: True if n == "dashboard_hhsize_backfill_auto" else d), \
             patch.object(svc, "_needs_backfill", return_value=True), \
             patch.object(svc, "_celery_worker_available", return_value=False), \
             patch("api_etl.tasks.backfill_hh_size_task.delay") as delay:
            svc._maybe_enqueue_backfill(IDENT)
        delay.assert_not_called()
