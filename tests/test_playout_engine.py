import unittest
from datetime import datetime
from pathlib import Path

from fs42stream.live_controller import LiveController, LiveControllerConfig
from fs42stream.playout_engine import build_block_timeline, playout_snapshot, project_playout_state, resolve_block_playout, trim_block_to_wallclock

from tests.test_live_controller import FakeBlockRunner, FakeScheduleClient


class PlayoutEngineTests(unittest.TestCase):
    def test_build_block_timeline_and_current_item_are_wallclock_anchored(self):
        block = {
            "title": "Star Trek The Next Generation",
            "start_time": "2026-06-25T22:00:00",
            "end_time": "2026-06-25T22:42:00",
            "plan": [
                {"path": "intro.ts", "duration": 10, "skip": 0, "content_type": "bump", "media_type": "video", "is_stream": False},
                {"path": "part1.avi", "duration": 501, "skip": 0, "content_type": "feature", "media_type": "video", "is_stream": False},
                {"path": "adbreak.mp4", "duration": 30, "skip": 0, "content_type": "commercial", "media_type": "video", "is_stream": False},
                {"path": "part2.avi", "duration": 120, "skip": 500, "content_type": "feature", "media_type": "video", "is_stream": False},
            ],
        }

        timeline = build_block_timeline(block)
        snapshot = playout_snapshot(block, now=datetime.fromisoformat("2026-06-25T22:09:05"))

        self.assertEqual(len(timeline), 4)
        self.assertEqual(timeline[0]["wallclock_start"], "2026-06-25T22:00:00")
        self.assertEqual(timeline[0]["wallclock_end"], "2026-06-25T22:00:10")
        self.assertEqual(timeline[2]["content_type"], "commercial")
        self.assertEqual(timeline[2]["wallclock_start"], "2026-06-25T22:08:31")
        self.assertEqual(timeline[2]["wallclock_end"], "2026-06-25T22:09:01")
        self.assertEqual(snapshot["current_item"]["index"], 3)
        self.assertEqual(snapshot["current_item"]["path"], "part2.avi")
        self.assertEqual(snapshot["current_item"]["current_offset_in_item"], 4.0)
        self.assertEqual(snapshot["current_item"]["media_seek"], 504.0)
        self.assertEqual(snapshot["next_item"], None)

    def test_trim_block_to_wallclock_returns_remaining_plan_and_seek_diagnostics(self):
        block = {
            "title": "Block",
            "start_time": "2026-06-17T10:00:00",
            "end_time": "2026-06-17T10:30:00",
            "plan": [
                {"path": "a.mp4", "duration": 60, "skip": 0, "content_type": "bump"},
                {"path": "b.mp4", "duration": 300, "skip": 10, "content_type": "feature"},
                {"path": "c.mp4", "duration": 90, "skip": 0, "content_type": "commercial"},
            ],
        }

        trimmed_block, trim = trim_block_to_wallclock(
            block,
            now=datetime.fromisoformat("2026-06-17T10:03:00"),
            schedule_timezone="Europe/London",
        )

        self.assertTrue(trim["applied"])
        self.assertEqual(trim["start_plan_index"], 1)
        self.assertEqual(trim["offset_in_item"], 120.0)
        self.assertEqual(trim["media_seek"], 130.0)
        self.assertEqual(trimmed_block["plan"][0]["path"], "b.mp4")
        self.assertEqual(trimmed_block["plan"][0]["skip"], 130.0)
        self.assertEqual(trimmed_block["plan"][0]["duration"], 180.0)
        self.assertEqual(trimmed_block["plan"][1]["path"], "c.mp4")

    def test_resolve_block_playout_returns_engine_state_and_remaining_render_plan(self):
        block = {
            "title": "Block",
            "start_time": "2026-06-17T10:00:00",
            "end_time": "2026-06-17T10:30:00",
            "plan": [
                {"path": "a.mp4", "duration": 60, "skip": 0, "content_type": "bump"},
                {"path": "b.mp4", "duration": 300, "skip": 10, "content_type": "feature"},
                {"path": "c.mp4", "duration": 90, "skip": 0, "content_type": "commercial"},
            ],
        }

        decision = resolve_block_playout(
            block,
            now=datetime.fromisoformat("2026-06-17T10:03:00"),
            schedule_timezone="Europe/London",
            selection_index=4,
            selection_reason="next",
        )

        self.assertTrue(decision.catch_up["applied"])
        self.assertEqual(decision.snapshot["current_item"]["path"], "b.mp4")
        self.assertEqual(decision.snapshot["current_item"]["media_seek"], 130.0)
        self.assertEqual(decision.render_plan[0]["path"], "b.mp4")
        self.assertEqual(decision.render_plan[0]["skip"], 130.0)
        self.assertEqual(decision.render_plan[0]["duration"], 180.0)
        self.assertEqual(decision.render_plan[1]["path"], "c.mp4")
        self.assertEqual(decision.render_state.title, "Block")
        self.assertEqual(decision.render_state.start_time, "2026-06-17T10:00:00")
        self.assertEqual(decision.render_state.end_time, "2026-06-17T10:30:00")
        self.assertIs(decision.render_state.source, block)
        self.assertEqual(decision.render_state.plan[0]["path"], "b.mp4")
        self.assertEqual(decision.render_state.plan[0]["skip"], 130.0)
        self.assertEqual(decision.render_state.plan[1]["path"], "c.mp4")
        self.assertEqual(decision.render_state.selection.index, 4)
        self.assertEqual(decision.render_state.selection.reason, "next")
        self.assertEqual(decision.render_state.selection.block_title, "Block")
        self.assertEqual(decision.render_state.selection.start_time, "2026-06-17T10:00:00")
        self.assertEqual(decision.render_state.selection.end_time, "2026-06-17T10:30:00")

    def test_project_playout_state_unifies_current_next_block_and_item_projection(self):
        current_block = {
            "title": "Current Block",
            "start_time": "2026-06-17T10:00:00",
            "end_time": "2026-06-17T10:30:00",
            "plan": [
                {"path": "a.mp4", "duration": 60, "skip": 0, "content_type": "bump"},
                {"path": "b.mp4", "duration": 300, "skip": 10, "content_type": "feature"},
            ],
        }
        next_block = {
            "title": "Next Block",
            "start_time": "2026-06-17T10:30:00",
            "end_time": "2026-06-17T11:00:00",
        }

        decision = resolve_block_playout(
            current_block,
            now=datetime.fromisoformat("2026-06-17T10:03:00"),
            schedule_timezone="Europe/London",
            selection_index=4,
            selection_reason="current",
        )

        projection = project_playout_state(decision, next_block=next_block, schedule_now=datetime.fromisoformat("2026-06-17T10:03:00"))

        self.assertEqual(projection["current_block"]["index"], 4)
        self.assertEqual(projection["current_block"]["title"], "Current Block")
        self.assertEqual(projection["current_block"]["plan"][0]["path"], "b.mp4")
        self.assertEqual(projection["current_block"]["plan"][0]["skip"], 130.0)
        self.assertEqual(projection["next_block"]["title"], "Next Block")
        self.assertEqual(projection["current_item"]["path"], "b.mp4")
        self.assertEqual(projection["current_item"]["media_seek"], 130.0)
        self.assertIsNone(projection["next_item"])
        self.assertEqual(projection["schedule_now"], "2026-06-17T10:03:00")

    def test_live_controller_status_includes_playout_snapshot_from_engine(self):
        updates = []
        controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=FakeBlockRunner())

        controller.run(
            LiveControllerConfig(
                channel="Sky One",
                output_root=Path(datetime.now().strftime("/tmp/fs42-playout-%Y%m%d%H%M%S")),
                max_blocks=1,
                duration_limit=10,
                now=datetime(2026, 6, 17, 10, 5, 0),
                dry_run=True,
                status_callback=updates.append,
            )
        )

        self.assertGreaterEqual(len(updates), 1)
        snapshot = updates[0]["playout"]
        self.assertEqual(snapshot["block_title"], "First Live Block")
        self.assertEqual(snapshot["block_elapsed"], 300.0)
        self.assertEqual(snapshot["current_item"]["path"], "catalog/first.mp4")
        self.assertEqual(snapshot["current_item"]["media_seek"], 300.0)
        self.assertEqual(snapshot["timeline"][0]["wallclock_start"], "2026-06-17T10:00:00")


if __name__ == "__main__":
    unittest.main()
