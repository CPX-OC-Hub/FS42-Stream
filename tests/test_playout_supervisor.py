import unittest
from datetime import datetime

from fs42stream.playout_supervisor import supervise_schedule_playout


class PlayoutSupervisorTests(unittest.TestCase):
    def test_supervise_schedule_playout_projects_current_block_items_and_upcoming_blocks(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Current Block",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [
                        {"path": "feature.mp4", "duration": 1800, "skip": 0, "content_type": "feature"},
                    ],
                },
                {
                    "title": "Next Block",
                    "start_time": "2026-06-17T10:30:00",
                    "end_time": "2026-06-17T11:00:00",
                    "plan": [{"path": "next.mp4", "duration": 1800, "skip": 0, "content_type": "feature"}],
                },
                {
                    "title": "Later Block",
                    "start_time": "2026-06-17T11:00:00",
                    "end_time": "2026-06-17T11:30:00",
                    "plan": [{"path": "later.mp4", "duration": 1800, "skip": 0, "content_type": "feature"}],
                },
            ],
        }

        state = supervise_schedule_playout(
            schedule,
            now=datetime(2026, 6, 17, 10, 5, 0),
            schedule_timezone="Europe/London",
        )

        self.assertEqual(state.selected.index, 0)
        self.assertEqual(state.selected.reason, "current")
        self.assertEqual(state.active_block["title"], "Current Block")
        self.assertEqual(state.active_block["plan"][0]["skip"], 300.0)
        self.assertEqual(state.current_item["path"], "feature.mp4")
        self.assertEqual(state.current_item["media_seek"], 300.0)
        self.assertEqual(state.next_block["title"], "Next Block")
        self.assertEqual([block["title"] for block in state.upcoming_blocks], ["Next Block", "Later Block"])
        self.assertEqual(state.projection.as_dict()["current_block"]["plan"][0]["skip"], 300.0)

        contract = state.contract_dict(schedule_now=datetime(2026, 6, 17, 10, 5, 0))
        self.assertEqual(contract["selection"], {"index": 0, "reason": "current"})
        self.assertEqual(contract["schedule_now"], "2026-06-17T10:05:00")
        self.assertEqual(contract["current_block"]["title"], "Current Block")
        self.assertEqual(contract["upcoming_blocks"][0]["title"], "Next Block")
        self.assertEqual(contract["timeline"][0]["path"], "feature.mp4")
        self.assertEqual(contract["current_item"]["media_seek"], 300.0)
        self.assertEqual(contract["catch_up"]["applied"], True)
        self.assertEqual(contract["catch_up"]["media_seek"], 300.0)
        self.assertEqual(contract["render_plan_item_count"], 1)
        self.assertEqual(contract["source_plan_item_count"], 1)

    def test_supervise_schedule_playout_honors_minimum_index_to_prevent_rewinding(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Current Block",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [{"path": "current.mp4", "duration": 1800, "skip": 0, "content_type": "feature"}],
                },
                {
                    "title": "Forced Next Block",
                    "start_time": "2026-06-17T10:30:00",
                    "end_time": "2026-06-17T11:00:00",
                    "plan": [{"path": "next.mp4", "duration": 1800, "skip": 0, "content_type": "feature"}],
                },
            ],
        }

        state = supervise_schedule_playout(
            schedule,
            now=datetime(2026, 6, 17, 10, 5, 0),
            schedule_timezone="Europe/London",
            minimum_index=1,
        )

        self.assertEqual(state.selected.index, 1)
        self.assertEqual(state.selected.reason, "next")
        self.assertEqual(state.active_block["title"], "Forced Next Block")
        self.assertEqual(state.upcoming_blocks, [])
        self.assertEqual(state.current_item, None)
        self.assertEqual(state.next_item["path"], "next.mp4")


if __name__ == "__main__":
    unittest.main()
