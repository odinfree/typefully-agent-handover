import contextlib
import datetime
import io
import json
import pathlib
import sys
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import lane_fixture  # noqa: E402,F401
import typefully_smart_schedule as smart  # noqa: E402
import typefully_lib as cli  # noqa: E402


def empty_queue():
    return {"days": []}


def scheduled_x(at, title="PROPOSED: unrelated", preview="other topic"):
    local = smart.local_datetime(at)
    return {
        "days": [
            {
                "date": local.date().isoformat(),
                "items": [
                    {
                        "at": local.astimezone(datetime.timezone.utc).isoformat(),
                        "kind": "custom_time",
                        "draft": {
                            "id": 99,
                            "draft_title": title,
                            "preview": preview,
                            "x_post_enabled": True,
                            "linkedin_post_enabled": False,
                            "threads_post_enabled": False,
                            "bluesky_post_enabled": False,
                        },
                    }
                ],
            }
        ]
    }


class DraftSpecTests(unittest.TestCase):
    def test_all_four_platforms_are_required(self):
        with self.assertRaises(SystemExit) as raised:
            smart.parse_draft_specs(["x=1", "linkedin=2"])
        self.assertIn("all four platforms", str(raised.exception))

    def test_exact_mapping_is_parsed(self):
        parsed = smart.parse_draft_specs(
            ["x=1", "linkedin=2", "threads=3", "bluesky=4"]
        )
        self.assertEqual(
            parsed, {"x": 1, "linkedin": 2, "threads": 3, "bluesky": 4}
        )


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(
            2026, 7, 13, 6, 0, tzinfo=smart.LOCAL_TZ
        )
        self.draft_ids = {platform: index for index, platform in enumerate(smart.PLATFORMS, 1)}

    def test_campaign_has_distinct_platform_times_and_alternatives(self):
        schedule = smart.build_schedule(
            now=self.now,
            topic="privacy integration burden",
            kind="flagship",
            freshness="timely",
            queue=empty_queue(),
            x_analytics=[],
            horizon_days=7,
        )
        times = {item["at"] for item in schedule.values()}
        self.assertEqual(set(schedule), set(smart.PLATFORMS))
        self.assertEqual(len(times), 4)
        self.assertTrue(all(len(item["alternatives"]) == 2 for item in schedule.values()))
        anchor = schedule["x"]["at"]
        self.assertTrue(
            all(
                abs((item["at"] - anchor).total_seconds()) <= 48 * 3600
                for item in schedule.values()
            )
        )

        result = smart.serialize_schedule(
            schedule,
            self.draft_ids,
            "privacy integration burden",
            "flagship",
            "timely",
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["coverage"], list(smart.PLATFORMS))
        for platform in smart.PLATFORMS:
            self.assertIn(
                f"typefully publish {self.draft_ids[platform]} --at",
                result["recommendations"][platform]["preflight_command"],
            )

    def test_same_platform_collision_blocks_the_exact_slot(self):
        occupied = datetime.datetime(
            2026, 7, 15, 14, 30, tzinfo=smart.LOCAL_TZ
        )
        schedule = smart.build_schedule(
            now=self.now,
            topic="privacy integration burden",
            kind="flagship",
            freshness="timely",
            queue=scheduled_x(occupied),
            x_analytics=[],
            horizon_days=7,
        )
        self.assertNotEqual(schedule["x"]["at"], occupied)

    def test_topic_overlap_penalizes_nearby_inventory(self):
        occupied = datetime.datetime(
            2026, 7, 15, 10, 0, tzinfo=smart.LOCAL_TZ
        )
        queue = scheduled_x(
            occupied,
            title="PROPOSED: privacy integration burden",
            preview="privacy integration burden inside wrappers",
        )
        candidate = datetime.datetime(
            2026, 7, 15, 19, 30, tzinfo=smart.LOCAL_TZ
        )
        scored = smart.score_candidate(
            candidate,
            1,
            "x",
            "privacy integration burden",
            "timely",
            self.now,
            smart.scheduled_events(queue),
            {},
            [],
        )
        self.assertIn("nearby topic overlap penalty", scored["reasons"])

    def test_x_analytics_uses_small_sample_shrinkage(self):
        rows = []
        start = datetime.datetime(
            2026, 5, 6, 12, 30, tzinfo=datetime.timezone.utc
        )
        for day in range(8):
            at = start + datetime.timedelta(days=day * 7)
            rows.append(
                {
                    "created_at": at.isoformat(),
                    "metrics": {
                        "impressions": 5000,
                        "engagement": {"total": 250},
                    },
                }
            )
        rows.append(
            {
                "created_at": "2026-05-07T05:00:00+00:00",
                "metrics": {"impressions": 20, "engagement": {"total": 0}},
            }
        )
        candidate = datetime.datetime(
            2026, 7, 15, 14, 30, tzinfo=smart.LOCAL_TZ
        )
        adjustment, total, comparable = smart.x_analytics_adjustment(rows, candidate)
        self.assertGreater(adjustment, 0)
        self.assertEqual(total, 9)
        self.assertGreaterEqual(comparable, 1)


class CommandTests(unittest.TestCase):
    def test_smart_plan_is_read_only_and_covers_all_platforms(self):
        methods = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            methods.append(method)
            if "/drafts/" in path:
                draft_id = int(path.rsplit("/", 1)[-1])
                platform = smart.PLATFORMS[draft_id - 1]
                return {
                    "id": draft_id,
                    "status": "draft",
                    "publish_state": None,
                    "platforms": {
                        platform: {
                            "enabled": True,
                            "posts": [{"text": f"copy for {platform}"}],
                        }
                    },
                }
            if "/analytics/x/posts" in path:
                return {"results": [], "next": None}
            if path.endswith("/queue"):
                return empty_queue()
            if path.endswith("/"):
                return {"publishing_quota": {"remaining": 100}}
            raise AssertionError(path)

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft=["x=1", "linkedin=2", "threads=3", "bluesky=4"],
            topic="privacy integration burden",
            kind="flagship",
            freshness="timely",
            horizon_days=7,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "call", side_effect=fake_call):
            with contextlib.redirect_stdout(stdout):
                cli.cmd_smart_plan(args)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["coverage"], list(smart.PLATFORMS))
        self.assertFalse(result["public_action_executed"])
        self.assertTrue(methods)
        self.assertEqual(set(methods), {"GET"})


if __name__ == "__main__":
    unittest.main()
