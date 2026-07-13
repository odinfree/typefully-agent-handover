"""Stop Slop gate for Typefully draft creation.

Receipts bind the exact content bundle to the current contents of the
team's Stop Slop rules (a local clone of the public stop-slop-refined
repository). They are deliberately short-lived and are checked again
immediately before a Typefully draft is created.
"""

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse


DEFAULT_RULES_DIR = pathlib.Path.home() / "stop-slop-refined"
BUNDLE_SCHEMA = "thread-to-typefully-v1"
RECEIPT_TTL_SECONDS = 10 * 60
RECEIPT_RE = re.compile(
    r"^oss1\.(\d+)\.([0-9a-f]{64})\.([0-9a-f]{64})\.([0-9a-f]{32})$"
)
REQUIRED_ATTESTATIONS = (
    "live_rules_loaded",
    "ai_role_checked",
    "reader_fit_checked",
    "stopslop_rewrite_pass",
    "voice_pass",
    "second_reader_pass",
)
MEDIA_FILENAME_RE = re.compile(
    r"^[a-zA-Z0-9_.()\-]+\.(?:jpg|jpeg|png|webp|gif|mp4|mov|pdf)$",
    flags=re.IGNORECASE,
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def rules_root():
    return pathlib.Path(
        os.environ.get("STOPSLOP_RULES_DIR", DEFAULT_RULES_DIR)
    ).expanduser()


def load_sources(root=None):
    root = pathlib.Path(root) if root else rules_root()
    if not root.is_dir():
        sys.exit(
            f"Stop Slop rules directory not found: {root}. Clone "
            "https://github.com/welttowelt/stop-slop-refined there or set "
            "STOPSLOP_RULES_DIR."
        )
    paths = sorted(p for p in root.rglob("*.md") if ".git" not in p.parts)
    if not paths:
        sys.exit(f"no .md rules found under {root}")
    sources = []
    for path in paths:
        raw = path.read_bytes()
        sources.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": raw.decode("utf-8"),
            }
        )
    return sources


def source_manifest(sources):
    return [{"path": item["path"], "sha256": item["sha256"]} for item in sources]


def source_digest(sources):
    return digest(source_manifest(sources))


def load_bundle(path):
    try:
        bundle = json.loads(pathlib.Path(path).read_text())
    except OSError as error:
        sys.exit(f"cannot read content bundle {path}: {error}")
    except json.JSONDecodeError as error:
        sys.exit(f"invalid content bundle JSON {path}: {error}")
    validate_bundle(bundle)
    return bundle


def validate_quote_url(url):
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        sys.exit("quote_post_url must be a direct HTTPS x.com or twitter.com URL")
    if not re.fullmatch(r"/[^/]+/status/\d+/?", parsed.path):
        sys.exit("quote_post_url must point directly to an X status")
    if parsed.query or parsed.fragment:
        sys.exit("quote_post_url must not contain query parameters or a fragment")


def validate_media(media):
    if media is None:
        return
    if not isinstance(media, list) or len(media) > 4:
        sys.exit("content bundle media must be a list with at most four items")
    seen_paths = set()
    for index, item in enumerate(media, start=1):
        if not isinstance(item, dict):
            sys.exit(f"content bundle media item {index} must be an object")
        path_value = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(path_value, str) or not pathlib.Path(path_value).is_absolute():
            sys.exit(f"content bundle media item {index} path must be absolute")
        path = pathlib.Path(path_value)
        if not path.is_file():
            sys.exit(f"content bundle media item {index} is not a file: {path}")
        if path_value in seen_paths:
            sys.exit(f"content bundle media item {index} duplicates an earlier path")
        seen_paths.add(path_value)
        if not MEDIA_FILENAME_RE.fullmatch(path.name):
            sys.exit(
                f"content bundle media item {index} filename is unsupported: {path.name}"
            )
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            sys.exit(f"content bundle media item {index} sha256 is malformed")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(expected_hash, actual_hash):
            sys.exit(f"content bundle media item {index} sha256 does not match: {path}")


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get("schema") != BUNDLE_SCHEMA:
        sys.exit(f"content bundle must use schema {BUNDLE_SCHEMA}")
    platforms = bundle.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        sys.exit("content bundle platforms must be a non-empty list")
    allowed = {"x", "linkedin", "threads", "bluesky"}
    if len(set(platforms)) != len(platforms) or any(item not in allowed for item in platforms):
        sys.exit("content bundle contains duplicate or unsupported platforms")
    posts = bundle.get("posts")
    if not isinstance(posts, list) or not posts or any(not isinstance(item, str) or not item.strip() for item in posts):
        sys.exit("content bundle posts must be a non-empty list of non-empty strings")
    validate_media(bundle.get("media"))
    title = bundle.get("title")
    if not isinstance(title, str) or not title.startswith("PROPOSED:"):
        sys.exit("content bundle title must start with PROPOSED:")
    quote_url = bundle.get("quote_post_url")
    if quote_url is not None:
        if platforms != ["x"]:
            sys.exit("quote_post_url is only supported for an X-only content bundle")
        validate_quote_url(quote_url)
    gate = bundle.get("gate")
    if not isinstance(gate, dict):
        sys.exit("content bundle is missing gate attestations")
    missing = [name for name in REQUIRED_ATTESTATIONS if gate.get(name) is not True]
    if missing:
        sys.exit("content bundle has incomplete gate attestations: " + ", ".join(missing))


def lint_posts(posts):
    rules = (
        (r"\bmatters?\b", "banned word: matters"),
        (r"\breal(?:ly)?\b", "banned vague emphasis: real/really"),
        (r"\bturns? into\b", "banned transformation phrase: turns into"),
        (r"\bbecomes? real\b", "banned transformation phrase: becomes real"),
        (r"\bit stops being\b", "banned transformation phrase: it stops being"),
        (r"\bnot\b[^.!?\n]{0,100}\b(?:but|it(?:'|’)s|it is)\b", "banned binary contrast"),
        (r"\bisn(?:'|’)t\b[^.!?\n]{0,100}\bit(?:'|’)s\b", "banned binary contrast"),
        (r"[#＃][\w]+", "hashtags are disabled for X copy"),
    )
    errors = []
    for index, post in enumerate(posts, start=1):
        post_without_urls = re.sub(r"https?://\S+", "", post)
        for pattern, message in rules:
            if re.search(pattern, post_without_urls, flags=re.IGNORECASE):
                errors.append(f"post {index}: {message}")
        for character, message in (
            ("—", "em dash is banned"),
            (";", "semicolon is banned"),
            ("“", "curly quotes are banned"),
            ("”", "curly quotes are banned"),
            ("‘", "curly quotes are banned"),
            ("’", "curly quotes are banned"),
        ):
            if character in post:
                errors.append(f"post {index}: {message}")
        if ":" in post_without_urls:
            errors.append(f"post {index}: decorative colons are disabled")
        if re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", post):
            errors.append(f"post {index}: emojis are disabled for X copy")
        if re.search(r"(^|\s)(#{1,6}|[-*+]\s|>\s|```)", post, flags=re.MULTILINE):
            errors.append(f"post {index}: markdown residue")
    return sorted(set(errors))


def signing_key():
    # Import lazily so this module remains independently testable.
    import typefully_lib

    return typefully_lib.api_key().encode("utf-8")


def receipt_message(expires, vault_hash, bundle_hash):
    return f"stopslop-gate-v1\0{expires}\0{vault_hash}\0{bundle_hash}".encode("utf-8")


def issue_receipt(bundle, now=None, sources=None, key=None):
    validate_bundle(bundle)
    errors = lint_posts(bundle["posts"])
    if errors:
        sys.exit("Stop Slop hard-check failed:\n- " + "\n- ".join(errors))
    sources = sources or load_sources()
    expires = int(now if now is not None else time.time()) + RECEIPT_TTL_SECONDS
    vault_hash = source_digest(sources)
    bundle_hash = digest(bundle)
    key = key if key is not None else signing_key()
    signature = hmac.new(key, receipt_message(expires, vault_hash, bundle_hash), hashlib.sha256).hexdigest()[:32]
    return f"oss1.{expires}.{vault_hash}.{bundle_hash}.{signature}"


def verify_receipt(receipt, bundle, now=None, sources=None, key=None):
    validate_bundle(bundle)
    match = RECEIPT_RE.fullmatch(receipt or "")
    if not match:
        sys.exit("missing or malformed live Stop Slop receipt")
    expires, supplied_vault, supplied_bundle, supplied_signature = match.groups()
    if int(expires) < int(now if now is not None else time.time()):
        sys.exit("Stop Slop receipt expired; reload the rules and review again")
    sources = sources or load_sources()
    current_vault = source_digest(sources)
    current_bundle = digest(bundle)
    if not hmac.compare_digest(supplied_vault, current_vault):
        sys.exit("Stop Slop sources changed; reload and review the copy again")
    if not hmac.compare_digest(supplied_bundle, current_bundle):
        sys.exit("content changed after the Stop Slop pass; review the exact copy again")
    key = key if key is not None else signing_key()
    expected = hmac.new(
        key,
        receipt_message(int(expires), current_vault, current_bundle),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(supplied_signature, expected):
        sys.exit("invalid live Stop Slop receipt")
    errors = lint_posts(bundle["posts"])
    if errors:
        sys.exit("Stop Slop hard-check failed:\n- " + "\n- ".join(errors))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="stopslop-gate",
        description="Load the team's Stop Slop rules and gate a Typefully content bundle.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    context = sub.add_parser("context", help="print all live canonical source files")
    context.add_argument("--manifest", action="store_true", help="print paths and hashes without content")
    review = sub.add_parser("review", help="lint and sign an exact reviewed content bundle")
    review.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)

    sources = load_sources()
    if args.command == "context":
        payload = source_manifest(sources) if args.manifest else sources
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    bundle = load_bundle(args.bundle)
    receipt = issue_receipt(bundle, sources=sources)
    print(
        json.dumps(
            {
                "passed": True,
                "bundle_sha256": digest(bundle),
                "rules_sha256": source_digest(sources),
                "source_manifest": source_manifest(sources),
                "receipt": receipt,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
