# typefully-agent-handover

Run your social publishing (X, LinkedIn, Threads, Bluesky) from the terminal
with AI agents that cannot embarrass you. This repo is the complete handover:
a governed Typefully CLI, a copy-quality gate, a campaign scheduler, and the
operating rules that make agent-written posts safe to ship.

Built and battle-tested on a live account. Everything here is generic — you
plug in your own Typefully account and your own rules.

## What you get

```
scripts/typefully                  governed CLI for the Typefully v2 API
scripts/typefully_lib.py           the adapter (stdlib only, no dependencies)
scripts/typefully_smart_schedule.py  read-only campaign timing model
scripts/stopslop-gate              copy-quality gate with signed receipts
skills/thread-to-typefully/        agent skill: current conversation -> campaign
tests/                             75 offline tests covering the safety contract
AGENTS.example.md                  the governance policy, ready to adapt
lane.example.json                  your account config, ready to fill in
```

## The core idea

Agents draft. Humans publish. The CLI makes that mechanical:

1. Any draft an agent creates must carry a **receipt** from the Stop Slop gate,
   which binds the exact copy to the exact current rules. Change either and
   the receipt dies.
2. Any public transition (publish, schedule, delete) starts as a **read-only
   preflight** that prints the full plan and a ten-minute confirmation token,
   then exits. Only re-running the printed command with that token executes.
   Change the copy, the target accounts, the schedule, or the queue and the
   token dies.
3. Every write is **hard-locked to one social set** — your writable one —
   at a single chokepoint before any network call. Wrong set, no request.

The token proves the world did not change between preview and execution. It
does not prove a human approved — that stays a team rule (see
AGENTS.example.md), because agents run with your API key. Treat the gates as
seatbelts, not substitutes for judgment.

## Setup (15 minutes)

1. **Typefully API key**: Typefully → Settings → API. Save it:

   ```zsh
   mkdir -p ~/.config/typefully
   read -s "key?Paste Typefully API key: "
   printf '%s' "$key" > ~/.config/typefully/api_key
   chmod 600 ~/.config/typefully/api_key
   unset key; echo; echo saved
   ```

2. **Lane config**: copy `lane.example.json` to `~/.config/typefully/lane.json`
   and fill in your writable social set id plus the exact handles connected to
   it. Find the id with `./scripts/typefully sets`. The CLI refuses every
   write to any other set, and refuses to publish if a connected handle
   changed under you.

3. **Stop Slop rules**: clone the public rules the gate reads:

   ```sh
   git clone https://github.com/welttowelt/stop-slop-refined ~/stop-slop-refined
   ```

   Readable version: <https://welttowelt.github.io/stop-slop-refined/>.
   Point `STOPSLOP_RULES_DIR` elsewhere if you keep your own fork — teams
   should fork and make the rules their own.

4. **Verify**:

   ```sh
   ./scripts/typefully me
   python3 -m unittest discover -s tests
   ```

5. **Install the skill** for Claude Code and/or Codex:

   ```sh
   mkdir -p ~/.claude/skills ~/.codex/skills
   ln -s "$PWD/skills/thread-to-typefully" ~/.claude/skills/thread-to-typefully
   ln -s "$PWD/skills/thread-to-typefully" ~/.codex/skills/thread-to-typefully
   ```

## Daily use

```sh
typefully drafts                       # what exists
typefully drafts --status scheduled    # what will fire
typefully get <id>                     # one draft, full JSON
typefully queue                        # the next two weeks

# agent drafting (receipt required, creates a PRIVATE draft only):
scripts/stopslop-gate context
scripts/stopslop-gate review --bundle /abs/path/bundle.json
typefully post --bundle /abs/path/bundle.json --stopslop-receipt 'oss1...'

# public transitions (preflight first, token second):
typefully publish <id> --slot          # prints plan + token, exits 2
typefully publish <id> --slot --confirm 'tf1...'
typefully fire <id>                    # publish now (same two-step)
typefully delete <id>                  # same two-step

# campaign timing (read-only, one draft per platform):
typefully smart-plan --draft x=1 --draft linkedin=2 \
  --draft threads=3 --draft bluesky=4 \
  --topic "your topic" --kind flagship --freshness timely
```

Exit codes: `0` done and verified, `1` failed or unverified, `2` nothing
public happened — confirmation missing, stale, or changed.

## Operating rules

`AGENTS.example.md` is the policy we actually run, genericized. Rename it to
`AGENTS.md` in your fork and adapt. The rules that earn their keep:

- One writable social set. Everything else read-only.
- Agent drafts are titled `PROPOSED: [agent-name] <slug>` at creation, so
  provenance never needs archaeology.
- Sign-off means the owner saw the exact final text in the current
  conversation. A delegated "write something and post it" is not sign-off.
- Platform-native copy. One idea, four native expressions. Never one text
  pasted four times. Links stay on their own platform or the open web.
- Keep a never-amplify list (your competitors) and check it before any quote
  post or repost.
- Never discuss your posting setup in public copy. The kitchen stays private.
- After an ambiguous write failure, inspect before retrying. The server may
  have accepted the request.

## Multi-agent note

Two agents (for example Claude Code and Codex) can share this lane. Give the
repo a git-backed mailbox file for handoffs (append-only, unique message ids,
pull before read, push after write) and session-scoped ownership tags. The
patterns are simple and the tests encode the parts that must not regress.

## License

MIT. Built with Claude Code and Codex. No affiliation with Typefully — this
uses their public API: <https://typefully.com/docs/api>.
