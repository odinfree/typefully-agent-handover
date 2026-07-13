# Governance rules — publishing lane (template)

Rename to AGENTS.md in your fork. Replace <owner> with the human who owns the
accounts. This is the canonical policy for every agent operating the lane.

## Identity

- **<agent-a> / <agent-b>** — interchangeable operators (for example claude
  and codex). Any agent can run any stage.
- **<owner>** — the human. Their explicit approval gates every public or
  destructive transition.

## Session start

1. Run git pull --rebase in the lane repo.
2. Read the mailbox tail (if the team runs one) and acknowledge requests.
3. Run `typefully me` if API access is unverified in this environment.

## Hard gates

- **Draft gate:** Agents may create private drafts without prior approval,
  but only through the Stop Slop gate: a receipt from `stopslop-gate review`
  bound to the exact bundle and the current rules. Prefix agent-written
  titles with `PROPOSED: [agent-name]` and return the draft id and private
  URL. A private draft never authorizes scheduling or publishing.
- **Approval gate:** Publishing and scheduling bind the exact draft payload,
  platforms, connected accounts, social set, quota snapshot, and publish
  mode or time. Any change voids approval.
- **Token gate:** First run publish, fire, or delete without --confirm. Show
  the owner the returned plan and token. Only run the printed next_command
  after the owner explicitly approves that exact token in the CURRENT
  conversation. The token is a stale-state guard, not proof of human
  authority. An agent never generates and consumes one autonomously.
- **One writable set:** Every write uses the lane.json write_set. The CLI
  enforces this centrally, before any network call.
- **Copy gate:** Public copy is the owner's or has their explicit sign-off.
  Sign-off means the owner saw the exact final text in the current
  conversation and approved it. A delegated "write something and post it
  now" never substitutes — show the copy, get the go, then fire.
- **Topic ban:** The posting setup itself — the tools, the agents, the
  pipeline, the gates — never appears in public copy on any platform.
- **Secrets:** The API key lives in ~/.config/typefully/api_key (mode 600)
  or TYPEFULLY_API_KEY. Never in commits, logs, or pasted output.
  Confirmation tokens never persist in the repository.

## Platform rules

- One idea, four native expressions. X, LinkedIn, Threads, and Bluesky get
  their own copy. Never paste one text across platforms, and no two versions
  share more than a hook's worth of phrasing.
- Links stay on their own platform or the open web. Never link one social
  platform from another, in any direction.
- Maintain a never-amplify list (competitors). Check it before any quote
  post, repost, or third-party link. When unsure, ask the owner.
- Threads engagement is reply-first: agents draft and gate reply copy, the
  owner posts replies themselves. No automation inside the owner's accounts
  beyond the Typefully API.

## Verification

After an immediate publish, poll until processing ends. Success requires
status=published plus a URL for every intended platform. Partial results,
transport ambiguity, or timeouts are failures and get reported before any
retry. Never blindly retry a write — the server may have accepted it.
