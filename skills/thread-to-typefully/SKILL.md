---
name: thread-to-typefully
description: Turn the current agent conversation into a coordinated X, LinkedIn, Threads, and Bluesky campaign — platform-native copy, Stop Slop receipts, and read-only smart scheduling in a governed Typefully pipeline. Trigger on "tweet this", "turn this thread into content", "content desk", or "schedule posts from this".
license: MIT
---

# Thread to Typefully

Turn the current working conversation into a four-platform campaign of
private, reviewed Typefully drafts, then rank publishing times. Nothing in
this skill publishes. Public transitions always go through the CLI's
preflight-token flow with the owner's approval.

## Sources this skill must load first

1. The Stop Slop rules: run `scripts/stopslop-gate context` (reads the local
   clone of <https://github.com/welttowelt/stop-slop-refined>, or your fork
   via `STOPSLOP_RULES_DIR`). The rendered rules live at
   <https://welttowelt.github.io/stop-slop-refined/>.
2. The owner's voice and account playbook notes, wherever your team keeps
   them.
3. `references/scout-roster.md` for quote-post scouting targets.
4. `references/scheduling-policy.md` for timing doctrine.

If the rules are missing or unreadable, stop before writing any copy.

## Workflow

1. Establish the public boundary. The source conversation is private by
   default. Only public facts and the owner's public-safe judgment go into
   copy.
2. Check the live inventory: `typefully drafts` and `typefully queue`. Do not
   duplicate topics already staged or scheduled within ten days.
3. Pick the claim. One idea per campaign, stated in one sentence before any
   drafting.
4. Scout X for a genuine quote-post fit, starting from the roster.
   - A quote post needs a direct, current X status URL and a distinct claim
     the owner adds. A topical match alone is insufficient.
   - Competitor screen before selecting any quote target: never amplify a
     competitor. Keep the roster's never-amplify section current. When
     unsure, ask the owner.
   - No strong public post means a standalone original. Never force it.
5. Draft one native version per platform. One idea, four expressions.
   - X: compressed and concrete. Threads of numbered posts only when the
     material earns it. Links go in a reply post, never the hook.
   - LinkedIn: standard capitalization, professional register, complete
     mechanism, one self-contained post.
   - Threads: conversational, stands alone with zero X context, short.
   - Bluesky: compact and technically literate, hard 300-character limit.
   - No two versions share more than a hook's worth of identical phrasing.
   - Links stay on their own platform or the open web, in every direction.
   - Nothing references the posting setup itself.
6. Create one bundle per platform from `assets/bundle.example.json`. Each
   bundle enables exactly one platform. Title it
   `PROPOSED: [agent-name] <slug>`.
7. Gate and create each draft:

   ```sh
   scripts/stopslop-gate review --bundle /abs/path/x.json
   typefully post --bundle /abs/path/x.json --stopslop-receipt 'oss1...'
   ```

   The receipt binds the exact bundle to the exact current rules. Any edit
   to either invalidates it. The result is a private draft only.
8. Rank the campaign timing, read-only:

   ```sh
   typefully smart-plan --draft x=ID --draft linkedin=ID \
     --draft threads=ID --draft bluesky=ID \
     --topic "the claim" --kind flagship --freshness timely
   ```

9. Hand the owner the draft ids, private URLs, exact copy, and the proposed
   times. Scheduling and publishing happen only through the preflight-token
   flow with the owner's approval of the exact plan.

## Threads engagement mode

For an active reply presence on Threads: scout conversations from the
roster's Threads section, draft the reply as a single-post threads bundle,
gate it exactly like post copy, and hand the gated text plus the target URL
to the owner. The owner posts replies themselves — the Typefully API cannot
post Threads replies, and nothing automates inside the owner's accounts.

## Fail-closed conditions

Do not create a Typefully draft when:

- the live Stop Slop rules were not read in this invocation;
- the public/private boundary is unclear;
- the copy fails a rules check or the deterministic hard check;
- the content duplicates the pipeline;
- any of the four platforms is missing without the owner explicitly
  narrowing the campaign;
- platform copy is a mechanical cross-post instead of a native adaptation;
- a link crosses social platforms, or a quote or link amplifies a competitor;
- a quote URL is invented, stale, indirect, or a weak fit;
- the target is not the lane's writable social set.
