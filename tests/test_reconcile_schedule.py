import io
import json
import unittest
from datetime import datetime
from unittest import mock

from fs42stream.reconcile_schedule import _wait_for_validation, main


CURRENT_SCHEDULE = {
    "network_name": "Example Network",
    "schedule_blocks": [{"title": "Current", "start_time": "2026-07-01T10:00:00", "end_time": "2026-07-01T11:00:00", "plan": []}],
}
STALE_SUMMARY = {"schedule_summary": {"network_id": "Example Network", "end": "2026-07-01T09:00:00"}}
FRESH_SUMMARY = {"schedule_summary": {"network_id": "Example Network", "end": "2026-07-01T11:00:00"}}


class ReconcileScheduleCommandTests(unittest.TestCase):
    def test_validation_without_an_explicit_now_uses_a_fresh_current_time_for_each_poll(self):
        first_poll_time = datetime.fromisoformat("2026-07-01T10:10:02")
        second_poll_time = datetime.fromisoformat("2026-07-01T10:10:04")
        inspected_at = []

        def inspect(_client, *, channel, now, schedule_timezone):
            inspected_at.append(now)
            return {
                "summary_fresh": len(inspected_at) == 2,
                "full_schedule_current": len(inspected_at) == 2,
            }

        with mock.patch("fs42stream.reconcile_schedule.datetime") as clock, mock.patch(
            "fs42stream.reconcile_schedule.time.monotonic", side_effect=[100, 100, 100]
        ), mock.patch("fs42stream.reconcile_schedule.inspect_schedule", side_effect=inspect):
            clock.now.side_effect = [first_poll_time, second_poll_time]
            _wait_for_validation(
                mock.Mock(),
                channel="Example Network",
                now=None,
                schedule_timezone=None,
                wait_seconds=1,
                poll_interval=1,
            )

        self.assertEqual(inspected_at, [first_poll_time, second_poll_time])

    def test_dry_run_compares_stale_summary_and_current_full_schedule_without_mutating_fs42(self):
        client = mock.Mock()
        client.fetch_schedule_summary.return_value = STALE_SUMMARY
        client.fetch_schedule.return_value = CURRENT_SCHEDULE
        stdout = io.StringIO()

        with mock.patch("fs42stream.reconcile_schedule.FS42ScheduleClient", return_value=client), mock.patch("sys.stdout", stdout):
            rc = main([
                "--channel", "Example Network",
                "--api-base-url", "http://fs42.example:4242",
                "--schedule-timezone", "",
                "--now", "2026-07-01T10:10:00",
            ])

        self.assertEqual(rc, 0)
        client.reconcile_schedule.assert_not_called()
        client.reset_schedule.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["reconciliation_needed"])
        self.assertTrue(report["full_schedule_current"])
        self.assertEqual(report["action"], "dry-run")

    def test_apply_reconciles_then_validates_fresh_summary_and_current_full_schedule_before_success(self):
        client = mock.Mock()
        client.fetch_schedule_summary.side_effect = [STALE_SUMMARY, FRESH_SUMMARY]
        client.fetch_schedule.side_effect = [CURRENT_SCHEDULE, CURRENT_SCHEDULE]
        client.reconcile_schedule.return_value = {"status": "accepted"}
        stdout = io.StringIO()

        with mock.patch("fs42stream.reconcile_schedule.FS42ScheduleClient", return_value=client), mock.patch("sys.stdout", stdout):
            rc = main([
                "--channel", "Example Network",
                "--api-base-url", "http://fs42.example:4242",
                "--schedule-timezone", "",
                "--now", "2026-07-01T10:10:00",
                "--apply",
                "--wait-seconds", "0",
            ])

        self.assertEqual(rc, 0)
        client.reconcile_schedule.assert_called_once_with("Example Network")
        client.reset_schedule.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["action"], "reconciled")
        self.assertTrue(report["validation"]["summary_fresh"])
        self.assertTrue(report["validation"]["full_schedule_current"])

    def test_apply_refuses_to_mutate_when_full_schedule_has_no_current_coverage(self):
        expired_schedule = {"network_name": "Example Network", "schedule_blocks": [{"title": "Expired", "start_time": "2026-07-01T08:00:00", "end_time": "2026-07-01T09:00:00", "plan": []}]}
        client = mock.Mock()
        client.fetch_schedule_summary.return_value = STALE_SUMMARY
        client.fetch_schedule.return_value = expired_schedule
        stdout = io.StringIO()

        with mock.patch("fs42stream.reconcile_schedule.FS42ScheduleClient", return_value=client), mock.patch("sys.stdout", stdout):
            rc = main([
                "--channel", "Example Network",
                "--api-base-url", "http://fs42.example:4242",
                "--schedule-timezone", "",
                "--now", "2026-07-01T10:10:00",
                "--apply",
            ])

        self.assertEqual(rc, 2)
        client.reconcile_schedule.assert_not_called()
        client.reset_schedule.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["action"], "refused-no-current-full-schedule")


if __name__ == "__main__":
    unittest.main()
