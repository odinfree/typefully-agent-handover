import contextlib
import io
import json
import pathlib
import socket
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import lane_fixture  # noqa: E402,F401
import typefully_lib as cli  # noqa: E402


class FakeResponse:
    def __init__(self, obj):
        self.raw = json.dumps(obj).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.raw


def http_error(code, body=b""):
    return urllib.error.HTTPError(
        "https://api.typefully.com/test",
        code,
        "error",
        {"Retry-After": "1"},
        io.BytesIO(body),
    )


def fake_social_set():
    return {
        "id": cli.WRITE_SET,
        "name": "Example Set",
        "publishing_quota": {
            "used": 1,
            "remaining": 1499,
            "resets_at": "2026-08-01T00:00:00+02:00",
        },
        "platforms": {
            platform: {
                "platform": platform,
                "username": username,
                "profile_url": f"https://example.test/{username}",
            }
            for platform, username in cli.EXPECTED_TARGETS.items()
        },
    }


def fake_draft(status="draft", platforms=("x", "linkedin")):
    return {
        "id": 42,
        "status": status,
        "publish_state": None,
        "updated_at": "2026-07-11T12:00:00Z",
        "scheduled_date": None,
        "draft_title": "PROPOSED: test",
        "preview": "hello",
        "platforms": {
            platform: {
                "enabled": True,
                "posts": [{"text": f"hello {platform}"}],
            }
            for platform in platforms
        },
    }


class SafetyTests(unittest.TestCase):
    def test_mutating_path_rejects_noncanonical_set_before_network(self):
        with mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit) as raised:
                cli.call("POST", "/social-sets/999999/drafts", {})
        self.assertIn("111222", str(raised.exception))
        urlopen.assert_not_called()

    def test_read_command_allows_noncanonical_set(self):
        args = types.SimpleNamespace(set=999999, draft_id=7)
        stdout = io.StringIO()
        with mock.patch.object(cli, "call", return_value={"id": 7}) as call:
            with contextlib.redirect_stdout(stdout):
                cli.cmd_get(args)
        call.assert_called_once_with("GET", "/social-sets/999999/drafts/7")
        self.assertEqual(json.loads(stdout.getvalue()), {"id": 7})

    def test_empty_and_malformed_platform_lists_are_rejected(self):
        for value in ("", " ", ",", "x,", ",x"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    cli.parse_platforms(value)

    def test_duplicate_platform_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.parse_platforms("x,x")

    def test_omitted_platforms_keep_lane_default(self):
        self.assertEqual(cli.parse_platforms(None), list(cli.ALL_PLATFORMS))

    def test_unscoped_write_paths_are_rejected_before_network(self):
        for method, path in (
            ("PATCH", "/me"),
            ("DELETE", "/drafts/9"),
            ("PUT", "/user/settings"),
        ):
            with self.subTest(method=method, path=path):
                with mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
                    with self.assertRaises(SystemExit) as raised:
                        cli.call(method, path, {})
                self.assertIn("refusing unscoped", str(raised.exception))
                urlopen.assert_not_called()

    def test_put_to_foreign_set_is_rejected_before_network(self):
        with mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit) as raised:
                cli.call("PUT", "/social-sets/999999/queue/schedule", {})
        self.assertIn("refusing write to social set 999999", str(raised.exception))
        urlopen.assert_not_called()

    def test_non_normalized_write_paths_are_rejected(self):
        for path in (
            "/social-sets/111222/../999999/drafts",
            "/social-sets/00111222/drafts",
            "/social-sets//111222/drafts",
        ):
            with self.subTest(path=path):
                with mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
                    with self.assertRaises(SystemExit) as raised:
                        cli.call("POST", path, {})
                self.assertIn("refusing non-normalized", str(raised.exception))
                urlopen.assert_not_called()

    def test_invalid_at_is_rejected_before_draft_creation(self):
        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            text=["hello"],
            platforms="x",
            all=False,
            title=None,
            now=False,
            slot=False,
            at="2026-07-14T09:00:00",
        )
        with mock.patch.object(cli, "call") as call:
            with self.assertRaises(SystemExit):
                cli.cmd_post(args)
        call.assert_not_called()


class PaginationTests(unittest.TestCase):
    def test_collect_pages_walks_limit_offset_pages(self):
        def fake_call(method, path, body=None, params=None, timeout=None):
            self.assertEqual(method, "GET")
            if params["offset"] == 0:
                return {
                    "results": [{"id": 1}, {"id": 2}],
                    "count": 3,
                    "next": "page-2",
                }
            self.assertEqual(params["offset"], 2)
            return {"results": [{"id": 3}], "count": 3, "next": None}

        with mock.patch.object(cli, "call", side_effect=fake_call) as call:
            result = cli.collect_pages("/drafts")
        self.assertEqual([row["id"] for row in result["results"]], [1, 2, 3])
        self.assertTrue(result["complete"])
        self.assertEqual(call.call_count, 2)

    def test_drafts_full_is_one_aggregated_json_value(self):
        result = {
            "results": [{"id": 1}],
            "count": 1,
            "returned": 1,
            "offset": 0,
            "complete": True,
            "next_offset": None,
        }
        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            status=None,
            limit=None,
            offset=0,
            full=True,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "collect_pages", return_value=result):
            with contextlib.redirect_stdout(stdout):
                cli.cmd_drafts(args)
        self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_malformed_page_is_rejected_cleanly(self):
        with mock.patch.object(cli, "call", return_value=None):
            with self.assertRaises(SystemExit) as raised:
                cli.collect_pages("/drafts")
        self.assertIn("expected object", str(raised.exception))


class TransportTests(unittest.TestCase):
    def test_media_put_has_no_content_type_header(self):
        response = mock.Mock(status=204)
        response.read.return_value = b""
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "image.png"
            path.write_bytes(b"image-bytes")
            with mock.patch.object(
                cli.http.client,
                "HTTPSConnection",
                return_value=connection,
            ):
                cli.put_media_bytes(
                    "https://uploads.example.test/file.png?signature=one",
                    path,
                )
        connection.putrequest.assert_called_once_with(
            "PUT",
            "/file.png?signature=one",
            skip_accept_encoding=True,
        )
        connection.putheader.assert_called_once_with("Content-Length", "11")
        connection.endheaders.assert_called_once_with(b"image-bytes")

    def test_media_transport_retry_is_bounded(self):
        perform = mock.Mock(
            side_effect=[SystemExit("transport error during media"), "ready"]
        )
        with mock.patch.object(cli.time, "sleep") as sleep:
            self.assertEqual(cli.retry_media_write(perform), "ready")
        self.assertEqual(perform.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_call_supplies_timeout(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(
                cli.urllib.request,
                "urlopen",
                return_value=FakeResponse({"ok": True}),
            ) as urlopen:
                self.assertEqual(cli.call("GET", "/me"), {"ok": True})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], cli.REQUEST_TIMEOUT)

    def test_get_429_retries_then_succeeds(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "sleep") as sleep:
                with mock.patch.object(
                    cli.urllib.request,
                    "urlopen",
                    side_effect=[http_error(429), FakeResponse({"ok": True})],
                ) as urlopen:
                    self.assertEqual(cli.call("GET", "/me"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_get_retry_is_bounded(self):
        errors = [http_error(429), http_error(429), http_error(429)]
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "sleep"):
                with mock.patch.object(
                    cli.urllib.request, "urlopen", side_effect=errors
                ) as urlopen:
                    with self.assertRaises(SystemExit):
                        cli.call("GET", "/me")
        self.assertEqual(urlopen.call_count, 3)

    def test_mutation_transport_failure_is_not_retried(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(
                cli.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ) as urlopen:
                with self.assertRaises(SystemExit) as raised:
                    cli.call(
                        "PATCH",
                        f"/social-sets/{cli.WRITE_SET}/drafts/42",
                        {"publish_at": "now"},
                    )
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("Outcome is unknown", str(raised.exception))

    def test_mutation_503_warns_that_outcome_is_unknown(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(
                cli.urllib.request,
                "urlopen",
                side_effect=http_error(503),
            ) as urlopen:
                with self.assertRaises(SystemExit) as raised:
                    cli.call(
                        "PATCH",
                        f"/social-sets/{cli.WRITE_SET}/drafts/42",
                        {"publish_at": "now"},
                    )
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("Outcome is unknown", str(raised.exception))

    def test_timeout_is_clean_and_bounded(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "sleep"):
                with mock.patch.object(
                    cli.urllib.request,
                    "urlopen",
                    side_effect=socket.timeout("timed out"),
                ) as urlopen:
                    with self.assertRaises(SystemExit) as raised:
                        cli.call("GET", "/me")
        self.assertEqual(urlopen.call_count, 3)
        self.assertIn("transport error during GET /me", str(raised.exception))


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "schema": "typefully-confirm-v1",
            "action": "publish-now",
            "social_set_id": cli.WRITE_SET,
            "targets": {"x": {"username": "example_x"}},
            "draft": cli.draft_snapshot(fake_draft(platforms=("x",))),
            "publish_at": "now",
            "publishing_quota": {"remaining": 10},
        }

    def test_exact_confirmation_passes(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                token = cli.sign_plan(self.plan, 1100)
                cli.require_confirmation(token, self.plan)

    def test_missing_confirmation_is_fail_closed(self):
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        cli.require_confirmation(None, self.plan)
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["executed"])
        self.assertTrue(payload["confirmation_required"])

    def test_changed_plan_invalidates_confirmation(self):
        changed = json.loads(json.dumps(self.plan))
        changed["draft"]["platforms"]["x"]["posts"][0]["text"] = "changed"
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                token = cli.sign_plan(self.plan, 1100)
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        cli.require_confirmation(token, changed)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("changed", json.loads(stdout.getvalue())["error"])

    def test_expired_confirmation_is_rejected(self):
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1200):
                token = cli.sign_plan(self.plan, 1100)
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        cli.require_confirmation(token, self.plan)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("expired", json.loads(stdout.getvalue())["error"])

    def test_token_signed_with_different_key_is_rejected(self):
        with mock.patch.object(cli, "api_key", return_value="key-a"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                token = cli.sign_plan(self.plan, 1100)
        with mock.patch.object(cli, "api_key", return_value="key-b"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.require_confirmation(token, self.plan)
        self.assertEqual(raised.exception.code, 2)

    def test_action_substitution_invalidates_token(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                token = cli.sign_plan(self.plan, 1100)
                switched = json.loads(json.dumps(self.plan))
                switched["action"] = "delete"
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.require_confirmation(token, switched)
        self.assertEqual(raised.exception.code, 2)

    def test_draft_id_substitution_invalidates_token(self):
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                token = cli.sign_plan(self.plan, 1100)
                switched = json.loads(json.dumps(self.plan))
                switched["draft"]["id"] = 99
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.require_confirmation(token, switched)
        self.assertEqual(raised.exception.code, 2)


class KeyFileTests(unittest.TestCase):
    @unittest.skipIf(cli.os.name == "nt", "POSIX permissions required")
    def test_key_file_permission_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            key_dir = pathlib.Path(directory) / "typefully"
            key_dir.mkdir()
            key_path = key_dir / "api_key"
            key_path.write_text("secret-key\n")
            key_path.chmod(0o644)
            env = {"XDG_CONFIG_HOME": directory, "TYPEFULLY_API_KEY": ""}
            with mock.patch.dict(cli.os.environ, env):
                with self.assertRaises(SystemExit) as raised:
                    cli.api_key()
                self.assertIn("chmod 600", str(raised.exception))
                key_path.chmod(0o600)
                self.assertEqual(cli.api_key(), "secret-key")

    def test_env_key_takes_precedence_over_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_dir = pathlib.Path(directory) / "typefully"
            key_dir.mkdir()
            key_path = key_dir / "api_key"
            key_path.write_text("file-key")
            key_path.chmod(0o600)
            env = {"XDG_CONFIG_HOME": directory, "TYPEFULLY_API_KEY": " env-key "}
            with mock.patch.dict(cli.os.environ, env):
                self.assertEqual(cli.api_key(), "env-key")


class LockTests(unittest.TestCase):
    @unittest.skipIf(cli.fcntl is None, "Unix flock is required")
    def test_transition_lock_rejects_concurrent_process_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(cli, "state_dir", return_value=pathlib.Path(directory)):
                with cli.transition_lock():
                    with self.assertRaises(SystemExit) as raised:
                        with cli.transition_lock():
                            pass
        self.assertIn("already in progress", str(raised.exception))


class WorkflowTests(unittest.TestCase):
    def test_publish_without_confirmation_only_reads(self):
        draft = fake_draft(platforms=("x",))
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append((method, path))
            if path.endswith("/drafts/42"):
                return draft
            return fake_social_set()

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=True,
            slot=False,
            at=None,
            confirm=None,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli, "call", side_effect=fake_call):
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        cli.cmd_publish(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        self.assertEqual(json.loads(stdout.getvalue())["plan"]["draft"]["id"], 42)

    def test_exact_publish_confirmation_patches_then_returns_one_json(self):
        draft = fake_draft(platforms=("x", "linkedin"))
        social_set = fake_social_set()
        with mock.patch.object(cli, "call", return_value=social_set):
            plan = cli.build_transition_plan(
                cli.WRITE_SET, draft, "publish-now", "now"
            )
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)

        published = {
            **draft,
            "status": "published",
            "publish_state": "finished",
            "published_at": "2026-07-11T12:01:00Z",
            "x_published_url": "https://x.test/1",
            "linkedin_published_url": "https://linkedin.test/1",
        }
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append(method)
            if method == "GET" and path.endswith("/drafts/42"):
                return published if "PATCH" in calls else draft
            if method == "GET":
                return social_set
            return draft

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=True,
            slot=False,
            at=None,
            confirm=token,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            cli.cmd_publish(args)
        receipt = json.loads(stdout.getvalue())
        self.assertTrue(receipt["success"])
        self.assertEqual(receipt["missing_platforms"], [])
        self.assertEqual(calls, ["GET", "GET", "GET", "PATCH", "GET"])

    def test_partial_publish_is_reported_as_failure(self):
        published = {
            **fake_draft(platforms=("x", "linkedin")),
            "status": "published",
            "publish_state": "finished",
            "x_published_url": "https://x.test/1",
        }
        with mock.patch.object(cli, "call", return_value=published):
            receipt = cli.poll_publish(cli.WRITE_SET, 42, ["x", "linkedin"])
        self.assertFalse(receipt["success"])
        self.assertEqual(receipt["missing_platforms"], ["linkedin"])

    def test_finished_error_is_reported_as_failure(self):
        errored = {
            **fake_draft(status="error", platforms=("x",)),
            "publish_state": "finished",
        }
        with mock.patch.object(cli, "call", return_value=errored):
            receipt = cli.poll_publish(cli.WRITE_SET, 42, ["x"])
        self.assertFalse(receipt["success"])
        self.assertEqual(receipt["status"], "error")

    def test_poll_transport_failure_retries_until_success(self):
        published = {
            **fake_draft(platforms=("x",)),
            "status": "published",
            "publish_state": "finished",
            "x_published_url": "https://x.test/1",
        }
        failures = [SystemExit("offline"), SystemExit("offline")]

        def fake_call(method, path, body=None, params=None, timeout=None):
            if failures:
                raise failures.pop(0)
            return published

        with mock.patch.object(cli.time, "sleep"):
            with mock.patch.object(cli, "call", side_effect=fake_call):
                receipt = cli.poll_publish(cli.WRITE_SET, 42, ["x"])
        self.assertTrue(receipt["success"])
        self.assertEqual(receipt["urls"], {"x": "https://x.test/1"})

    def test_poll_transport_failure_exhausts_deadline_then_reports_unknown(self):
        with mock.patch.object(cli, "PUBLISH_TIMEOUT", 0.05):
            with mock.patch.object(cli, "call", side_effect=SystemExit("offline")):
                receipt = cli.poll_publish(cli.WRITE_SET, 42, ["x"])
        self.assertTrue(receipt["executed"])
        self.assertFalse(receipt["success"])
        self.assertIn("offline", receipt["error"])
        self.assertIn("outcome is unknown", receipt["error"])

    def test_post_now_stages_draft_and_emits_one_json(self):
        draft = fake_draft(platforms=("x",))
        created = {
            "id": 42,
            "status": "draft",
            "preview": "hello",
            "private_url": "https://typefully.test/draft/42",
        }
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append(method)
            if method == "POST":
                return created
            if path.endswith("/drafts/42"):
                return draft
            return fake_social_set()

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            text=["hello"],
            platforms="x",
            all=False,
            title=None,
            now=True,
            slot=False,
            at=None,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(
                cli,
                "load_reviewed_bundle",
                return_value={"platforms": ["x"], "media": []},
            ):
                with mock.patch.object(cli, "upload_bundle_media", return_value=[]):
                    with mock.patch.object(
                        cli,
                        "create_draft_body",
                        return_value=(
                            {
                                "draft_title": "PROPOSED: hello",
                                "platforms": {
                                    "x": {
                                        "enabled": True,
                                        "posts": [{"text": "hello"}],
                                    }
                                },
                            },
                            ["x"],
                        ),
                    ):
                        with mock.patch.object(cli, "call", side_effect=fake_call):
                            with contextlib.redirect_stdout(stdout):
                                with self.assertRaises(SystemExit) as raised:
                                    cli.cmd_post(args)
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["draft_created"])
        self.assertFalse(payload["public_action_executed"])
        self.assertEqual(calls, ["POST", "GET", "GET"])

    def test_update_replaces_reviewed_plain_draft_and_verifies(self):
        draft = fake_draft(platforms=("x",))
        updated = {
            **draft,
            "private_url": "https://typefully.test/draft/42",
            "platforms": {
                "x": {
                    "enabled": True,
                    "posts": [
                        {
                            "text": "revised",
                            "media_ids": ["media-1"],
                        }
                    ],
                }
            },
        }
        body = {
            "draft_title": "PROPOSED: test",
            "platforms": {
                "x": {
                    "enabled": True,
                    "posts": [
                        {
                            "text": "revised",
                            "media_ids": ["media-1"],
                        }
                    ],
                }
            },
        }
        calls = []

        def fake_call(method, path, request_body=None, params=None, timeout=None):
            calls.append((method, request_body))
            if method == "PATCH":
                return updated
            return draft if len(calls) == 1 else updated

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            bundle="bundle.json",
            stopslop_receipt="oss1.receipt",
        )
        stdout = io.StringIO()
        with mock.patch.object(
            cli,
            "load_reviewed_bundle",
            return_value={
                "title": "PROPOSED: test",
                "platforms": ["x"],
                "media": [{"path": "image.png"}],
            },
        ):
            with mock.patch.object(cli, "upload_bundle_media", return_value=["media-1"]):
                with mock.patch.object(
                    cli, "create_draft_body", return_value=(body, ["x"])
                ):
                    with mock.patch.object(cli, "call", side_effect=fake_call):
                        with contextlib.redirect_stdout(stdout):
                            cli.cmd_update(args)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["success"])
        self.assertFalse(payload["public_action_executed"])
        self.assertEqual([method for method, _ in calls], ["GET", "PATCH", "GET"])
        self.assertEqual(calls[1][1], body)

    def test_update_rejects_scheduled_draft_before_media_upload(self):
        draft = fake_draft(status="scheduled", platforms=("x",))
        draft["scheduled_date"] = "2026-07-13T12:00:00Z"
        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            bundle="bundle.json",
            stopslop_receipt="oss1.receipt",
        )
        with mock.patch.object(
            cli,
            "load_reviewed_bundle",
            return_value={"title": "PROPOSED: test", "platforms": ["x"], "media": []},
        ):
            with mock.patch.object(cli, "call", return_value=draft) as call:
                with mock.patch.object(cli, "upload_bundle_media") as upload:
                    with self.assertRaises(SystemExit) as raised:
                        cli.cmd_update(args)
        self.assertIn("unscheduled plain draft", str(raised.exception))
        call.assert_called_once()
        upload.assert_not_called()

    def test_update_refuses_platform_change(self):
        draft = fake_draft(platforms=("x",))
        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            bundle="bundle.json",
            stopslop_receipt="oss1.receipt",
        )
        with mock.patch.object(
            cli,
            "load_reviewed_bundle",
            return_value={
                "title": "PROPOSED: test",
                "platforms": ["linkedin"],
                "media": [],
            },
        ):
            with mock.patch.object(cli, "call", return_value=draft):
                with mock.patch.object(cli, "upload_bundle_media") as upload:
                    with self.assertRaises(SystemExit) as raised:
                        cli.cmd_update(args)
        self.assertIn("do not match draft platforms", str(raised.exception))
        upload.assert_not_called()

    def test_update_refuses_title_change(self):
        draft = fake_draft(platforms=("x",))
        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            bundle="bundle.json",
            stopslop_receipt="oss1.receipt",
        )
        with mock.patch.object(
            cli,
            "load_reviewed_bundle",
            return_value={
                "title": "PROPOSED: changed",
                "platforms": ["x"],
                "media": [],
            },
        ):
            with mock.patch.object(cli, "call", return_value=draft):
                with mock.patch.object(cli, "upload_bundle_media") as upload:
                    with self.assertRaises(SystemExit) as raised:
                        cli.cmd_update(args)
        self.assertIn("title must match", str(raised.exception))
        upload.assert_not_called()

    def test_delete_without_confirmation_never_deletes(self):
        draft = fake_draft()
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append(method)
            return draft if path.endswith("/drafts/42") else fake_social_set()

        args = types.SimpleNamespace(set=cli.WRITE_SET, draft_id=42, confirm=None)
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli, "call", side_effect=fake_call):
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        cli.cmd_delete(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("DELETE", calls)

    def test_empty_draft_can_reach_delete_preflight(self):
        draft = fake_draft(platforms=())
        args = types.SimpleNamespace(set=cli.WRITE_SET, draft_id=42, confirm=None)
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli, "call", return_value=draft) as call:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        cli.cmd_delete(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["plan"]["draft"]["platforms"], {})
        call.assert_called_once()

    def test_confirmed_empty_draft_delete_executes_once(self):
        draft = fake_draft(platforms=())
        plan = cli.build_transition_plan(cli.WRITE_SET, draft, "delete")
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append(method)
            return None if method == "DELETE" else draft

        args = types.SimpleNamespace(set=cli.WRITE_SET, draft_id=42, confirm=token)
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            cli.cmd_delete(args)
        self.assertEqual(calls, ["GET", "GET", "DELETE"])
        self.assertTrue(json.loads(stdout.getvalue())["success"])

    def test_command_level_foreign_set_is_rejected_before_any_call(self):
        cases = (
            (
                cli.cmd_publish,
                types.SimpleNamespace(
                    set=999999, draft_id=7, now=True, slot=False, at=None, confirm=None
                ),
            ),
            (
                cli.cmd_delete,
                types.SimpleNamespace(set=999999, draft_id=7, confirm=None),
            ),
            (
                cli.cmd_post,
                types.SimpleNamespace(
                    set=999999,
                    text=["hello"],
                    platforms="x",
                    all=False,
                    title=None,
                    now=False,
                    slot=False,
                    at=None,
                ),
            ),
            (
                cli.cmd_update,
                types.SimpleNamespace(
                    set=999999,
                    draft_id=7,
                    bundle="bundle.json",
                    stopslop_receipt="oss1.receipt",
                ),
            ),
        )
        for command, args in cases:
            with self.subTest(command=command.__name__):
                with mock.patch.object(cli, "call") as call:
                    with self.assertRaises(SystemExit) as raised:
                        command(args)
                self.assertIn(
                    "refusing write to social set 999999", str(raised.exception)
                )
                call.assert_not_called()

    def test_target_identity_mismatch_refuses_publish(self):
        social_set = fake_social_set()
        social_set["platforms"]["x"]["username"] = "someone_else"
        draft = fake_draft(platforms=("x",))
        with mock.patch.object(cli, "call", return_value=social_set):
            with self.assertRaises(SystemExit) as raised:
                cli.build_transition_plan(cli.WRITE_SET, draft, "publish-now", "now")
        self.assertIn("target changed", str(raised.exception))

    def test_write_transport_failure_emits_structured_receipt(self):
        draft = fake_draft(platforms=())
        plan = cli.build_transition_plan(cli.WRITE_SET, draft, "delete")
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)

        def fake_call(method, path, body=None, params=None, timeout=None):
            if method == "DELETE":
                raise SystemExit(
                    "transport error during DELETE /social-sets/111222/drafts/42: "
                    "boom. Outcome is unknown; inspect Typefully before retrying."
                )
            return draft

        args = types.SimpleNamespace(set=cli.WRITE_SET, draft_id=42, confirm=token)
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            with self.assertRaises(SystemExit) as raised:
                                cli.cmd_delete(args)
        self.assertEqual(raised.exception.code, 1)
        receipt = json.loads(stdout.getvalue())
        self.assertIsNone(receipt["executed"])
        self.assertEqual(receipt["outcome"], "unknown")
        self.assertFalse(receipt["success"])
        self.assertIn("transport error", receipt["error"])

    def test_write_client_error_emits_rejected_receipt(self):
        draft = fake_draft(platforms=("x",))
        social_set = fake_social_set()
        with mock.patch.object(cli, "call", return_value=social_set):
            plan = cli.build_transition_plan(
                cli.WRITE_SET, draft, "publish-now", "now"
            )
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)

        def fake_call(method, path, body=None, params=None, timeout=None):
            if method == "PATCH":
                raise SystemExit("HTTP 422 PATCH /social-sets/111222/drafts/42")
            if method == "GET" and path.endswith("/drafts/42"):
                return draft
            return social_set

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=True,
            slot=False,
            at=None,
            confirm=token,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            with self.assertRaises(SystemExit) as raised:
                                cli.cmd_publish(args)
        self.assertEqual(raised.exception.code, 1)
        receipt = json.loads(stdout.getvalue())
        self.assertFalse(receipt["executed"])
        self.assertEqual(receipt["outcome"], "rejected")
        self.assertEqual(receipt["expected_platforms"], ["x"])

    def test_confirmed_schedule_at_is_verified(self):
        draft = fake_draft(platforms=("x",))
        social_set = fake_social_set()
        publish_at = "2099-07-14T09:00:00+00:00"
        with mock.patch.object(cli, "call", return_value=social_set):
            plan = cli.build_transition_plan(
                cli.WRITE_SET, draft, "schedule-at", publish_at
            )
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)
        scheduled = {
            **draft,
            "status": "scheduled",
            "scheduled_date": "2099-07-14T09:00:00Z",
        }
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append(method)
            if method == "PATCH":
                return draft
            if path.endswith("/drafts/42"):
                return scheduled if "PATCH" in calls else draft
            return social_set

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=False,
            slot=False,
            at=publish_at,
            confirm=token,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            cli.cmd_publish(args)
        receipt = json.loads(stdout.getvalue())
        self.assertTrue(receipt["success"])
        self.assertEqual(receipt["action"], "schedule-at")

    def test_schedule_verification_transport_failure_returns_receipt(self):
        draft = fake_draft(platforms=("x",))
        social_set = fake_social_set()
        publish_at = "2099-07-14T09:00:00+00:00"
        with mock.patch.object(cli, "call", return_value=social_set):
            plan = cli.build_transition_plan(
                cli.WRITE_SET, draft, "schedule-at", publish_at
            )
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)
        calls = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            calls.append(method)
            if method == "PATCH":
                return draft
            if path.endswith("/drafts/42"):
                if "PATCH" in calls:
                    raise SystemExit("offline")
                return draft
            return social_set

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=False,
            slot=False,
            at=publish_at,
            confirm=token,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            with self.assertRaises(SystemExit) as raised:
                                cli.cmd_publish(args)
        self.assertEqual(raised.exception.code, 1)
        receipt = json.loads(stdout.getvalue())
        self.assertTrue(receipt["executed"])
        self.assertFalse(receipt["success"])
        self.assertIn("outcome is unknown", receipt["error"])

    def test_changed_queue_slot_invalidates_confirmation(self):
        draft = fake_draft(platforms=("x",))
        social_set = fake_social_set()
        slot_a = "2099-07-14T09:00:00Z"
        slot_b = "2099-07-14T10:00:00Z"
        with mock.patch.object(cli, "call", return_value=social_set):
            plan = cli.build_transition_plan(
                cli.WRITE_SET, draft, "schedule-slot", slot_a
            )
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)
        methods = []

        def fake_call(method, path, body=None, params=None, timeout=None):
            methods.append(method)
            return draft if path.endswith("/drafts/42") else social_set

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=False,
            slot=True,
            at=None,
            confirm=token,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli, "next_free_slot", side_effect=[slot_a, slot_b]
                    ):
                        with mock.patch.object(
                            cli,
                            "transition_lock",
                            return_value=contextlib.nullcontext(),
                        ):
                            with contextlib.redirect_stdout(stdout):
                                with self.assertRaises(SystemExit) as raised:
                                    cli.cmd_publish(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("PATCH", methods)
        self.assertEqual(
            json.loads(stdout.getvalue())["plan"]["publish_at"], slot_b
        )

    def test_last_second_draft_change_invalidates_confirmation(self):
        draft = fake_draft(platforms=("x",))
        changed = json.loads(json.dumps(draft))
        changed["updated_at"] = "2026-07-11T12:01:00Z"
        changed["platforms"]["x"]["posts"][0]["text"] = "changed"
        social_set = fake_social_set()
        with mock.patch.object(cli, "call", return_value=social_set):
            plan = cli.build_transition_plan(
                cli.WRITE_SET, draft, "publish-now", "now"
            )
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            token = cli.sign_plan(plan, 2000)
        methods = []
        drafts = iter([draft, changed])

        def fake_call(method, path, body=None, params=None, timeout=None):
            methods.append(method)
            return next(drafts) if path.endswith("/drafts/42") else social_set

        args = types.SimpleNamespace(
            set=cli.WRITE_SET,
            draft_id=42,
            now=True,
            slot=False,
            at=None,
            confirm=token,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "api_key", return_value="test-key"):
            with mock.patch.object(cli.time, "time", return_value=1000):
                with mock.patch.object(cli, "call", side_effect=fake_call):
                    with mock.patch.object(
                        cli,
                        "transition_lock",
                        return_value=contextlib.nullcontext(),
                    ):
                        with contextlib.redirect_stdout(stdout):
                            with self.assertRaises(SystemExit) as raised:
                                cli.cmd_publish(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("PATCH", methods)


if __name__ == "__main__":
    unittest.main()
