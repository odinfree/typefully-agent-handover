"""Read-only, platform-aware campaign scheduling for Typefully drafts."""

import datetime
import math
import re
import statistics
import sys
from zoneinfo import ZoneInfo


TIMEZONE = "Europe/Zurich"
LOCAL_TZ = ZoneInfo(TIMEZONE)
PLATFORMS = ("x", "linkedin", "threads", "bluesky")
PLATFORM_ORDER = ("x", "linkedin", "threads", "bluesky")
KINDS = ("flagship", "quote", "warmup", "update")
FRESHNESS = ("breaking", "timely", "evergreen")

# Monday is 0. Values are deliberate fallbacks, not claims of universal best times.
DAY_SCORES = {
    "x": (0.3, 0.35, 1.0, 0.7, 0.9, 0.4, 0.5),
    "linkedin": (0.55, 1.0, 0.95, 0.9, 0.45, -0.5, -0.35),
    "threads": (0.35, 0.55, 0.85, 1.0, 0.9, 0.65, 0.7),
    "bluesky": (0.3, 0.55, 0.9, 0.85, 1.0, 0.7, 0.65),
}

SLOTS = {
    "x": {
        "flagship": ((14, 30), (19, 30), (10, 0)),
        "quote": ((10, 0), (19, 30), (14, 30)),
        "warmup": ((10, 0), (14, 30), (19, 30)),
        "update": ((14, 30), (19, 30), (10, 0)),
    },
    "linkedin": {
        "flagship": ((9, 10), (11, 45), (15, 20)),
        "quote": ((11, 45), (9, 10), (15, 20)),
        "warmup": ((9, 10), (11, 45), (15, 20)),
        "update": ((9, 10), (11, 45), (15, 20)),
    },
    "threads": {
        "flagship": ((18, 20), (12, 20), (9, 40)),
        "quote": ((18, 20), (12, 20), (9, 40)),
        "warmup": ((12, 20), (9, 40), (18, 20)),
        "update": ((12, 20), (18, 20), (9, 40)),
    },
    "bluesky": {
        "flagship": ((19, 10), (17, 40), (12, 10)),
        "quote": ((17, 40), (19, 10), (12, 10)),
        "warmup": ((12, 10), (17, 40), (19, 10)),
        "update": ((17, 40), (19, 10), (12, 10)),
    },
}

DEFAULT_HORIZON = {"breaking": 2, "timely": 7, "evergreen": 21}
DELAY_COST = {"breaking": 1.5, "timely": 0.25, "evergreen": 0.04}
TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
STOPWORDS = {
    "about",
    "after",
    "before",
    "from",
    "into",
    "post",
    "proposed",
    "that",
    "this",
    "with",
}


def parse_draft_specs(values):
    mapping = {}
    for value in values or []:
        if "=" not in value:
            sys.exit("--draft must use PLATFORM=DRAFT_ID")
        platform, raw_id = value.split("=", 1)
        platform = platform.strip().lower()
        if platform not in PLATFORMS:
            sys.exit(f"unknown campaign platform: {platform}")
        if platform in mapping:
            sys.exit(f"duplicate campaign draft for {platform}")
        try:
            mapping[platform] = int(raw_id)
        except ValueError:
            sys.exit(f"draft id for {platform} must be an integer")
    missing = [platform for platform in PLATFORMS if platform not in mapping]
    if missing:
        sys.exit("smart campaigns require all four platforms: " + ", ".join(missing))
    return mapping


def local_datetime(value):
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(LOCAL_TZ)


def enabled_platforms_from_queue_draft(draft):
    return {
        platform
        for platform in PLATFORMS
        if draft.get(f"{platform}_post_enabled") is True
    }


def topic_tokens(text):
    return {token for token in TOKEN_RE.findall((text or "").lower()) if token not in STOPWORDS}


def topic_overlap(topic, draft):
    wanted = topic_tokens(topic)
    if not wanted:
        return 0.0
    existing = topic_tokens(
        f"{draft.get('draft_title') or ''} {draft.get('preview') or ''}"
    )
    return len(wanted & existing) / len(wanted)


def scheduled_events(queue):
    events = []
    for day in queue.get("days", []) if isinstance(queue, dict) else []:
        for item in day.get("items", []):
            draft = item.get("draft")
            if not isinstance(draft, dict) or not item.get("at"):
                continue
            try:
                at = local_datetime(item["at"])
            except ValueError:
                continue
            events.append(
                {
                    "at": at,
                    "draft": draft,
                    "platforms": enabled_platforms_from_queue_draft(draft),
                }
            )
    return events


def analytics_value(row):
    metrics = row.get("metrics") or {}
    impressions = max(int(metrics.get("impressions") or 0), 0)
    engagement = metrics.get("engagement") or {}
    total = max(int(engagement.get("total") or 0), 0)
    rate = total / max(impressions, 1)
    return math.log1p(impressions) + min(rate, 0.25) * 8.0


def x_analytics_adjustment(rows, candidate):
    usable = []
    local = []
    for row in rows or []:
        try:
            at = local_datetime(row["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        value = analytics_value(row)
        usable.append(value)
        hour_gap = min((at.hour - candidate.hour) % 24, (candidate.hour - at.hour) % 24)
        if at.weekday() == candidate.weekday() and hour_gap <= 1:
            local.append(value)
    if len(usable) < 8 or not local:
        return 0.0, len(usable), len(local)
    overall = statistics.fmean(usable)
    spread = statistics.pstdev(usable) or 1.0
    weight = len(local) / (len(local) + 5.0)
    shrunk = weight * statistics.fmean(local) + (1.0 - weight) * overall
    return max(-1.5, min(1.5, (shrunk - overall) / spread)), len(usable), len(local)


def candidates(now, platform, kind, horizon_days):
    result = []
    start = now.astimezone(LOCAL_TZ).date()
    for day_offset in range(horizon_days + 1):
        date = start + datetime.timedelta(days=day_offset)
        for rank, (hour, minute) in enumerate(SLOTS[platform][kind]):
            at = datetime.datetime.combine(
                date, datetime.time(hour, minute), tzinfo=LOCAL_TZ
            )
            if at > now.astimezone(LOCAL_TZ) + datetime.timedelta(hours=2):
                result.append((at, rank))
    return result


def score_candidate(
    candidate,
    slot_rank,
    platform,
    topic,
    freshness,
    now,
    existing,
    selected,
    x_analytics,
):
    score = DAY_SCORES[platform][candidate.weekday()] * 4.0
    score += (3 - slot_rank) * 0.8
    hours_out = (candidate - now.astimezone(LOCAL_TZ)).total_seconds() / 3600
    score -= (hours_out / 24.0) * DELAY_COST[freshness]
    reasons = [
        f"{candidate.strftime('%A')} platform window",
        f"{freshness} delay cost applied",
    ]

    for event in existing:
        gap = abs((candidate - event["at"]).total_seconds()) / 3600
        if platform in event["platforms"]:
            if gap < 3:
                score -= 100
                reasons.append("blocked by same-platform post within 3 hours")
            elif candidate.date() == event["at"].date():
                score -= 4
                reasons.append("same-platform post already scheduled that day")
            overlap = topic_overlap(topic, event["draft"])
            if overlap >= 0.5 and gap < 24 * 10:
                score -= 5 * overlap
                reasons.append("nearby topic overlap penalty")

    for other in selected.values():
        gap = abs((candidate - other["at"]).total_seconds()) / 60
        if gap < 45:
            score -= 100
            reasons.append("blocked by campaign cross-post within 45 minutes")
        elif gap < 180:
            score -= 0.5
            reasons.append("small cross-platform spacing penalty")

    anchor = selected.get("x")
    if platform != "x" and anchor:
        anchor_gap = abs((candidate - anchor["at"]).total_seconds()) / 3600
        max_gap = {"breaking": 24, "timely": 48, "evergreen": 24 * 7}[freshness]
        if anchor_gap > max_gap:
            score -= 100
            reasons.append("blocked by campaign coherence window")
        else:
            coherence_cost = {"breaking": 4.0, "timely": 1.5, "evergreen": 0.4}[
                freshness
            ]
            score -= (anchor_gap / 24.0) * coherence_cost
            reasons.append("campaign coherence cost applied")

    analytics_adjustment = 0.0
    analytics_total = 0
    analytics_local = 0
    if platform == "x":
        analytics_adjustment, analytics_total, analytics_local = x_analytics_adjustment(
            x_analytics, candidate
        )
        score += analytics_adjustment * 2.0
        if analytics_local:
            reasons.append(
                f"X analytics adjustment from {analytics_local} comparable posts"
            )

    return {
        "at": candidate,
        "score": round(score, 3),
        "reasons": reasons,
        "analytics_adjustment": round(analytics_adjustment, 3),
        "analytics_total": analytics_total,
        "analytics_comparable": analytics_local,
    }


def build_schedule(
    now,
    topic,
    kind,
    freshness,
    queue,
    x_analytics=None,
    horizon_days=None,
):
    if kind not in KINDS:
        sys.exit(f"unknown content kind: {kind}")
    if freshness not in FRESHNESS:
        sys.exit(f"unknown freshness: {freshness}")
    now = local_datetime(now)
    horizon_days = horizon_days or DEFAULT_HORIZON[freshness]
    existing = scheduled_events(queue)
    selected = {}
    for platform in PLATFORM_ORDER:
        ranked = [
            score_candidate(
                at,
                rank,
                platform,
                topic,
                freshness,
                now,
                existing,
                selected,
                x_analytics or [],
            )
            for at, rank in candidates(now, platform, kind, horizon_days)
        ]
        ranked.sort(key=lambda item: (item["score"], -item["at"].timestamp()), reverse=True)
        viable = [item for item in ranked if item["score"] > -50]
        if not viable:
            sys.exit(f"no collision-free smart slot found for {platform}")
        chosen = viable[0]
        selected[platform] = chosen
        selected[platform]["alternatives"] = viable[1:3]
    return selected


def serialize_choice(item):
    return {
        "publish_at": item["at"].isoformat(),
        "publish_at_utc": item["at"].astimezone(datetime.timezone.utc).isoformat(),
        "score": item["score"],
        "reasons": item["reasons"],
        "analytics_adjustment": item["analytics_adjustment"],
        "analytics_total": item["analytics_total"],
        "analytics_comparable": item["analytics_comparable"],
    }


def serialize_schedule(schedule, draft_ids, topic, kind, freshness):
    recommendations = {}
    for platform in PLATFORMS:
        choice = schedule[platform]
        recommendations[platform] = {
            "draft_id": draft_ids[platform],
            **serialize_choice(choice),
            "alternatives": [serialize_choice(item) for item in choice["alternatives"]],
            "preflight_command": (
                f"typefully publish {draft_ids[platform]} --at {choice['at'].isoformat()}"
            ),
        }
    return {
        "schema": "typefully-smart-schedule-v1",
        "executed": False,
        "public_action_executed": False,
        "timezone": TIMEZONE,
        "topic": topic,
        "kind": kind,
        "freshness": freshness,
        "coverage": list(PLATFORMS),
        "recommendations": recommendations,
        "evidence": {
            "x": "Typefully account analytics plus live queue and account playbook",
            "linkedin": "platform fallback window plus live queue and campaign spacing",
            "threads": "platform fallback window plus live queue and campaign spacing",
            "bluesky": "platform fallback window plus live queue and campaign spacing",
            "analytics_limitation": "Typefully API currently exposes normalized post analytics only for X",
        },
    }
