# Proofie as an MCP server

`python -m copilot.mcp_server` exposes Proofie to a seller's AI assistant (Claude Desktop, Claude Code, or
any MCP client) over stdio. The assistant can read a dispute file, see what evidence is missing and ask the
copilot to prepare a **draft**. It cannot approve anything.

## Why

PayPal's own agent toolkit (`paypal/agent-toolkit`) offers `list_disputes`, `get_dispute` and
`accept_dispute_claim` for disputes. It has **no evidence tool**: nothing to prepare or provide evidence
(toolkit README, "Dispute Management", re-read on 2026-10-17). This server fills that gap with the
traceable, human-approved evidence workflow the web app already has. We deliberately do not expose
`accept_dispute_claim`: accepting liability and refunding is too dangerous for an autonomous agent.

## Tools

All tools return plain JSON with seller-friendly text and a `review_url` into the web UI
(`http://127.0.0.1:8000/...`). PayPal is only read (GET) unless the optional send tool is enabled.

| Tool | What it does |
|---|---|
| `list_disputes` | Disputes from PayPal, humanized status, deadline only if PayPal gives one ("No verified deadline available" otherwise), and the auto-prep status. |
| `get_dispute_file(dispute_id, include_text=false)` | PayPal context, the checklist (state, basis and action per check), and the draft package with its approval state, claim count and send-status banner. By default it leaves out the claim text and the per-check seller messages, because they quote values read from the seller's documents; `include_text=true` returns them. |
| `what_is_missing(dispute_id)` | Only the checks that need action (missing, unverifiable, conflict), each with its concrete action. |
| `prepare_draft(dispute_id)` | Runs the same idempotent preparation as the inbox auto-prep. Drafts only; one preparation per dispute and data version. |
| `get_send_status(dispute_id)` | Local send-attempt registry plus a fresh GET. Says "received" only when PayPal's own record shows the content. A refusal because PayPal already lists evidence from the seller is said in plain words, not as the raw precondition. |
| `send_approved_package(dispute_id)` | **Optional, off by default.** Registered only if `COPILOT_MCP_ALLOW_SEND=true` when the server starts. See below. |

The four read tools (`list_disputes`, `get_dispute_file`, `what_is_missing`, `get_send_status`) are marked `readOnlyHint=true` and `destructiveHint=false` for the client. `prepare_draft` writes the draft to the local database and may call the local model, so it is marked `readOnlyHint=false`, `destructiveHint=false` and `idempotentHint=true`; the optional send tool is marked `readOnlyHint=false`. There is no approve tool, in any configuration.

Everything a tool returns reaches the MCP client's model provider (for example, Claude Desktop sends tool results to Anthropic's servers), which is why `get_dispute_file` withholds claim and document text unless asked.

## What an assistant can and cannot do, tested

The MCP server has no tool to approve. The table is Proofie's `tools/list` as a client sees it (the hints are `ToolAnnotations`); a test reads the live `tools/list` and compares it with these rows.

| Tool | readOnlyHint | destructiveHint | idempotentHint | What an assistant can do with it |
|---|---|---|---|---|
| `list_disputes` | true | false | not set | Read the seller's disputes (GET). |
| `get_dispute_file` | true | false | not set | Read one dispute: PayPal's context, the checklist, and the draft's approval and send status. Document and statement text only with `include_text=true`. |
| `what_is_missing` | true | false | not set | Read which checks still need the seller. |
| `get_send_status` | true | false | not set | Read the attempt registry and a fresh GET; `received_by_paypal` only when PayPal's own record shows the content. |
| `prepare_draft` | false | false | true | Save a draft package in the local database (the draft step may call the local model). It cannot approve and cannot send. |
| `send_approved_package` | false | not set | not set | Exists only when the server starts with `COPILOT_MCP_ALLOW_SEND=true`. Sends a package a human already approved in the web page, if the approval still matches the content. |
| `approve` | no such tool | | | Does not exist, in any configuration. |
| `accept_dispute_claim` | no such tool | | | Does not exist, in any configuration. No refund, offer, message or escalation tool exists either. The seller can accept a claim in Proofie's web page as a last option; the assistant cannot. |

PayPal's own agent toolkit (`paypal/agent-toolkit`, package version 1.11.0, README read on 2026-10-19) lists three Dispute Management tools: `list_disputes` and `get_dispute`, which read, and `accept_dispute_claim`, which accepts the claim and so concedes the dispute. The README's own configuration example enables only `disputes: { list: true, get: true }`. The toolkit has no tool to prepare or provide evidence; Proofie has no tool to accept a claim.

**What was tried.** `tests/local/test_mcp_safety_card.py` puts text that gives orders (approve, send, accept the claim, refund, escalate, leak a secret, claim a receipt) in a buyer message, in the text of a seller document and in a draft statement, and calls every tool through the server's own dispatch. The draft step runs with a stand-in for the model that obeys the text. After every payload the test checks that PayPal received no write, no approval row exists, no send attempt exists, no table outside the draft's own holds a row, and the audit log holds only the server's own entries. The table below is compared with the lists in that file, so it cannot drift.

| What was tried | Count |
|---|---|
| Injected payloads | 8 |
| Tool calls per payload | 6 |
| Tool calls with injected text | 48 |
| Calls that return the injected text | 1 of 6 |
| Tool names an assistant might try, absent | 9 |
| Hostile dispute ids refused on every tool | 12 ids x 4 tools |
| PayPal writes | 0 |
| Approvals created | 0 |
| Send attempts created | 0 |
| Rows in tables the draft does not fill | 0 |

The injected text comes back to the assistant in one place: the draft statements returned by `get_dispute_file` with `include_text=true`. The server does not filter or rewrite it, so an assistant should treat that field as untrusted data. What the server guarantees is that no tool can act on it: no tool approves, accepts a claim, refunds or escalates, and the send tool refuses a package no human approved.

**Not covered, and not claimed.** An assistant that drives the web page in a browser can press the page's buttons; the approval button is a control of that page, and such a client is outside this server and outside this test. The test also does not show how a real model reacts to injected text: the stand-in obeys it, which shows what the server does with an obedient model.

## Safety model

- **Approval stays human.** Approval is a button in the web UI (`/disputes/<id>/packages/<n>`). The server has
  no code path that creates an approval.
- **Send is opt-in and gated.** `send_approved_package` refuses unless a human already approved the latest
  package and that approval's fingerprint still equals the package's current content (an edit, a new
  attachment or new tracking invalidates it). It does not re-implement sending: it calls the same
  `copilot/core/send_service.py` function the web route calls after its own CSRF check, so it inherits the
  attempt registry (claim before POST), a fresh GET before sending, PayPal-link validation, bounded
  reconciliation (about 5/15/30 s), the once-per-dispute rule (no send while PayPal lists evidence from the seller) and **no automatic retry**. "Received by PayPal" is reported only when
  PayPal's GET shows the content.
- **Sandbox only.** `SandboxClient`'s base URL is the sandbox host (`pilot/client.py:23`); the send path accepts
  only that host (`copilot/core/send.py:27`); the allowlists for the host and the paths the app follows are in
  `copilot/core/context.py` (lines 36-37 and 91). The server never follows a URL it is given.
- **Untrusted arguments.** `dispute_id` must match a strict PayPal id format (e.g. `PP-R-ABC-12345`, a made-up id);
  anything else is rejected before any call.
- **Audited.** Every tool call is written to the audit log as actor `system` with `{"via": "mcp"}`.
- **The model's text is never evidence.** AI-suggested content is labelled as such; statuses come from PayPal.

## Register it

Prerequisites: the repo's `.venv` with `requirements-app.txt` installed, and `.env` with sandbox credentials.
The web UI (`python -m copilot.app`) must be running for the review links to open, and for a human to approve.

**Claude Code** (project scope): copy `.mcp.json.example` to `.mcp.json` in the repo root (on macOS or Linux, change `command` to `.venv/bin/python`), or run from the
repo root:

    claude mcp add proofie -- .venv\Scripts\python.exe -m copilot.mcp_server

On macOS or Linux the same command is:

    claude mcp add proofie -- .venv/bin/python -m copilot.mcp_server

**Claude Desktop**: add to `claude_desktop_config.json` (use absolute paths):

    {
      "mcpServers": {
        "proofie": {
          "command": "C:\\path\\to\\repo\\.venv\\Scripts\\python.exe",
          "args": ["-m", "copilot.mcp_server"],
          "cwd": "C:\\path\\to\\repo"
        }
      }
    }

On macOS or Linux use the interpreter of the virtual environment there and forward slashes:

    {
      "mcpServers": {
        "proofie": {
          "command": "/path/to/repo/.venv/bin/python",
          "args": ["-m", "copilot.mcp_server"],
          "cwd": "/path/to/repo"
        }
      }
    }

To enable the optional send tool add `"env": {"COPILOT_MCP_ALLOW_SEND": "true"}`. Leave it off unless you
understand that the assistant can then trigger a send of an already-approved package.

## Server id and optional environment

- The MCP server id is `proofie` (`SERVER_NAME` in `copilot/mcp_server.py`); register it under that name. The Python package
  path stays `copilot/`, so the command is still `python -m copilot.mcp_server`.
- `COPILOT_MCP_ALLOW_SEND` - see above; unset or anything other than `true` means no send tool exists.
- `COPILOT_MCP_LOG_NAME` - optional. The server's PayPal calls are appended, sanitised, to `copilot_mcp.jsonl` in the evidence folder;
  this names another file instead (`<name>.jsonl`, same folder). Only `[a-z0-9_]`, up to 40 characters; anything else falls back
  to the default, so it can never point outside `evidence/`. The smoke test below uses it. The log stays local and
  private and is never published: the public repository has no `evidence/` folder (D-2026-10-05-11).

## Smoke test (live, read-only)

`demo/mcp_smoke.py` starts this server over stdio with the MCP Python SDK client and checks, against the PayPal
**sandbox**, that it works end to end without being able to write anything:

    .\.venv\Scripts\python.exe demo\mcp_smoke.py            # macOS / Linux: .venv/bin/python demo/mcp_smoke.py; ends by itself

- It asserts `COPILOT_MCP_ALLOW_SEND` is unset, that the tool list is exactly the five read and draft tools above (no approve tool, no
  send tool), then calls `list_disputes` and `get_dispute_file` on one sandbox dispute and nothing else.
- Evidence: the one run on 2026-10-05 is recorded as claim **EV-03** in `EVIDENCE-INDEX.md`, which gives the hash
  and line range of the private log and our own summary of what it shows (the server writes its own PayPal calls to that
  log through `SandboxClient` with `COPILOT_MCP_LOG_NAME=mcp_smoke`; the script adds summary lines). The log is not
  published. Result: the sandbox dispute is in the list, `get_dispute_file` returned its file, and the only PayPal calls
  were 4 GETs plus 2 OAuth token requests.
- Known detail of that run's log: it was written before the sanitiser fix, which then redacted every key
  named `state`, so the checklist `state` values in that log read `[redacted]`; the check ids and actions are intact.
  Since that fix, `state` is redacted only inside address-like objects. The log is append-only, so that line was not rewritten.

## A second, separate server: local contracts

A second server, separate from the one above, serves only cases stored locally: `python -m copilot.features.mcp_local_contracts.server`, over stdio. It starts only when `COPILOT_FEATURE_MCP_LOCAL_CONTRACTS=true`, and the hosted demo has that flag off. It has three tools: `list_local_cases`, `read_local_case` and `prepare_local_note`. Each call is checked against a local allowlist that assigns case ids to the server's configured actor (`PROOFIE_MCP_LOCAL_ACTOR`), and each call is written to a local audit file. It reads package and document records from the configured local database and registers no PayPal client, model, send, approve or accept-claim tool.

## Limits

- `prepare_draft` runs the preparation synchronously and may take a while when the local model is used.
- `send_approved_package` can block about 50 s while it reconciles with PayPal.
- Stdio only; the server shares the web app's local SQLite database, so run it on the same machine.
