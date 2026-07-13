"""Governed Typefully v2 CLI implementation.

The API adapter is intentionally small. Public, scheduled, and destructive
transitions are fail-closed behind a short-lived confirmation fingerprint that
binds the exact draft, connected accounts, targets, and requested action.
"""

import argparse
import contextlib
import datetime
import hashlib
import hmac
import http.client
import json
import os
import pathlib
import re
import shlex
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import stopslop_gate
import typefully_smart_schedule

try:
    import fcntl
except ImportError:  # pragma: no cover - the governed lane runs on Unix
    fcntl = None


BASE = "https://api.typefully.com/v2"
ALL_PLATFORMS = ("x", "linkedin", "threads", "bluesky")


def _lane_config():
    raw = os.environ.get("TYPEFULLY_LANE_FILE")
    if raw:
        path = pathlib.Path(raw).expanduser()
    else:
        root = os.environ.get("XDG_CONFIG_HOME")
        base = pathlib.Path(root).expanduser() if root else pathlib.Path.home() / ".config"
        path = base / "typefully" / "lane.json"
    if not path.exists():
        sys.exit(
            f"no lane config at {path} (or set TYPEFULLY_LANE_FILE). "
            "Copy lane.example.json there and fill in your writable social "
            "set id and the exact account handles it must match."
        )
    try:
        cfg = json.loads(path.read_text())
        write_set = int(cfg["write_set"])
        targets = {str(k): str(v) for k, v in cfg["expected_targets"].items()}
    except (KeyError, TypeError, ValueError) as error:
        sys.exit(f"invalid lane config {path}: {error}")
    unknown = sorted(set(targets) - set(ALL_PLATFORMS))
    if unknown:
        sys.exit(f"invalid lane config {path}: unknown platforms {unknown}")
    return write_set, targets


WRITE_SET, EXPECTED_TARGETS = _lane_config()
READ_RETRIES = 2
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
CONFIRMATION_TTL_SECONDS = 10 * 60
TOKEN_RE = re.compile(r"^tf1\.(\d+)\.([0-9a-f]{32})$")


def env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer, got {raw!r}")


def env_float(name, default, minimum):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        sys.exit(f"{name} must be a number, got {raw!r}")
    if value < minimum:
        sys.exit(f"{name} must be at least {minimum}")
    return value


DEFAULT_SET = env_int("TYPEFULLY_SET", WRITE_SET)
REQUEST_TIMEOUT = env_float("TYPEFULLY_TIMEOUT", 15.0, 1.0)
PUBLISH_TIMEOUT = env_float("TYPEFULLY_PUBLISH_TIMEOUT", 120.0, 10.0)
MEDIA_TIMEOUT = env_float("TYPEFULLY_MEDIA_TIMEOUT", 90.0, 5.0)
MEDIA_WRITE_ATTEMPTS = 3


def config_dir():
    root = os.environ.get("XDG_CONFIG_HOME")
    return pathlib.Path(root).expanduser() if root else pathlib.Path.home() / ".config"


def api_key():
    key = os.environ.get("TYPEFULLY_API_KEY", "").strip()
    if key:
        return key

    path = config_dir() / "typefully" / "api_key"
    if path.exists():
        if not path.is_file():
            sys.exit(f"api key path is not a regular file: {path}")
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                sys.exit(f"api key file permissions are {mode:o}; run: chmod 600 {path}")
        key = path.read_text().strip()

    if not key:
        sys.exit("no api key: set TYPEFULLY_API_KEY or write ~/.config/typefully/api_key")
    return key


def out(obj, stream=None):
    stream = stream or sys.stdout
    json.dump(obj, stream, indent=2, ensure_ascii=False)
    print(file=stream)


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def ensure_write_set(set_id):
    if int(set_id) != WRITE_SET:
        sys.exit(
            f"refusing write to social set {set_id}: this lane only permits writes "
            f"to {WRITE_SET}; other sets are read-only"
        )


def enforce_write_path(method, path):
    if method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return
    if ".." in path or "//" in path:
        sys.exit(f"refusing non-normalized {method} path: {path}")
    match = re.match(r"^/social-sets/(\d+)(?:/|$)", path)
    if not match:
        sys.exit(f"refusing unscoped {method} request: {path}")
    if match.group(1) != str(int(match.group(1))):
        sys.exit(f"refusing non-normalized {method} path: {path}")
    ensure_write_set(int(match.group(1)))


def retry_delay(headers, attempt):
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 5.0)
        except ValueError:
            pass

    reset = headers.get("X-RateLimit-User-Reset") if headers else None
    if reset:
        try:
            return min(max(float(reset) - time.time(), 0.0), 5.0)
        except ValueError:
            pass
    return min(2 ** attempt, 5.0)


def http_error_message(error, method, path):
    body = error.read().decode(errors="replace")[:800]
    detail = f"HTTP {error.code} {method} {path}"
    if error.code == 429:
        reset = (
            error.headers.get("X-RateLimit-User-Reset")
            or error.headers.get("X-RateLimit-SocialSet-Reset")
            or error.headers.get("Retry-After")
        )
        if reset:
            detail += f" (rate limit reset/retry value: {reset})"
    return detail + (f"\n{body}" if body else "")


def call(method, path, body=None, params=None, timeout=None):
    method = method.upper()
    enforce_write_path(method, path)
    url = BASE + path
    if params:
        clean = {key: value for key, value in params.items() if value is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    attempts = 1 + (READ_RETRIES if method == "GET" else 0)
    request_timeout = timeout if timeout is not None else REQUEST_TIMEOUT
    headers = {"Authorization": "Bearer " + api_key()}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = canonical_json(body).encode("utf-8")

    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            method=method,
            headers=headers,
            data=data,
        )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = response.read()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                if method == "GET" and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 5.0))
                    continue
                suffix = (
                    " Outcome is unknown; inspect Typefully before retrying."
                    if method != "GET"
                    else ""
                )
                sys.exit(f"invalid JSON from {method} {path}: {error}.{suffix}")
        except urllib.error.HTTPError as error:
            if (
                method == "GET"
                and error.code in RETRYABLE_HTTP
                and attempt + 1 < attempts
            ):
                time.sleep(retry_delay(error.headers, attempt))
                continue
            message = http_error_message(error, method, path)
            if method != "GET" and error.code in {500, 502, 503, 504}:
                message += (
                    "\nOutcome is unknown; inspect Typefully before retrying."
                )
            sys.exit(message)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
            if method == "GET" and attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 5.0))
                continue
            reason = getattr(error, "reason", error)
            suffix = (
                " Outcome is unknown; inspect Typefully before retrying."
                if method != "GET"
                else ""
            )
            sys.exit(f"transport error during {method} {path}: {reason}.{suffix}")

    sys.exit(f"request failed after {attempts} attempts: {method} {path}")


def put_media_bytes(upload_url, path):
    data = pathlib.Path(path).read_bytes()
    parsed = urllib.parse.urlsplit(upload_url)
    if parsed.scheme != "https" or not parsed.hostname:
        sys.exit("Typefully media upload URL must be absolute HTTPS")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port or 443,
        timeout=REQUEST_TIMEOUT,
    )
    try:
        connection.putrequest("PUT", target, skip_accept_encoding=True)
        connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        status = response.status
        body = response.read().decode(errors="replace")[:800]
    except (http.client.HTTPException, socket.timeout, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        sys.exit(
            "transport error during PUT presigned media upload "
            f"to {parsed.hostname}: {reason}."
        )
    finally:
        connection.close()
    if status not in {200, 204}:
        sys.exit(
            f"HTTP {status} PUT presigned media upload"
            + (f"\n{body}" if body else "")
        )


def wait_for_media(set_id, media_id):
    deadline = time.monotonic() + MEDIA_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        last = call("GET", f"/social-sets/{set_id}/media/{media_id}")
        status = (last or {}).get("status")
        if status == "ready":
            return last
        if status in {"failed", "error"}:
            reason = (last or {}).get("error_reason") or "unknown processing error"
            sys.exit(f"Typefully media {media_id} failed processing: {reason}")
        time.sleep(0.5)
    status = (last or {}).get("status") or "unknown"
    sys.exit(f"Typefully media {media_id} did not become ready; last status: {status}")


def retry_media_write(perform):
    """Retry media staging only. A repeated URL request can leave an unused
    media object, while a repeated PUT replaces the same private object bytes."""
    for attempt in range(MEDIA_WRITE_ATTEMPTS):
        try:
            return perform()
        except SystemExit as error:
            message = str(error)
            retryable = message.startswith(("transport error", "HTTP 5"))
            if not retryable or attempt + 1 >= MEDIA_WRITE_ATTEMPTS:
                raise
            time.sleep(0.5 * (attempt + 1))
    sys.exit("media write failed after bounded retries")


def upload_bundle_media(set_id, media):
    ensure_write_set(set_id)
    media_ids = []
    for item in media or []:
        path = pathlib.Path(item["path"])
        created = guarded_write(
            "media-upload-url",
            None,
            lambda path=path: retry_media_write(
                lambda: call(
                    "POST",
                    f"/social-sets/{set_id}/media/upload",
                    {"file_name": path.name},
                )
            ),
            extra={"file_name": path.name},
        )
        media_id = (created or {}).get("media_id")
        upload_url = (created or {}).get("upload_url")
        if not media_id or not upload_url:
            sys.exit("Typefully media upload response omitted media_id or upload_url")
        guarded_write(
            "media-upload-bytes",
            media_id,
            lambda upload_url=upload_url, path=path: retry_media_write(
                lambda: put_media_bytes(upload_url, path)
            ),
            extra={"file_name": path.name},
        )
        wait_for_media(set_id, media_id)
        media_ids.append(media_id)
    return media_ids


AMBIGUOUS_WRITE_PREFIXES = ("HTTP 5", "transport error", "invalid JSON from", "request failed")


def guarded_write(action, draft_id, perform, extra=None):
    """Run a write call; if the write itself fails, keep the one-JSON-document
    contract by emitting a structured receipt before exiting 1. Failures that
    happen before any request (refusals, key errors) re-raise unchanged."""
    try:
        return perform()
    except SystemExit as error:
        message = str(error)
        rejected = message.startswith("HTTP 4")
        ambiguous = message.startswith(AMBIGUOUS_WRITE_PREFIXES)
        if not rejected and not ambiguous:
            raise
        receipt = {
            "executed": False if rejected else None,
            "action": action,
            "success": False,
            "outcome": "rejected" if rejected else "unknown",
            "id": draft_id,
            "error": message,
        }
        if extra:
            receipt.update(extra)
        out(receipt)
        raise SystemExit(1)


def collect_pages(path, params=None, max_results=None, offset=0):
    if max_results is not None and max_results < 1:
        sys.exit("--limit must be at least 1")
    if offset < 0:
        sys.exit("--offset must be zero or greater")

    results = []
    total = None
    current_offset = offset
    base_params = dict(params or {})

    while True:
        remaining = None if max_results is None else max_results - len(results)
        if remaining is not None and remaining <= 0:
            break
        page_size = 50 if remaining is None else min(50, remaining)
        page_params = {
            **base_params,
            "limit": page_size,
            "offset": current_offset,
        }
        page = call("GET", path, params=page_params)
        if not isinstance(page, dict):
            sys.exit(f"invalid paginated response from GET {path}: expected object")
        rows = page.get("results", [])
        if not isinstance(rows, list):
            sys.exit(f"invalid paginated response from GET {path}: results is not a list")
        if total is None:
            total = page.get("count", len(rows))
        results.extend(rows)
        current_offset += len(rows)

        if not rows:
            break
        if max_results is not None and len(results) >= max_results:
            break
        if total is not None and current_offset >= total:
            break
        if not page.get("next") and len(rows) < page_size:
            break

    total = len(results) if total is None else total
    complete = current_offset >= total
    return {
        "results": results,
        "count": total,
        "returned": len(results),
        "offset": offset,
        "complete": complete,
        "next_offset": None if complete else current_offset,
    }


def enabled_platforms(draft):
    platforms = draft.get("platforms") or {}
    enabled = []
    for platform, config in platforms.items():
        if isinstance(config, dict) and config.get("enabled", True):
            enabled.append(platform)
    return enabled


def target_identities(social_set, platforms):
    connected = social_set.get("platforms") or {}
    identities = {}
    for platform in platforms:
        if platform not in EXPECTED_TARGETS:
            sys.exit(f"unsupported writable platform in this lane: {platform}")
        account = connected.get(platform)
        if not isinstance(account, dict):
            sys.exit(f"platform {platform} is not connected on social set {WRITE_SET}")
        username = account.get("username")
        expected = EXPECTED_TARGETS[platform]
        if username != expected:
            sys.exit(
                f"refusing write: {platform} target changed from {expected!r} "
                f"to {username!r}; update and review the lane policy first"
            )
        identities[platform] = {
            "username": username,
            "profile_url": account.get("profile_url"),
        }
    return identities


def published_urls(draft, platforms):
    return {
        platform: draft.get(platform + "_published_url")
        for platform in platforms
        if draft.get(platform + "_published_url")
    }


def draft_snapshot(draft, include_publication=False):
    platforms = enabled_platforms(draft)
    snapshot = {
        "id": draft.get("id"),
        "status": draft.get("status"),
        "publish_state": draft.get("publish_state"),
        "updated_at": draft.get("updated_at"),
        "scheduled_date": draft.get("scheduled_date"),
        "draft_title": draft.get("draft_title"),
        "platforms": draft.get("platforms") or {},
    }
    if include_publication:
        snapshot["published_at"] = draft.get("published_at")
        snapshot["published_urls"] = published_urls(draft, platforms)
    return snapshot


def ensure_actionable(draft, action):
    state = draft.get("publish_state")
    status = draft.get("status")
    if state == "in_progress" or status == "publishing":
        sys.exit(f"refusing {action}: draft {draft.get('id')} is currently publishing")
    if action != "delete" and status == "published":
        sys.exit(f"refusing {action}: draft {draft.get('id')} is already published")
    if action != "delete" and status == "error":
        sys.exit(
            f"refusing {action}: draft {draft.get('id')} has status=error; "
            "inspect platform URLs before deciding how to recover"
        )


def current_quota(social_set):
    quota = social_set.get("publishing_quota")
    if isinstance(quota, dict):
        remaining = quota.get("remaining")
        if isinstance(remaining, int) and remaining <= 0:
            sys.exit("publishing quota is exhausted; no public action was run")
    return quota


def parse_at(value):
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        sys.exit("--at must be an ISO 8601 datetime with timezone")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        sys.exit("--at must include a timezone")
    if parsed.astimezone(datetime.timezone.utc) <= datetime.datetime.now(
        datetime.timezone.utc
    ):
        sys.exit("--at must be in the future")
    return value


def next_free_slot(set_id):
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=1)).date()
    end = start + datetime.timedelta(days=61)
    queue = call(
        "GET",
        f"/social-sets/{set_id}/queue",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    candidates = []
    for day in queue.get("days", []) if isinstance(queue, dict) else []:
        for item in day.get("items", []):
            if item.get("kind") != "queue_slot" or item.get("draft") is not None:
                continue
            value = item.get("at")
            if not value:
                continue
            try:
                parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.astimezone(datetime.timezone.utc) > now:
                candidates.append((parsed, value))
    if not candidates:
        sys.exit("no free queue slot found in the next 61 days")
    return min(candidates, key=lambda item: item[0])[1]


def action_mode(args):
    if getattr(args, "now", False):
        return "publish-now", "now"
    if getattr(args, "slot", False):
        return "schedule-slot", next_free_slot(args.set)
    if getattr(args, "at", None):
        return "schedule-at", parse_at(args.at)
    return None, None


def build_transition_plan(set_id, draft, action, publish_at=None):
    ensure_write_set(set_id)
    ensure_actionable(draft, "delete" if action == "delete" else action)
    if action == "delete":
        return {
            "schema": "typefully-confirm-v1",
            "action": action,
            "social_set_id": set_id,
            "draft": draft_snapshot(draft, include_publication=True),
        }

    social_set = call("GET", f"/social-sets/{set_id}/")
    platforms = enabled_platforms(draft)
    if not platforms:
        sys.exit(f"draft {draft.get('id')} has no enabled platforms")
    targets = target_identities(social_set, platforms)
    plan = {
        "schema": "typefully-confirm-v1",
        "action": action,
        "social_set_id": set_id,
        "targets": targets,
        "draft": draft_snapshot(draft, include_publication=(action == "delete")),
    }
    if publish_at is not None:
        plan["publish_at"] = publish_at
        plan["publishing_quota"] = current_quota(social_set)
    return plan


def sign_plan(plan, expires_at):
    envelope = {
        "schema": "typefully-confirm-token-v1",
        "expires_at": int(expires_at),
        "plan": plan,
    }
    digest = hmac.new(
        api_key().encode("utf-8"),
        canonical_json(envelope).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"tf1.{int(expires_at)}.{digest}"


def parse_token(token):
    match = TOKEN_RE.fullmatch(token or "")
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def next_command(plan, token):
    draft_id = str(plan["draft"]["id"])
    action = plan["action"]
    if action == "delete":
        parts = ["typefully", "delete", draft_id, "--confirm", token]
    else:
        parts = ["typefully", "publish", draft_id]
        if action == "publish-now":
            parts.append("--now")
        elif action == "schedule-slot":
            parts.append("--slot")
        else:
            parts.extend(["--at", plan["publish_at"]])
        parts.extend(["--confirm", token])
    return shlex.join(parts)


def confirmation_payload(plan, error=None, extra=None):
    expires_at = int(time.time()) + CONFIRMATION_TTL_SECONDS
    token = sign_plan(plan, expires_at)
    payload = {
        "executed": False,
        "confirmation_required": True,
        "error": error,
        "plan": plan,
        "content_sha256": content_sha256(plan["draft"]["platforms"]),
        "confirm_token": token,
        "expires_at": datetime.datetime.fromtimestamp(
            expires_at, datetime.timezone.utc
        ).isoformat(),
        "next_command": next_command(plan, token),
    }
    if extra:
        payload.update(extra)
    return payload


def require_confirmation(token, plan):
    parsed = parse_token(token)
    if parsed is None:
        out(confirmation_payload(plan, error="missing or malformed confirmation token"))
        raise SystemExit(2)
    expires_at, _ = parsed
    if expires_at < int(time.time()):
        out(confirmation_payload(plan, error="confirmation token expired"))
        raise SystemExit(2)
    expected = sign_plan(plan, expires_at)
    if not hmac.compare_digest(expected, token):
        out(
            confirmation_payload(
                plan,
                error="draft, targets, quota, queue slot, or requested action changed",
            )
        )
        raise SystemExit(2)


def state_dir():
    root = os.environ.get("XDG_STATE_HOME")
    return pathlib.Path(root).expanduser() if root else pathlib.Path.home() / ".local" / "state"


@contextlib.contextmanager
def transition_lock():
    if fcntl is None:
        sys.exit("public transitions require Unix file-lock support")
    directory = state_dir() / "typefully-agent-lane"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lock_path = directory / "transition.lock"
    with lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another Typefully transition is already in progress")
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def poll_publish(set_id, draft_id, expected_platforms):
    deadline = time.monotonic() + PUBLISH_TIMEOUT
    last = None
    last_error = None
    while time.monotonic() < deadline:
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            draft = call(
                "GET",
                f"/social-sets/{set_id}/drafts/{draft_id}",
                timeout=min(REQUEST_TIMEOUT, remaining),
            )
        except SystemExit as error:
            last_error = str(error)
            print(
                f"status poll failed ({last_error}); retrying until deadline",
                file=sys.stderr,
            )
            sleep_for = min(3.0, max(deadline - time.monotonic(), 0.0))
            if sleep_for:
                time.sleep(sleep_for)
            continue
        last = draft
        state = draft.get("publish_state")
        status = draft.get("status")
        print(f"publish_state={state} status={status}", file=sys.stderr)
        if state == "finished" or status == "error":
            urls = published_urls(draft, expected_platforms)
            missing = [
                platform for platform in expected_platforms if platform not in urls
            ]
            success = status == "published" and not missing
            return {
                "executed": True,
                "action": "publish-now",
                "success": success,
                "id": draft.get("id"),
                "status": status,
                "publish_state": state,
                "published_at": draft.get("published_at"),
                "expected_platforms": list(expected_platforms),
                "urls": urls,
                "missing_platforms": missing,
                "error": None if success else "publication failed or was incomplete",
            }
        sleep_for = min(3.0, max(deadline - time.monotonic(), 0.0))
        if sleep_for:
            time.sleep(sleep_for)

    return {
        "executed": True,
        "action": "publish-now",
        "success": False,
        "id": draft_id,
        "status": last.get("status") if last else None,
        "publish_state": last.get("publish_state") if last else None,
        "expected_platforms": list(expected_platforms),
        "urls": published_urls(last or {}, expected_platforms),
        "missing_platforms": [
            platform
            for platform in expected_platforms
            if not (last or {}).get(platform + "_published_url")
        ],
        "error": (
            "timed out waiting for publication"
            + (f"; last status poll failed: {last_error}" if last_error else "")
            + "; outcome is unknown, inspect Typefully before retrying"
        ),
    }


def parse_platforms(value, all_platforms=False):
    if all_platforms:
        return list(ALL_PLATFORMS)
    if value is None:
        return list(ALL_PLATFORMS)
    raw = value.split(",")
    if not raw or any(not item.strip() for item in raw):
        sys.exit("--platforms must be a non-empty comma-separated list")
    platforms = [item.strip() for item in raw]
    if len(platforms) != len(set(platforms)):
        sys.exit("--platforms contains a duplicate")
    bad = [platform for platform in platforms if platform not in ALL_PLATFORMS]
    if bad:
        sys.exit(
            f"unknown platform(s): {','.join(bad)} "
            f"(valid: {','.join(ALL_PLATFORMS)})"
        )
    return platforms


def cmd_me(args):
    out(call("GET", "/me"))


def cmd_sets(args):
    out(collect_pages("/social-sets"))


def cmd_set(args):
    out(call("GET", f"/social-sets/{args.set}/"))


def cmd_drafts(args):
    data = collect_pages(
        f"/social-sets/{args.set}/drafts",
        params={"status": args.status},
        max_results=args.limit,
        offset=args.offset,
    )
    if args.full:
        out(data)
        return
    for row in data["results"]:
        print(
            f"{row['id']}  {row['status']:<10} "
            f"{row.get('scheduled_date') or '-':<22} {(row.get('preview') or '')!r}"
        )
    print(f"({data['returned']} shown; {data['count']} total)")


def cmd_get(args):
    out(call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}"))


def cmd_queue(args):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = args.start or (today - datetime.timedelta(days=1)).isoformat()
    end = args.end or (today + datetime.timedelta(days=14)).isoformat()
    out(
        call(
            "GET",
            f"/social-sets/{args.set}/queue",
            params={"start_date": start, "end_date": end},
        )
    )


def collect_x_analytics(set_id, start_date, end_date, max_results=200):
    results = []
    offset = 0
    while len(results) < max_results:
        limit = min(100, max_results - len(results))
        page = call(
            "GET",
            f"/social-sets/{set_id}/analytics/x/posts",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "include_replies": "false",
                "limit": limit,
                "offset": offset,
            },
        )
        rows = page.get("results", []) if isinstance(page, dict) else []
        if not isinstance(rows, list):
            sys.exit("invalid X analytics response: results is not a list")
        results.extend(rows)
        if not rows or not page.get("next"):
            break
        offset += len(rows)
    return results


def cmd_smart_plan(args):
    ensure_write_set(args.set)
    if args.horizon_days is not None and not 1 <= args.horizon_days <= 60:
        sys.exit("--horizon-days must be between 1 and 60")
    draft_ids = typefully_smart_schedule.parse_draft_specs(args.draft)
    for platform, draft_id in draft_ids.items():
        draft = call("GET", f"/social-sets/{args.set}/drafts/{draft_id}")
        enabled = enabled_platforms(draft)
        if enabled != [platform]:
            sys.exit(
                f"smart campaign draft {draft_id} must enable only {platform}; "
                f"found {','.join(enabled) or 'none'}"
            )
        ensure_actionable(draft, "smart schedule")

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = args.horizon_days or typefully_smart_schedule.DEFAULT_HORIZON[
        args.freshness
    ]
    start_date = now.astimezone(typefully_smart_schedule.LOCAL_TZ).date()
    end_date = start_date + datetime.timedelta(days=horizon)
    queue = call(
        "GET",
        f"/social-sets/{args.set}/queue",
        params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
    )
    analytics_start = (start_date - datetime.timedelta(days=120)).isoformat()
    x_analytics = collect_x_analytics(
        args.set, analytics_start, start_date.isoformat()
    )
    schedule = typefully_smart_schedule.build_schedule(
        now=now,
        topic=args.topic,
        kind=args.kind,
        freshness=args.freshness,
        queue=queue,
        x_analytics=x_analytics,
        horizon_days=horizon,
    )
    plan = typefully_smart_schedule.serialize_schedule(
        schedule, draft_ids, args.topic, args.kind, args.freshness
    )
    quota = current_quota(call("GET", f"/social-sets/{args.set}/"))
    if isinstance(quota, dict) and isinstance(quota.get("remaining"), int):
        if quota["remaining"] < len(typefully_smart_schedule.PLATFORMS):
            sys.exit("publishing quota cannot cover all four campaign platforms")
    plan["publishing_quota"] = quota
    out(plan)


def load_reviewed_bundle(args):
    if getattr(args, "text", None):
        sys.exit(
            "plain-text draft creation is disabled: use --bundle with a live "
            "Stop Slop receipt"
        )
    if not getattr(args, "bundle", None):
        sys.exit("--bundle is required for draft creation")
    bundle = stopslop_gate.load_bundle(args.bundle)
    stopslop_gate.verify_receipt(
        getattr(args, "stopslop_receipt", None), bundle
    )
    return bundle


def create_draft_body(args, bundle=None, media_ids=None):
    bundle = bundle or load_reviewed_bundle(args)
    platforms = bundle["platforms"]
    quote_url = bundle.get("quote_post_url")
    posts = []
    for index, text in enumerate(bundle["posts"]):
        post = {"text": text}
        if quote_url:
            post["quote_post_url"] = quote_url
        if index == 0 and media_ids:
            post["media_ids"] = list(media_ids)
        posts.append(post)
    body = {
        "platforms": {
            platform: {
                "enabled": True,
                "posts": posts,
            }
            for platform in platforms
        }
    }
    body["draft_title"] = bundle["title"]
    return body, platforms


def cmd_post(args):
    ensure_write_set(args.set)
    action, publish_at = action_mode(args)
    bundle = load_reviewed_bundle(args)
    media_ids = upload_bundle_media(args.set, bundle.get("media"))
    body, platforms = create_draft_body(args, bundle=bundle, media_ids=media_ids)
    created = guarded_write(
        "draft-create",
        None,
        lambda: call("POST", f"/social-sets/{args.set}/drafts", body),
    )
    draft_id = created["id"]
    if action is None:
        out(
            {
                "executed": True,
                "action": "draft-created",
                "public_action_executed": False,
                "id": draft_id,
                "status": created.get("status"),
                "preview": created.get("preview"),
                "private_url": created.get("private_url"),
                "platforms": platforms,
                "media_count": len(media_ids),
            }
        )
        return

    draft = call("GET", f"/social-sets/{args.set}/drafts/{draft_id}")
    plan = build_transition_plan(args.set, draft, action, publish_at)
    out(
        confirmation_payload(
            plan,
            extra={
                "draft_created": True,
                "draft_id": draft_id,
                "private_url": created.get("private_url"),
                "public_action_executed": False,
            },
        )
    )
    raise SystemExit(2)


def draft_matches_update(draft, body):
    if draft.get("draft_title") != body.get("draft_title"):
        return False
    actual_platforms = draft.get("platforms") or {}
    for platform, expected in (body.get("platforms") or {}).items():
        actual = actual_platforms.get(platform) or {}
        if bool(actual.get("enabled")) != bool(expected.get("enabled")):
            return False
        actual_posts = actual.get("posts") or []
        expected_posts = expected.get("posts") or []
        if len(actual_posts) != len(expected_posts):
            return False
        for actual_post, expected_post in zip(actual_posts, expected_posts):
            if actual_post.get("text") != expected_post.get("text"):
                return False
            for field in ("quote_post_url", "media_ids"):
                if field in expected_post and actual_post.get(field) != expected_post[field]:
                    return False
    return True


def cmd_update(args):
    ensure_write_set(args.set)
    bundle = load_reviewed_bundle(args)
    draft = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
    ensure_actionable(draft, "update")
    if draft.get("status") != "draft" or draft.get("scheduled_date"):
        sys.exit(
            f"refusing update: draft {args.draft_id} must be an unscheduled plain draft"
        )
    if bundle.get("title") != draft.get("draft_title"):
        sys.exit("refusing update: reviewed bundle title must match the existing draft")

    requested_platforms = list(bundle["platforms"])
    current_platforms = enabled_platforms(draft)
    if requested_platforms != current_platforms:
        sys.exit(
            "refusing update: reviewed bundle platforms "
            f"{requested_platforms} do not match draft platforms {current_platforms}"
        )

    media_ids = upload_bundle_media(args.set, bundle.get("media"))
    body, platforms = create_draft_body(args, bundle=bundle, media_ids=media_ids)
    guarded_write(
        "draft-update",
        args.draft_id,
        lambda: call(
            "PATCH",
            f"/social-sets/{args.set}/drafts/{args.draft_id}",
            body,
        ),
        extra={"expected_platforms": platforms},
    )
    verified = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
    success = (
        verified.get("status") == "draft"
        and not verified.get("scheduled_date")
        and draft_matches_update(verified, body)
    )
    out(
        {
            "executed": True,
            "action": "draft-updated",
            "public_action_executed": False,
            "success": success,
            "id": verified.get("id"),
            "status": verified.get("status"),
            "private_url": verified.get("private_url"),
            "platforms": platforms,
            "media_count": len(media_ids),
            "error": None if success else "draft update verification failed",
        }
    )
    if not success:
        raise SystemExit(1)


def same_instant(left, right):
    try:
        left_dt = datetime.datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_dt = datetime.datetime.fromisoformat(right.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    return left_dt.astimezone(datetime.timezone.utc) == right_dt.astimezone(
        datetime.timezone.utc
    )


def cmd_publish(args):
    ensure_write_set(args.set)
    if not args.confirm:
        action, publish_at = action_mode(args)
        draft = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
        plan = build_transition_plan(args.set, draft, action, publish_at)
        require_confirmation(None, plan)

    with transition_lock():
        action, publish_at = action_mode(args)
        if action is None:
            sys.exit("choose exactly one of --now, --slot, or --at")
        draft = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
        plan = build_transition_plan(args.set, draft, action, publish_at)
        require_confirmation(args.confirm, plan)

        latest = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
        if draft_snapshot(latest) != plan["draft"]:
            fresh_plan = build_transition_plan(
                args.set, latest, action, publish_at
            )
            require_confirmation(args.confirm, fresh_plan)
            draft = latest
            plan = fresh_plan

        if action == "schedule-slot":
            latest_slot = next_free_slot(args.set)
            if latest_slot != publish_at:
                fresh_plan = build_transition_plan(
                    args.set, draft, action, latest_slot
                )
                require_confirmation(args.confirm, fresh_plan)
                publish_at = latest_slot
                plan = fresh_plan

        platforms = enabled_platforms(draft)
        guarded_write(
            action,
            args.draft_id,
            lambda: call(
                "PATCH",
                f"/social-sets/{args.set}/drafts/{args.draft_id}",
                {"publish_at": publish_at},
            ),
            extra={"expected_platforms": list(platforms)},
        )

    if action == "publish-now":
        receipt = poll_publish(args.set, args.draft_id, platforms)
    else:
        try:
            verified = call(
                "GET", f"/social-sets/{args.set}/drafts/{args.draft_id}"
            )
        except SystemExit as error:
            receipt = {
                "executed": True,
                "action": action,
                "success": False,
                "id": args.draft_id,
                "status": None,
                "scheduled_date": None,
                "expected_scheduled_date": publish_at,
                "platforms": platforms,
                "error": (
                    f"schedule status check failed: {error}; "
                    "outcome is unknown, inspect Typefully before retrying"
                ),
            }
            out(receipt)
            raise SystemExit(1)
        scheduled_date = verified.get("scheduled_date")
        success = verified.get("status") == "scheduled" and same_instant(
            scheduled_date, publish_at
        )
        receipt = {
            "executed": True,
            "action": action,
            "success": success,
            "id": verified.get("id"),
            "status": verified.get("status"),
            "scheduled_date": scheduled_date,
            "expected_scheduled_date": publish_at,
            "platforms": platforms,
            "error": None if success else "schedule verification failed",
        }
    out(receipt)
    if not receipt["success"]:
        raise SystemExit(1)


def cmd_fire(args):
    args.now = True
    args.slot = False
    args.at = None
    cmd_publish(args)


def cmd_delete(args):
    ensure_write_set(args.set)
    if not args.confirm:
        draft = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
        plan = build_transition_plan(args.set, draft, "delete")
        require_confirmation(None, plan)

    with transition_lock():
        draft = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
        plan = build_transition_plan(args.set, draft, "delete")
        require_confirmation(args.confirm, plan)
        latest = call("GET", f"/social-sets/{args.set}/drafts/{args.draft_id}")
        if draft_snapshot(latest, include_publication=True) != plan["draft"]:
            fresh_plan = build_transition_plan(args.set, latest, "delete")
            require_confirmation(args.confirm, fresh_plan)
            draft = latest
        guarded_write(
            "delete",
            args.draft_id,
            lambda: call(
                "DELETE", f"/social-sets/{args.set}/drafts/{args.draft_id}"
            ),
        )
    out(
        {
            "executed": True,
            "action": "delete",
            "success": True,
            "id": args.draft_id,
            "previous_status": draft.get("status"),
            "preview": draft.get("preview"),
        }
    )


def add_transition_flags(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--now", action="store_true", help="publish immediately")
    group.add_argument("--slot", action="store_true", help="schedule exact next free slot")
    group.add_argument("--at", help="schedule at ISO 8601 time with timezone")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="typefully",
        description=(
            "Governed Typefully v2 CLI. Public, scheduled, and destructive actions "
            "require a content-bound confirmation token."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, function, **kwargs):
        command = sub.add_parser(name, **kwargs)
        command.set_defaults(function=function)
        return command

    add("me", cmd_me, help="who am I (verifies the key)")
    add("sets", cmd_sets, help="list all social sets")

    command = add("set", cmd_set, help="social set details and current quota")
    command.add_argument("--set", type=int, default=DEFAULT_SET)

    command = add("drafts", cmd_drafts, help="list drafts (all pages by default)")
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    command.add_argument(
        "--status",
        help="filter: draft|scheduled|published|error|publishing",
    )
    command.add_argument("--limit", type=int, help="maximum results (default all)")
    command.add_argument("--offset", type=int, default=0)
    command.add_argument("--full", action="store_true", help="aggregated JSON")

    command = add("get", cmd_get, help="get one draft (full JSON)")
    command.add_argument("draft_id", type=int)
    command.add_argument("--set", type=int, default=DEFAULT_SET)

    command = add("queue", cmd_queue, help="upcoming queue")
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    command.add_argument("--start", help="start date YYYY-MM-DD")
    command.add_argument("--end", help="end date YYYY-MM-DD")

    command = add(
        "smart-plan",
        cmd_smart_plan,
        help="rank platform-specific campaign slots without scheduling",
    )
    command.add_argument(
        "--draft",
        action="append",
        required=True,
        help="repeat four times as PLATFORM=DRAFT_ID",
    )
    command.add_argument("--topic", required=True, help="short deduplication topic label")
    command.add_argument(
        "--kind",
        choices=typefully_smart_schedule.KINDS,
        default="flagship",
    )
    command.add_argument(
        "--freshness",
        choices=typefully_smart_schedule.FRESHNESS,
        default="timely",
    )
    command.add_argument("--horizon-days", type=int)
    command.add_argument("--set", type=int, default=DEFAULT_SET)

    command = add("post", cmd_post, help="create a Stop Slop-gated plain draft")
    command.add_argument(
        "text",
        nargs="*",
        help="disabled compatibility input; use --bundle",
    )
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    command.add_argument("--bundle", help="reviewed thread-to-typefully JSON bundle")
    command.add_argument(
        "--stopslop-receipt",
        help="short-lived receipt from stopslop-gate review",
    )
    public_group = command.add_mutually_exclusive_group()
    public_group.add_argument(
        "--now",
        action="store_true",
        help="stage draft and return publish confirmation; does not publish",
    )
    public_group.add_argument(
        "--slot",
        action="store_true",
        help="stage draft and return next-slot confirmation; does not schedule",
    )
    public_group.add_argument(
        "--at",
        help="stage draft and return timed confirmation; does not schedule",
    )

    command = add(
        "update",
        cmd_update,
        help="update an unscheduled plain draft with a reviewed bundle",
    )
    command.add_argument("draft_id", type=int)
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    command.add_argument("--bundle", help="reviewed thread-to-typefully JSON bundle")
    command.add_argument(
        "--stopslop-receipt",
        help="short-lived receipt from stopslop-gate review",
    )

    command = add("publish", cmd_publish, help="preflight or execute a draft transition")
    command.add_argument("draft_id", type=int)
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    add_transition_flags(command)
    command.add_argument("--confirm", help="short-lived token from the preflight")

    command = add("fire", cmd_fire, help="alias for publish DRAFT_ID --now")
    command.add_argument("draft_id", type=int)
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    command.add_argument("--confirm", help="short-lived token from the preflight")

    command = add("delete", cmd_delete, help="preflight or delete an exact draft")
    command.add_argument("draft_id", type=int)
    command.add_argument("--set", type=int, default=DEFAULT_SET)
    command.add_argument("--confirm", help="short-lived token from the preflight")

    args = parser.parse_args(argv)
    args.function(args)


if __name__ == "__main__":
    main()
