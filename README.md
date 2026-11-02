# Proofie

> Status: **Final cut of the hackathon submission (2026-11-01, tagged v1.0 locally); this documentation matches that cut. The kit of record is the one built from it; its hash is given with the submission, not in this file.**

**For judges.**
1. Next step: follow [docs/JUDGE-RUN.md](docs/JUDGE-RUN.md) to start the app. A run from scratch asks two sittings of you: your sandbox credentials and one setup command. The sandbox disputes are opened for you by one command on the seller side; if you prefer the buyer's own path, that is one more sitting as the sandbox buyer. The setup script (`tools/sandbox_seed.py`, standard library, `--dry-run` first) creates in your own PayPal Sandbox account two sales by the same sandbox buyer, one with a DEMO tracker; you then open the "item not received" dispute as that buyer, in seven steps on the app's "Judge as buyer" page. The inbox marks the seeded sale ("Opened by you as buyer — seeded sandbox sale").
2. Check your setup first: `python tools/judge_check.py` (see "Check your setup" in [docs/JUDGE-RUN.md](docs/JUDGE-RUN.md#check-your-setup)). It works before you have a dispute: the rows that need one say so and point to section 3 of the guide.
3. Nothing is sent to PayPal until you approve the exact package; the approval screen shows the hash of what you approved. At send, the hash of the approved package is written into the evidence line of the request (`copilot/core/send_service.py:821`), and the dispute is read back from PayPal by GET (`copilot/core/send_recovery.py:53-54`); the send shows as confirmed when PayPal's record lists the same evidence type, tracking number and file names (`copilot/core/send.py:137-150`).

4. Each case opens in the v3 case workspace: an overview and task views for reviewing the claims, preparing and checking the evidence package, approving it, sending it and keeping the records. The case bar shows three links (Overview, Review claims, and Evidence package or Prepare package) and a closed "Other case links" block. The pages one level down (history, ledger, draft, call script, two clocks, first guide, lost package paths, buyer note, third-party statements and the fulfilment moment) are three clicks from the overview: open "Other case links", open the task page, then follow its link. Every one of them is linked from a task page; this release does not offer them in two clicks.

**Why it exists.** PayPal's agent toolkit (`github.com/paypal/agent-toolkit`, its README's "Dispute Management" list, re-read 2026-10-17) gives an AI assistant three dispute tools: `list_disputes` and `get_dispute` to see a seller's disputes, and `accept_dispute_claim` to concede them and refund; it exposes no tool to defend one (`provide-evidence`, make-offer, appeal and send-message are not there). Proofie covers part of what the toolkit does not, the other way round: it prepares and sends the evidence under a human approval bound to the exact content, and it never exposes accept-claim to any agent.

Proofie is a local web app that helps a small seller answer a PayPal "item not received" dispute. It builds an **evidence file** and checks every claim in it:

- against the **PayPal data** of the dispute and the transaction, read live from the PayPal **sandbox**;
- against the **published PayPal Seller Protection requirements for Mexico** (terms dated 2025-01-13), one by one, before anything is sent;
- with a **source cited for every claim**: a document you uploaded, a PayPal field, or your own statement, labelled as such.

**The approval binds one exact version.** The seller reviews the file and approves that version. The approval is bound to a hash of the content, so any later edit invalidates it.

**Sending is reconciled.** After the POST, the app re-reads the dispute from PayPal and shows the result headline "Evidence sent to PayPal" and the tag "Received by PayPal (confirmed by reading the dispute back)" when the read-back finds the evidence there (EV-09 and EV-10 in the evidence index: two sends of evidence by hand, on earlier release candidates, and EV-21 and EV-23: two sends on the rc4.2 candidate, walked by an AI session, each one POST and a read-back). It never re-sends automatically, and Proofie itself refuses a second send of the same approved package.

**AI assists; it does not decide.** A local language model (Ollama) can read documents and draft text, but it is an **assistant**: code validates numbers, dates and ids, and a human approves. See [docs/AI-USAGE.md](docs/AI-USAGE.md).

**Measured, with failures kept.** In the candidate-specific held-out measurement dated 2026-10-21, the local model passed every scored check in 52 of 72 runs (72.2%; run-level Wilson 95% interval 60.95%–81.24%); 20 runs failed. It used the 24 frozen held-out synthetic cases across three seeds. In the contradictory bucket, 1 of 9 runs passed every scored check. In the injection bucket, all 9 runs passed scored checks and `injection_followed` was 0; this does not establish general resistance to injection. Validators passed 62/72 runs and the expected action was chosen in 55/72. A person reviews every send. W21 (2026-10-20, 54/72, 22 fail-to-pass and four regressions) and the original-prompt baseline (2026-10-19, 36/72) are separate historical series; no paired cut comparison is claimed. See [docs/AI-USAGE.md section 3.11](docs/AI-USAGE.md#311-candidate-specific-held-out-cut72-measurement-2026-10-21).

**The account's own number.** An account page counts the account's completed sales (Transaction Search) and its disputes over the three full calendar months before the month of the case, and compares the dispute rate with the high-volume threshold that PayPal's help describes (more than 100 sales and 1.50 %, PayPal Help article 350, read 2026-10-05). The arithmetic is integer-only, every constant carries its source and read date, and the fee amounts come from PayPal's US business fees page: Proofie has no Mexico fee schedule.

**The Rule of Two, applied.** An AI agent should not hold all three of these without a human in between: untrusted text, the seller's data, and a write to PayPal. The idea is Meta's Agents Rule of Two (https://ai.meta.com/blog/practical-ai-agent-security/) and Simon Willison's lethal trifecta (https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/), pages read 2026-10-17; Proofie applies it, and neither page endorses Proofie.

```
The "Rule of Two": an AI agent should not hold all three of these without a human in between.
(Idea from Meta's Agents Rule of Two and Simon Willison's lethal trifecta.)

   1. Untrusted text          2. The seller's data         3. A write to PayPal
   (the buyer's messages,     (the PayPal dispute and      (send evidence, send a message,
    uploaded documents)        sale, the seller's files)    make an offer, add tracking)
          |                          |                            ^
          +------------+-------------+                            |
                       v                                          |
        Proofie's local model reads 1 and 2.             The seller approves the exact content.
        It is an assistant: it has no tools and          The approval is bound to a hash of it;
        cannot call PayPal, approve or send.             any edit clears it.
                       |                                          |
                       +---------- human gate --------------------+
```

This limits what the model can do; it does not detect a prompt injection ([docs/AI-USAGE.md](docs/AI-USAGE.md) section 3.4 reports what we measured).

**MCP server, optional and local.** Proofie can also run as an MCP server, so a seller's AI assistant can read a dispute file, see what is missing and prepare a **draft**. Approval stays a button in Proofie's page. See [docs/MCP.md](docs/MCP.md).

## What it is not (limits, stated plainly)

- **Sandbox only.** The PayPal client is fixed to `api-m.sandbox.paypal.com` (`pilot/client.py`). There are no production results of any kind, and no claim about win rates, time saved or money recovered.
- **The model can pass the validators and still recommend the wrong action.** The revised-prompt evaluation above retains contradictory-document failures even when every validator passes. Code scoring, validation and right-action results are reported separately; none establishes real-world reliability. The original v2 baseline, revised-prompt held-out series, earlier v1 round and selected historical-six diagnostic are separate measurements. Human review is mandatory. See [docs/AI-USAGE.md](docs/AI-USAGE.md) for all failures and limits. If Ollama is missing or fails, **manual mode** lets you enter the data yourself.
- **No webhooks in the local app (reading back instead); the hosted copy is the one exception, pending.** PayPal's webhooks are HTTPS posts from PayPal to an endpoint on your server when an event occurs, dispute events included (`CUSTOMER.DISPUTE.CREATED`, `.UPDATED`, `.RESOLVED`). Proofie is local-first and runs on your own machine with no public HTTPS endpoint, so the local app registers no webhook. The state of a dispute comes from PayPal's own GET after each of Proofie's writes and from a bounded, visible refresh: the "Refresh from PayPal" link (and "Check again" on a dispute), and, while a draft is being prepared, a "Reload to see progress" link on the dispute list (the list does not reload itself). Nothing in the app estimates a state; a case the buyer closed is shown as "Canceled by the buyer (as stated by PayPal)" only when PayPal's own dispute record carries the outcome `CANCELED_BY_BUYER` (PayPal's published Disputes API specification), and that is a read of PayPal's recorded outcome, nothing more. Each send is recorded in an attempt registry before the network call, so a second click does not send again. What stops a repeat depends on the write. Evidence: PayPal's own record, read fresh before the POST (a send is refused while PayPal lists evidence from the seller or offers no provide-evidence link), plus the installation's attempt registry. A message: the installation's own records (a reservation taken in the local database before the POST), and a read of PayPal's own message list, so a message that PayPal already lists with the same text is refused before the POST. An offer: the installation's own records plus PayPal's own links. A tracker registration: the installation's records, and the prepare step refuses a number that PayPal already lists. The hosted demo's records are cleared on restart, redeploy or idle sleep, so after one of those the reservations are gone: a message that PayPal's list already shows with the same text is still refused, but one that the list does not show yet can be approved and sent again while PayPal still offers send-message; in the sandbox, PayPal did not deduplicate send-message by its `PayPal-Request-Id` header (observed 2026-10-16, EV-11). When the outcome of a send is uncertain (a timeout, a retryable error), the app checks with PayPal by GET before any retry and never re-sends on its own. A webhook receiver that verifies each event with PayPal before trusting it exists in the code and is switched off in the local app; `tools/register_webhook.py` is a helper that prepares a webhook registration for a deployment with a public HTTPS endpoint (a dry run unless a person runs it with `--execute`), and no webhook is registered yet. In the hosted copy only, the receiver and its verification are switched on in the Blueprint (`render.free.yaml`; `COPILOT_FEATURE_WEBHOOKS` and `COPILOT_FEATURE_WEBHOOKS_VERIFY`). Pending: the owner has not registered the copy's HTTPS address in the sandbox app yet (`tools/register_webhook.py --execute` is his step), so until he does the hosted copy receives no events. Once it does, each dispute event is verified one by one through PayPal's own verification call (`verify-webhook-signature`); an unsigned or badly signed event is refused and logged; the copy then reads the dispute from PayPal (one GET) and shows each verified event in the dispute's timeline as "PayPal notified Proofie at <time> (verified)", with Proofie's own read-back status and never the event's own, and no state is written from an event without that read. Stated limits: the free instance sleeps, so an event sent to a sleeping instance may time out and PayPal retries; events are lost on restart like every hosted row; the sandbox account is shared, so visitors see each other's events. The local app does not change: its published `.env.example` keeps both settings false, so it keeps the bounded, visible read-back.
- **Evidence is sent once per dispute.** Proofie sends evidence once per dispute: it never sends when PayPal already lists evidence from you for that dispute (`pilot/step5_send_evidence.py`, `check_preconditions`, applied before any POST); the package and dispute pages then say so and offer no send. If a send reached PayPal with a file missing, you continue in PayPal's Resolution Center.
- **Attached files are confirmed by name.** After an evidence send the read-back matches each attached file by its file name, because PayPal's dispute record lists a document by its name and a link, not by its contents (PayPal's published Disputes API specification; see `docs/CONFORMANCE.md`). PayPal's record gives the app nothing to compare the bytes with, so a name match alone does not show that PayPal holds the same bytes; the app does keep the SHA-256 of each file it sent, and the approval covers the attached files. Two same-name files are handled conservatively: a send counts as confirmed only when each attached name appears in PayPal's read-back at least as many times as it was attached (`copilot/core/send.py`), and a second file with the same name cannot be attached to one package (the page says to detach the other file or upload this one under another name). How PayPal lists two documents with the same name was never observed, so a shortfall is reported as not every file confirmed, never as confirmed.
- **An optional hosted demo exists, and you do not need it.** It is deployed on Render's free tier from the Blueprint `render.free.yaml` in this repository. It runs in manual mode (no local model) behind an access code, against the PayPal sandbox only; the MCP server and the assistant are not part of the hosted demo; it uses one shared sandbox account, so everyone who opens it sees the same disputes and uploads; its own notice reads: "Each restart, redeploy, or idle sleep starts this demo with a fresh session. The first request after idle may take about one minute." The access page says what the copy is, and each inbox row is marked "Available for a full walk.", "Already answered by a visitor." or "PayPal is not asking for evidence on this dispute now.", from that case's own read. One page, behind the access code and linked from the inbox, shows a legacy synthetic replay fixture. It is static example data, not a recorded model output or an assistant run; nothing on that page runs or sends anything. Its address and the access code come with the submission, not with this repository; everything in this README is for your own copy.
- **Deadlines are shown only when PayPal states one.** PayPal's help for sellers says you must respond within the first 10 days after a dispute is created, and if you do not and the buyer escalates it to a claim, PayPal resolves the case in the buyer's favour (help article HELP1083, read 2026-10-16). The dispute's own `seller_response_due_date` is the date by which the seller must respond to that case (PayPal's published Disputes API specification). The due date appears, in Mexico City time, only when PayPal's response carries that field; otherwise the page says "No verified deadline available". Nothing is estimated.
- **Keep your records for at least 180 days.** A buyer can open an "Item not received" dispute within 180 days of the date of the payment (PayPal help article HELP1173, read 2026-10-16), so keep the records of orders, shipments and messages at least that long.
- **Issuing a refund outside a dispute stays in PayPal's Resolution Center; accepting the claim is available in Proofie as the seller's last option.** Accepting the claim has a check page of its own that shows PayPal's refund amount and currency and the sentence "This refunds the buyer and closes the dispute. Proofie cannot undo it."; you type the amount phrase, approve the exact request by hash and send it as a separate click. On the sandbox run of record, PayPal showed the refund at once, moved the case to its review, and resolved it in the buyer's favour within minutes; the sent page names the state PayPal's read-back returns. It is offered while PayPal's fresh read of the dispute shows the action, listed after the offer, the message and the defence, and the model never suggests it. On the hosted copy it is allowed on the judge's own pool case and refused on every other case. The one escalation it can make is a sandbox-only judge re-arm by PayPal's escalate link, approval-bound, in the helper script or the hosted demo's own button, never in the seller's flow; the resolution page never escalates. The resolution page prepares an offer, a refund offer that PayPal's make-offer sends for the buyer to accept (REFUND is the only offer type the sandbox lists, `copilot/features/resolution/actions.py:3-4`), or a message to the buyer. A separate check page shows the exact request and sends it once only after you approve it; a sent page then shows what was sent and whether PayPal confirmed it (EV-12, EV-13 and EV-18 in the evidence index: the read-back after a message, a tracking registration and an offer, on earlier builds, driven by a script). Of the official toolkit's three dispute tools, accept-claim (`accept_dispute_claim`) is the one that changes a dispute; accepting the claim is the seller's last-resort option in Proofie, shown only when PayPal offers it and sent after the seller's own approval; the assistant has no tool to approve, send or accept a claim.
- **The Mexico rules are context, not legal advice.** The rule set is versioned, and its official source is linked on the review page. It does not predict whether PayPal will rule in your favour. A United States rule set ships too (PayPal's US Seller Protection terms, last updated 2026-01-26, read 2026-10-04 and 2026-10-06): `COPILOT_RULES_COUNTRY` chooses the rule set for the installation, `MX` by default, `US` the other value.
- **How the sandbox disputes are opened.** With the setup script, the buyer account opens the dispute itself in PayPal's Resolution Center after the script prepares the test payment and its tracking; every piece of synthetic data is labelled as such. The judge can open a ready-to-answer sandbox dispute with one command on the seller side: a card-funded sale with PayPal's published test card, PayPal's chargeback call and one supporting note; PayPal takes one to three minutes to prepare the case; no buyer account or login is needed. Each run creates one sandbox sale and one dispute. (`tools/sandbox_card.py`; `docs/JUDGE-RUN.md`)
- **What this release includes, and what stays off.** This release includes the AI message draft, delivery facts, fulfilment registration, resolution offers and messages, the contract-checks page (`/conformance`), PayPal's own timeline of a dispute (the dated items its record gives, behind `COPILOT_FEATURE_TIMELINE`), PayPal's own words behind each check for the Mexico and United States rule sets (a quoted clause with its source, behind `COPILOT_FEATURE_CLAUSES`), a Spanish version of each statement for the Mexico rule set (behind `COPILOT_FEATURE_BILINGUAL`): typed by the seller or, with the local model, drafted by it and reviewed by the seller, with code checking that numbers, dates and tracking codes match the English (the hosted copy: typed only); approving covers the English and the Spanish text, and PayPal receives the English statements only; a downloadable case file of a package (a zip with the print view, the files and a manifest of hashes, checked offline with `python -m copilot.verify --case-file`); and, in the hosted demo only, a legacy synthetic replay fixture, not a recording of a model run. Write actions remain subject to the app's approval and access controls. The appeal call is on: it appears only when PayPal's fresh read of the dispute offers an appeal, and it is sent once after the seller approves the exact request. It has not been exercised against PayPal, because no sandbox dispute of ours has offered an appeal. Webhooks and webhook verification are off in the published `.env.example` and on only in the hosted copy's Blueprint, pending the owner's registration of its address. The published `.env.example` contains 75 feature settings: 72 enabled; `COPILOT_FEATURE_DEMO_DOCUMENTS=false`, `COPILOT_FEATURE_WEBHOOKS=false` and `COPILOT_FEATURE_WEBHOOKS_VERIFY=false` are off. Some of these settings belong to the pages in the next section.
- **Where it meets PayPal's published integration guidance and where it deliberately deviates** is listed point by point in [docs/CONFORMANCE.md](docs/CONFORMANCE.md).

## Ten more local pages, on by default

Ten further pages ship in this release. Each has its own `COPILOT_FEATURE_*` flag and each is on by default in `.env.example`. None of them sends anything to PayPal or calls the local model: a send still needs a person's approval on the existing check pages.

| Page | Address | What it does |
|---|---|---|
| Evidence dossier | `/features/evidence_dossier` | Assembles a dossier from recorded data you supply and reviews each claim against its sources; the source labels do not verify authenticity. |
| Explainable case queue | `/features/explainable_queue` | Ranks recorded case snapshots offline and explains the order, with deadlines shown in Mexico City time. |
| Negotiation drafts | `/features/negotiation_drafts` | Prepares versioned proposals bound to recorded facts and permitted actions; nothing is sent. |
| Sale readiness | `/features/sale_readiness` | An editable checklist for a physical, digital or service sale; tracking is prepared locally and never registered. |
| Appeal workspace | `/features/appeal_workspace` | Compares a recorded outcome and evidence versions by content hash, offline; it is a preview, not an appeal submission. |
| Local MCP contracts | `/features/mcp_local_contracts` | Describes the three tools of a separate local stdio server (`list_local_cases`, `read_local_case`, `prepare_local_note`); it has no send, approve or accept-claim tool. |
| Evaluation workbench | `/features/evaluation_workbench` | Validates and shows a supplied evaluation manifest; it runs no scorer, benchmark or model. |
| Judge walkthrough | `/features/judge_walkthrough` | An offline guide that follows one synthetic case from the inbox to its saved package. |
| Local autopilot | `/features/local_autopilot` | Checks a proposed action against recorded facts and a merchant policy and saves a result marked SIMULATED — NOT SENT. |
| Demo hub | `/features/local_demo_hub` | Indexes the case flow and the records of these pages; it creates no workflow of its own. |

The hosted demo's Blueprint (`render.free.yaml`) sets `COPILOT_FEATURE_MCP_LOCAL_CONTRACTS=true` too: the hosted copy shows the page, which only describes that stdio server; the server itself runs on your machine, not on the host. The other nine pages ship in the kit and the local app, and the hosted copy shows them as its Blueprint enables them.

## Requirements

- **Python 3.12.** Tested on Windows 11 and on Linux (under WSL) with Python 3.12; macOS not tested.
- **A free PayPal Developer account and your own sandbox app.** We ship no credentials (see [docs/JUDGE-RUN.md](docs/JUDGE-RUN.md)).
- **Optional: [Ollama](https://ollama.com) with the default model `qwen3.5:9b`.** About 6.6 GB on disk (6,550,825,550 bytes, as Ollama lists it). Measured once with `nvidia-smi` on one 12 GB card: 7.85 GiB of total GPU memory in use at an 8k context (the model's own share 6.46 GiB) and 8.12 GiB at 16k; nothing was tested on an 8 GB card (the evaluation's own peak, 7897 MiB, is a different measurement, in [docs/AI-USAGE.md](docs/AI-USAGE.md)). The AI step needs an NVIDIA GPU: before every model call the app runs `nvidia-smi` and refuses when another model is loaded in Ollama or the GPU is busy. Without Ollama, or when that check fails, the app runs in manual mode, up to and including the send.

## Install

**Windows (tested; every package pinned by version and sha256):**
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock.hashes.txt
```

**Linux (tested; every package pinned by version and sha256):**
```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock.linux.hashes.txt
```

**macOS (not tested):**
```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt     # the app plus the test tools; -r requirements-app.txt alone is enough to run the app
```

`requirements-app.txt` pins the packages the app uses directly; `requirements-dev.txt` adds the test tools (pytest, pytest-timeout, playwright). `requirements-app.txt` also pins the three packages `jsonschema` pulls in; every other package those pull in is chosen by pip at install time, so a few versions can differ from the locks. `requirements.lock.hashes.txt` (Windows, includes the Windows-only `pywin32`) and `requirements.lock.linux.hashes.txt` (Linux) pin every package, the app, the test tools and everything they pull in, with sha256 hashes; `requirements.lock.txt` and `requirements.lock.linux.txt` are the same lists without hashes.

Optional model:
```bash
ollama pull qwen3.5:9b
```

Re-score the held-out set of the draft step with `python tools/eval_draft.py --heldout`. Optional: if a local model already serves at 127.0.0.1:11434, the evaluation uses it; otherwise it replays the recorded model outputs, and no model runs. The report lists every failure with its manual-fallback mark.

## Configure (your own PayPal sandbox app)

1. Copy `.env.example` to `.env` and replace both placeholder values; untouched placeholders count as missing credentials. Four more local fronts are visible once the file is copied: open the case as the sandbox buyer (`/features/judge_as_buyer`), a buyer note (`/fulfilment/<capture_id>/buyer-note` and `/disputes/<id>/buyer-note`), a presentation guide and offline market mode (`/presentation-mode`, `/offline-market-mode/status`), and options for a package not received (`/disputes/<id>/lost-package-paths`). The file is git-ignored; never commit it.
2. Create a sandbox app at <https://developer.paypal.com> and put its Client ID and Secret in `.env`.
3. Step-by-step instructions, including how to create a sandbox dispute, are in [docs/JUDGE-RUN.md](docs/JUDGE-RUN.md).

## Run

```powershell
.\.venv\Scripts\python.exe -m copilot.app        # macOS / Linux: .venv/bin/python -m copilot.app
```

This starts one `uvicorn` process bound to **127.0.0.1:8000**; a middleware also rejects non-local requests. Open <http://127.0.0.1:8000/> (it redirects to `/disputes`).

The key path: open the dispute, add the documents, generate the package, **read every statement in full beside its source, then give one approval that names their count and is bound to a hash of those exact statements (any change voids it)** (a missing carrier on the tracking evidence blocks approval, because PayPal rejects evidence with no carrier), then **print or send**. Approval records a local receipt and sends nothing; on the package page, "Send evidence to PayPal" is what reaches PayPal. For an offer, a message or tracking, a separate check page shows the exact request, and there approving it is the send. Nothing is sent on its own. The step-by-step version, and the other pages of this release, are in [docs/JUDGE-RUN.md](docs/JUDGE-RUN.md) section 4.

To check your own setup before you start (Python, `.env`, the app, an open sandbox dispute, every seller page), run `python tools/judge_check.py`; it sends only local GET requests and prints a table ([docs/JUDGE-RUN.md](docs/JUDGE-RUN.md), "Check your setup").

## Point your assistant at Proofie (MCP, optional)

`python -m copilot.mcp_server` runs Proofie as a local MCP server over stdio for an assistant such as Claude Desktop or Claude Code; registration steps are in [docs/MCP.md](docs/MCP.md). It has five tools, which read and draft only. Four are the read tools, marked read-only for the client (`readOnlyHint`): `list_disputes` (the disputes from PayPal, with the due date only when PayPal gives one), `get_dispute_file` (PayPal's context, the checklist and the draft package's state), `what_is_missing` (only the checks that need action) and `get_send_status` (the local send registry plus a fresh read from PayPal). The fifth, `prepare_draft` (the same draft preparation as the inbox), writes the draft to the local database and may call the local model, so it is not marked read-only. It has no approve tool in any configuration: approval is a button in Proofie's page. A sixth tool, `send_approved_package`, marked as not read-only, exists only when `COPILOT_MCP_ALLOW_SEND=true` is set when the server starts, and it sends only a package a person already approved there, while that approval's fingerprint still matches the content. There is no accept-claim tool. By contrast, PayPal's agent toolkit (`github.com/paypal/agent-toolkit`, its README's "Dispute Management" list, re-read 2026-10-17) lists `accept_dispute_claim` among its three dispute tools.

## Create a sandbox dispute

You need a dispute in `INQUIRY` / `WAITING_FOR_SELLER_RESPONSE`. The setup script `tools/sandbox_seed.py` (one file, standard library only, nothing to install) creates two sandbox sales by your buyer account, one captured with a DEMO tracker and one without, waits for the buyer's approval of each, and then waits for the dispute; the buyer opens that dispute by hand in PayPal's Resolution Center (creating one by API needs buyer-side credentials, which this guide does not ask you to set up). Run it with `--dry-run` first; the real run keeps its state and evidence outside the folder you extracted or cloned (the script refuses paths inside it). In our one real run (2026-10-12) both sales were ready in under 3 minutes, but the case the buyer opened on the sale that already had a tracker went under PayPal's review (`CHARGEBACK` / `UNDER_REVIEW`) instead of the inquiry stage, while a case opened the same day on a sale without tracking reached `WAITING_FOR_SELLER_RESPONSE` in about 4 minutes (EV-14): open the dispute on a sale without tracking. Exact steps, and the manual route, are in [docs/JUDGE-RUN.md](docs/JUDGE-RUN.md) section 3.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                              # offline tests (the default)
.\.venv\Scripts\python.exe -m pytest -m sandbox tests/sandbox     # hits the real sandbox; needs your .env
```

The tests need the dev tools: on Windows the hash-pinned install already has them; elsewhere install `requirements-dev.txt` (above). By default, `pytest.ini` deselects tests marked `sandbox` (network) and `live_model` (a running Ollama).

**Kit verification:** This README reports no kit-suite figure, because a figure written into the archive would change the archive it describes. Run the offline command above on your own copy to get the counts for your platform.

## Repository map

| Path | What |
|---|---|
| `copilot/` | The app: `app.py`; `core/` (disputes, send, attempt store, checklist, context); `features/` (the optional pages behind `COPILOT_FEATURE_*` flags); `db/` (SQLite); `web/` (routes, templates); `rules/` (versioned rule sets); `mcp_server.py` |
| `pilot/` | Code that `copilot/` reuses (sandbox client with a sanitised call log, dispute normaliser, evidence validator, send logic), plus the evaluation cases in `pilot/eval/` |
| `demo/` | Synthetic demo documents ("DEMO DATA") and demo scripts |
| `tests/local/`, `tests/sandbox/` | Offline tests; sandbox tests (deselected by default) |
| `tools/` | Secret and privacy scanners and the public-export tool (commit-time tools: they need a git checkout), the sandbox setup script `sandbox_seed.py` and the order helper used in the judge guide |
| `docs/` | Public documentation (list below) |
| `EVIDENCE-INDEX.md` | What we ran against the PayPal sandbox, as hashes, line counts and plain-language statements (ids `EV-nn`). The raw logs contain PayPal API responses, so they stay private under the PayPal Developer Agreement. The author can show them on request, and anyone can check them against the hashes. The logs recorded after 2026-10-05 on Windows have CRLF line endings; the index hashes copies of them with LF line endings (nothing else changed), so a hash is recomputed on the LF copy the author shows, or after converting CRLF to LF |

## Documentation

[Judge run guide](docs/JUDGE-RUN.md) · [AI usage](docs/AI-USAGE.md) · [Architecture](docs/ARCHITECTURE.md) · [Conformance](docs/CONFORMANCE.md) · [MCP server](docs/MCP.md) · [Evidence index](EVIDENCE-INDEX.md) · [Third-party](THIRD-PARTY.md)

## License

Apache-2.0, see [LICENSE](LICENSE). Third-party components: [THIRD-PARTY.md](THIRD-PARTY.md).
