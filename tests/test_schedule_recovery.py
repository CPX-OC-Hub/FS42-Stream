import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fs42stream.live_controller import LiveController, LiveControllerConfig, _select_shared_schedule_block
from fs42stream.schedule_recovery import RecoveringScheduleClient


PRIMARY_SCHEDULE = {
    "network_name": "Example Network",
    "schedule_blocks": [{"title": "Primary", "start_time": "2026-07-01T10:00:00", "end_time": "2026-07-01T11:00:00", "plan": []}],
}
FALLBACK_SCHEDULE = {
    "network_name": "Example Network",
    "schedule_blocks": [{"title": "BRB", "start_time": "2026-07-01T10:00:00", "end_time": "2026-07-01T11:00:00", "plan": []}],
}


class ScriptedScheduleClient:
    def __init__(self, *, schedules, summaries, base_url, reconciliation_results=()):
        self.schedules = list(schedules)
        self.summaries = list(summaries)
        self.reconciliation_results = list(reconciliation_results)
        self.base_url = base_url
        self.schedule_calls = 0
        self.summary_calls = 0
        self.reconciliation_calls = []

    def fetch_schedule(self, channel, *, expected_blocks=None):
        self.schedule_calls += 1
        value = self.schedules.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def fetch_schedule_summary(self, channel):
        self.summary_calls += 1
        value = self.summaries.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def reconcile_schedule(self, channel):
        self.reconciliation_calls.append(channel)
        if self.reconciliation_results:
            value = self.reconciliation_results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return {"status": "accepted"}


class RecoveringScheduleClientTests(unittest.TestCase):
    def test_live_controller_keeps_fallback_schedule_and_exposes_degraded_upstream_status(self):
        primary = ScriptedScheduleClient(
            schedules=[],
            summaries=[OSError("no route to upstream")],
            base_url="http://upstream:4242",
        )
        fallback = ScriptedScheduleClient(
            schedules=[FALLBACK_SCHEDULE],
            summaries=[{"schedule_summary": {"network_id": "Example Network"}}],
            base_url="http://127.0.0.1:4242",
        )
        schedule_client = RecoveringScheduleClient(primary=primary, fallback=fallback, recovery_interval=30)
        updates = []

        class BlockRunner:
            def run(self, config):
                self.schedule = config.schedule
                return {"status": "ok", "hls_next_start_number": 1, "ffmpeg": {"returncode": 0}, "plan": []}

        runner = BlockRunner()
        with tempfile.TemporaryDirectory() as tmp:
            LiveController(schedule_client=schedule_client, block_runner=runner).run(
                LiveControllerConfig(
                    channel="Example Network",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=10,
                    now=datetime.fromisoformat("2026-07-01T10:10:00"),
                    status_callback=updates.append,
                )
            )

        self.assertEqual(runner.schedule, FALLBACK_SCHEDULE)
        self.assertTrue(updates[0]["stale_schedule"]["active"])
        self.assertEqual(updates[0]["stale_schedule"]["reason"], "upstream-unavailable")

    def test_uses_explicit_fallback_and_reports_degraded_when_upstream_is_unreachable(self):
        primary = ScriptedScheduleClient(
            schedules=[OSError("no route to upstream")],
            summaries=[OSError("no route to upstream")],
            base_url="http://upstream:4242",
        )
        fallback = ScriptedScheduleClient(
            schedules=[FALLBACK_SCHEDULE],
            summaries=[{"schedule_summary": {"network_id": "Example Network"}}],
            base_url="http://127.0.0.1:4242",
        )
        client = RecoveringScheduleClient(primary=primary, fallback=fallback, recovery_interval=30)

        schedule = client.fetch_schedule("Example Network")

        self.assertEqual(schedule, FALLBACK_SCHEDULE)
        self.assertEqual(primary.summary_calls, 1)
        self.assertEqual(primary.schedule_calls, 1)
        self.assertEqual(client.fetch_schedule_summary("Example Network"), {"schedule_summary": {"network_id": "Example Network"}})
        self.assertEqual(client.status()["active"], True)
        self.assertEqual(client.status()["source"], "fallback")
        self.assertEqual(client.status()["reason"], "upstream-unavailable")

    def test_only_leaves_fallback_after_summary_and_full_schedule_both_succeed(self):
        primary = ScriptedScheduleClient(
            schedules=[OSError("full schedule timeout"), PRIMARY_SCHEDULE],
            summaries=[OSError("summary unavailable"), {"schedule_summary": {"network_id": "Example Network"}}, {"schedule_summary": {"network_id": "Example Network"}}],
            base_url="http://upstream:4242",
        )
        fallback = ScriptedScheduleClient(
            schedules=[FALLBACK_SCHEDULE, FALLBACK_SCHEDULE],
            summaries=[{"schedule_summary": {"network_id": "Example Network"}}, {"schedule_summary": {"network_id": "Example Network"}}],
            base_url="http://127.0.0.1:4242",
        )
        client = RecoveringScheduleClient(
            primary=primary,
            fallback=fallback,
            recovery_interval=30,
            schedule_timezone=None,
            clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00"),
        )
        self.assertEqual(client.fetch_schedule("Example Network"), FALLBACK_SCHEDULE)
        self.assertTrue(client.recheck_upstream("Example Network"))
        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)
        self.assertTrue(client.status()["active"])
        self.assertEqual(client.status()["reason"], "summary-stale-full-schedule-valid")
        self.assertEqual(client.status()["source"], "primary")

    def test_uses_last_good_schedule_when_explicit_fallback_is_unavailable(self):
        primary = ScriptedScheduleClient(
            schedules=[PRIMARY_SCHEDULE, OSError("timeout")],
            summaries=[{"schedule_summary": {"network_id": "Example Network"}}, OSError("timeout")],
            base_url="http://upstream:4242",
        )
        fallback = ScriptedScheduleClient(
            schedules=[OSError("fallback offline")],
            summaries=[],
            base_url="http://127.0.0.1:4242",
        )
        client = RecoveringScheduleClient(primary=primary, fallback=fallback, recovery_interval=30)

        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)
        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)
        self.assertEqual(client.status()["source"], "cache")
        self.assertTrue(client.status()["active"])

    def test_summary_timeout_with_current_full_schedule_uses_primary_without_placeholder(self):
        primary = ScriptedScheduleClient(
            schedules=[PRIMARY_SCHEDULE],
            summaries=[TimeoutError("summary timed out")],
            base_url="http://upstream:4242",
        )
        fallback = ScriptedScheduleClient(
            schedules=[FALLBACK_SCHEDULE],
            summaries=[{}],
            base_url="http://127.0.0.1:4242",
        )
        client = RecoveringScheduleClient(
            primary=primary,
            fallback=fallback,
            recovery_interval=30,
            schedule_timezone=None,
            clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00"),
        )

        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)

        self.assertEqual(primary.summary_calls, 1)
        self.assertEqual(primary.schedule_calls, 1)
        self.assertEqual(fallback.schedule_calls, 0)
        self.assertEqual(primary.reconciliation_calls, ["Example Network"])
        status = client.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["source"], "primary")
        self.assertEqual(status["reason"], "summary-unavailable-full-schedule-valid")
        self.assertTrue(status["full_schedule_current"])
        self.assertFalse(status["requires_placeholder"])

    def test_summary_error_with_current_full_schedule_uses_primary_without_fallback_or_cache(self):
        primary = ScriptedScheduleClient(
            schedules=[PRIMARY_SCHEDULE],
            summaries=[OSError("summary endpoint returned 500")],
            base_url="http://upstream:4242",
        )
        client = RecoveringScheduleClient(
            primary=primary,
            recovery_interval=30,
            schedule_timezone=None,
            clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00"),
        )

        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)

        self.assertEqual(primary.schedule_calls, 1)
        status = client.status()
        self.assertEqual(status["source"], "primary")
        self.assertEqual(status["reason"], "summary-unavailable-full-schedule-valid")
        self.assertFalse(status["requires_placeholder"])

    def test_summary_failure_without_current_full_schedule_allows_fallback(self):
        expired_schedule = {
            "network_name": "Example Network",
            "schedule_blocks": [{"title": "Expired", "start_time": "2026-07-01T08:00:00", "end_time": "2026-07-01T09:00:00", "plan": []}],
        }
        primary = ScriptedScheduleClient(
            schedules=[expired_schedule],
            summaries=[TimeoutError("summary timed out")],
            base_url="http://upstream:4242",
        )
        fallback = ScriptedScheduleClient(
            schedules=[FALLBACK_SCHEDULE],
            summaries=[{}],
            base_url="http://127.0.0.1:4242",
        )
        client = RecoveringScheduleClient(
            primary=primary,
            fallback=fallback,
            recovery_interval=30,
            schedule_timezone=None,
            clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00"),
        )

        self.assertEqual(client.fetch_schedule("Example Network"), FALLBACK_SCHEDULE)

        self.assertEqual(primary.schedule_calls, 1)
        self.assertEqual(fallback.schedule_calls, 1)
        status = client.status()
        self.assertEqual(status["source"], "fallback")
        self.assertEqual(status["reason"], "summary-unavailable-no-current-full-schedule")

    def test_stale_summary_with_a_current_full_schedule_runs_one_bounded_reconciliation_without_forcing_fallback(self):
        summary = {"schedule_summary": {"network_id": "Example Network", "end": "2026-07-01T09:00:00"}}
        primary = ScriptedScheduleClient(
            schedules=[PRIMARY_SCHEDULE, PRIMARY_SCHEDULE],
            summaries=[summary, summary],
            base_url="http://upstream:4242",
        )
        client = RecoveringScheduleClient(
            primary=primary,
            recovery_interval=30,
            schedule_timezone=None,
            clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00"),
            reconciliation_cooldown=300,
        )

        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)
        self.assertEqual(client.fetch_schedule("Example Network"), PRIMARY_SCHEDULE)

        self.assertEqual(primary.reconciliation_calls, ["Example Network"])
        status = client.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "summary-stale-full-schedule-valid")
        self.assertTrue(status["full_schedule_current"])
        self.assertFalse(status["requires_placeholder"])
        selection = _select_shared_schedule_block(
            config=LiveControllerConfig(channel="Example Network", schedule_timezone=None, clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00")),
            schedule_client=client,
            cursor=None,
            simulated_cursor=False,
            minimum_index=0,
        )
        self.assertEqual(selection.playout_state.decision.render_state.title, "Primary")

    def test_stale_summary_without_a_current_full_schedule_requires_brb_fallback_and_does_not_reconcile(self):
        expired_schedule = {
            "network_name": "Example Network",
            "schedule_blocks": [{"title": "Expired", "start_time": "2026-07-01T08:00:00", "end_time": "2026-07-01T09:00:00", "plan": []}],
        }
        primary = ScriptedScheduleClient(
            schedules=[expired_schedule, expired_schedule],
            summaries=[
                {"schedule_summary": {"network_id": "Example Network", "end": "2026-07-01T09:00:00"}},
                {"schedule_summary": {"network_id": "Example Network", "end": "2026-07-01T09:00:00"}},
            ],
            base_url="http://upstream:4242",
        )
        client = RecoveringScheduleClient(
            primary=primary,
            recovery_interval=30,
            schedule_timezone=None,
            clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00"),
        )

        self.assertEqual(client.fetch_schedule("Example Network"), expired_schedule)

        self.assertEqual(primary.reconciliation_calls, [])
        status = client.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "summary-stale-no-current-full-schedule")
        self.assertFalse(status["full_schedule_current"])
        self.assertTrue(status["requires_placeholder"])
        selection = _select_shared_schedule_block(
            config=LiveControllerConfig(channel="Example Network", schedule_timezone=None, clock=lambda: datetime.fromisoformat("2026-07-01T10:10:00")),
            schedule_client=client,
            cursor=None,
            simulated_cursor=False,
            minimum_index=0,
        )
        self.assertEqual(selection.playout_state.decision.render_state.title, "Schedule stale - BRB")


if __name__ == "__main__":
    unittest.main()
