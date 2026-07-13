# Smart scheduling policy

This is the deterministic fallback layer for `typefully smart-plan`. It exists
so a recommended time has inspectable reasons instead of being the first empty
Typefully slot.

## Evidence order

Use scheduling evidence in this order:

1. A fixed public event, launch, embargo, or time-sensitive discussion.
2. The live Typefully queue and nearby topic inventory.
3. the account owner's playbook notes.
4. Typefully X analytics from the last 120 days, shrunk toward the account mean
   when a comparable weekday/hour sample is small.
5. The platform fallback windows below.

Typefully's API currently provides normalized post analytics only for X. Do not
present the other platform windows as account-derived performance.

## Platform fallback windows

All times use Europe/Zurich with the date's actual UTC offset.

| Platform | Flagship order | Reader logic |
| --- | --- | --- |
| X | 14:30, 19:30, 10:00 | Account playbook and audience handoff |
| LinkedIn | 09:10, 11:45, 15:20 | Professional workday attention, Europe first |
| Threads | 18:20, 12:20, 09:40 | Conversational browsing and evening overlap |
| Bluesky | 19:10, 17:40, 12:10 | Technical/global discussion and US handoff |

The non-round minutes reduce simultaneous cross-posting and make each platform
release an intentional event.

## Scoring inputs

- content freshness: breaking, timely, or evergreen;
- content kind: flagship, quote, warm-up, or update;
- weekday preference by platform;
- delay cost, strongest for breaking content;
- same-platform collision within three hours, which blocks a slot;
- another same-platform post on the same day;
- overlapping topic language within ten days;
- less than 45 minutes between campaign platforms, which blocks a slot;
- X weekday/hour performance when at least eight account posts exist.

The planner returns two alternatives for every platform. An agent may override
the top result only with a concrete reason and must show that reason to the owner.

## Approval boundary

`smart-plan` is read-only. Its output contains four preflight commands. Run
those commands without confirmation to obtain the exact Typefully plans and
tokens. Scheduling happens only after the owner approves all four current plans in
the same conversation.
