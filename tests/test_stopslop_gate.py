import hashlib
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import lane_fixture  # noqa: E402,F401
import stopslop_gate as gate  # noqa: E402
import typefully_lib as cli  # noqa: E402


def sources(suffix=""):
    return [
        {
            "path": f"/vault/rule-{index}.md",
            "sha256": f"{index:064x}"[:-len(suffix) or None] + suffix,
            "content": f"rule {index}",
        }
        for index in range(1, 8)
    ]


def bundle(**overrides):
    value = {
        "schema": gate.BUNDLE_SCHEMA,
        "platforms": ["x"],
        "title": "PROPOSED: gate test",
        "posts": ["A specific public-safe claim."],
        "quote_post_url": None,
        "source": {"thread": "test", "public_boundary": "public", "x_sources": []},
        "gate": {name: True for name in gate.REQUIRED_ATTESTATIONS},
    }
    value.update(overrides)
    return value


class ReceiptTests(unittest.TestCase):
    def test_exact_bundle_and_vault_pass(self):
        value = bundle()
        receipt = gate.issue_receipt(value, now=1000, sources=sources(), key=b"key")
        self.assertTrue(
            gate.verify_receipt(
                receipt, value, now=1001, sources=sources(), key=b"key"
            )
        )

    def test_copy_change_invalidates_receipt(self):
        value = bundle()
        receipt = gate.issue_receipt(value, now=1000, sources=sources(), key=b"key")
        changed = bundle(posts=["A changed public-safe claim."])
        with self.assertRaises(SystemExit) as raised:
            gate.verify_receipt(
                receipt, changed, now=1001, sources=sources(), key=b"key"
            )
        self.assertIn("content changed", str(raised.exception))

    def test_vault_change_invalidates_receipt(self):
        value = bundle()
        receipt = gate.issue_receipt(value, now=1000, sources=sources(), key=b"key")
        with self.assertRaises(SystemExit) as raised:
            gate.verify_receipt(
                receipt, value, now=1001, sources=sources("f"), key=b"key"
            )
        self.assertIn("sources changed", str(raised.exception))

    def test_expired_receipt_is_rejected(self):
        value = bundle()
        receipt = gate.issue_receipt(value, now=1000, sources=sources(), key=b"key")
        with self.assertRaises(SystemExit) as raised:
            gate.verify_receipt(
                receipt, value, now=2000, sources=sources(), key=b"key"
            )
        self.assertIn("expired", str(raised.exception))

    def test_wrong_key_is_rejected(self):
        value = bundle()
        receipt = gate.issue_receipt(value, now=1000, sources=sources(), key=b"key")
        with self.assertRaises(SystemExit) as raised:
            gate.verify_receipt(
                receipt, value, now=1001, sources=sources(), key=b"other"
            )
        self.assertIn("invalid", str(raised.exception))


class BundleTests(unittest.TestCase):
    def test_hard_bans_fail_before_receipt(self):
        for text in (
            "This matters.",
            "A real shift.",
            "Not a tool, but infrastructure.",
            "Three things: one, two, three.",
            "A launch #starknet",
            "A launch 🚀",
        ):
            with self.subTest(text=text):
                with self.assertRaises(SystemExit):
                    gate.issue_receipt(
                        bundle(posts=[text]), now=1000, sources=sources(), key=b"key"
                    )

    def test_quote_url_must_be_direct_x_status(self):
        with self.assertRaises(SystemExit):
            gate.validate_bundle(bundle(quote_post_url="https://example.com/post/1"))
        gate.validate_bundle(
            bundle(quote_post_url="https://x.com/someone/status/123456789")
        )

    def test_incomplete_attestation_is_rejected(self):
        attestations = {name: True for name in gate.REQUIRED_ATTESTATIONS}
        attestations["second_reader_pass"] = False
        with self.assertRaises(SystemExit) as raised:
            gate.validate_bundle(bundle(gate=attestations))
        self.assertIn("second_reader_pass", str(raised.exception))

    def test_media_is_bound_by_absolute_path_and_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "evidence.png"
            path.write_bytes(b"public-safe-image")
            media = [
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ]
            gate.validate_bundle(bundle(media=media))
            path.write_bytes(b"changed")
            with self.assertRaises(SystemExit) as raised:
                gate.validate_bundle(bundle(media=media))
        self.assertIn("sha256 does not match", str(raised.exception))

    def test_typefully_payload_includes_quote_url(self):
        value = bundle(
            posts=["One post.", "Second post."],
            quote_post_url="https://x.com/someone/status/123456789",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bundle.json"
            path.write_text(json.dumps(value))
            args = types.SimpleNamespace(
                text=[], bundle=str(path), stopslop_receipt="receipt"
            )
            with mock.patch.object(gate, "verify_receipt", return_value=True):
                body, platforms = cli.create_draft_body(args)
        self.assertEqual(platforms, ["x"])
        self.assertEqual(body["draft_title"], "PROPOSED: gate test")
        self.assertEqual(
            body["platforms"]["x"]["posts"][0]["quote_post_url"],
            "https://x.com/someone/status/123456789",
        )

    def test_typefully_payload_attaches_media_to_first_post(self):
        value = bundle(posts=["One post.", "Second post."])
        args = types.SimpleNamespace(text=[], bundle=None, stopslop_receipt="receipt")
        body, _ = cli.create_draft_body(
            args,
            bundle=value,
            media_ids=["media-one", "media-two"],
        )
        posts = body["platforms"]["x"]["posts"]
        self.assertEqual(posts[0]["media_ids"], ["media-one", "media-two"])
        self.assertNotIn("media_ids", posts[1])

    def test_typefully_rejects_legacy_plain_text(self):
        args = types.SimpleNamespace(text=["hello"], bundle=None)
        with self.assertRaises(SystemExit) as raised:
            cli.create_draft_body(args)
        self.assertIn("plain-text draft creation is disabled", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class UrlColonTest(unittest.TestCase):
    def test_url_colons_are_not_decorative(self):
        clean = ["code is public, full writeup below.\n\nhttps://x.com/example/status/123"]
        self.assertEqual(gate.lint_posts(clean), [])

    def test_decorative_colon_still_fails_with_url_present(self):
        dirty = ["the point is simple: links win.\n\nhttps://x.com/example/status/123"]
        self.assertTrue(any("decorative colons" in e for e in gate.lint_posts(dirty)))

    def test_url_fragments_are_not_hashtags(self):
        clean = ["numbers in the discussion.\n\nhttps://github.com/org/repo/discussions/408#discussioncomment-17446486"]
        self.assertEqual(gate.lint_posts(clean), [])

    def test_real_hashtags_still_fail(self):
        dirty = ["big news #zk\n\nhttps://github.com/org/repo"]
        self.assertTrue(any("hashtags" in e for e in gate.lint_posts(dirty)))
