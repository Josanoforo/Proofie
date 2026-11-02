# Architecture — Proofie

The design record of the app's first version (v1, approved on 2026-10-04 before the interface code was written), kept as written; section 9 states what this release added on top of it. This document is design, not a line-by-line description of the code. `pilot/` holds the pilot's code, which `copilot/` reuses; later fixes to it are noted in its own comments.

## 1. Goal and scope of v1
A local single-process application for one seller: dispute list → detail → upload/read documents → generate an evidence package with the AI → review/edit with traceability → approve (bound to the exact content) → send (with failure recovery) → check the result against PayPal. UI in English. Sandbox only.

## 2. Components

```
┌─────────────────────────────────────────────────────────────────┐
│ copilot/ (new package; ONE process, one start command)           │
│                                                                   │
│  web/            FastAPI + Jinja2 (server-rendered), minimal JS  │
│   ├─ routes_disputes.py   list, detail, refresh                  │
│   ├─ routes_documents.py  upload, text extraction                │
│   ├─ routes_package.py    generate, edit, review                 │
│   └─ routes_send.py       approve, send, verify, recover         │
│                                                                   │
│  core/           logic reused and adapted from pilot/            │
│   ├─ sandbox.py        = pilot/client.py (SandboxClient), no behaviour change       │
│   ├─ disputes.py       = pilot/step3_dispute.py (normalize, fetch_all_disputes)     │
│   ├─ evidence_model.py new EvidenceModel interface (see §6) wrapping                │
│   │                    pilot/step4_evidence_ai.py (validate, enforce_sufficiency, basis) │
│   ├─ attempt_store.py  AttemptStore interface (claim/read/write) + new SQLite impl., tested │
│   │                    separately (see §4); the pilot's file-based one stays as reference only │
│   └─ send.py           = pilot/step5_send_evidence.py (fingerprint, reconciliation) on top of AttemptStore │
│                                                                   │
│  db/             local SQLite access (data/copilot.db, git-ignored)                     │
│   ├─ schema.sql        see §5                                    │
│   └─ repo.py            read/write functions, one per table      │
│                                                                   │
│  docs_ingest/    text extraction from uploaded documents         │
│   └─ extract.py        pypdfium2 (PDF), type detection by content (not by extension) │
│                                                                   │
│  app.py          startup: one command (`uvicorn copilot.app:app` or an equivalent script) │
└─────────────────────────────────────────────────────────────────┘
          │                                   │
          ▼                                   ▼
  PayPal sandbox (api-m.sandbox...)    local Ollama, optional (qwen3.5:9b by default when present, D-2026-10-04-01)
```

`core/` is not a copy-paste: it adapts the pilot's functions to work with documents that come from SQLite/upload instead of `.txt` files on disk (just as `run_with_docs` already separated "resolve documents" from "run the model" in the pilot). The sandbox client, the validator, `enforce_sufficiency` and the send-attempt registry are **reused as they are** (their behaviour is already tested); what is new is the layer that connects them to SQLite and to the interface.

## 3. Source of truth per datum

| Datum | Source of truth | Notes |
|---|---|---|
| Dispute state, reason, amount, deadlines, evidence received by PayPal | **PayPal** (fresh GET) | Never stored as "true" in SQLite beyond a read cache with a timestamp; the interface always shows "checked X ago" and a "Refresh from PayPal" button that asks again |
| Uploaded documents (file, extracted text) | **Local SQLite** | The original file is not forwarded to PayPal unless the seller approves including it; the extracted text is what the model sees |
| Evidence package proposed by the AI (version N) | **Local SQLite** | Each generation is a new row, never overwritten (traceability) |
| Human edits to the package | **Local SQLite** | A claim edited by the seller becomes `basis: seller_statement` unless they attach a new source (rule already stated in the design) |
| Approval (fingerprint of the exact content) | **Local SQLite**, computed with `approval_fingerprint` from `pilot/step5_send_evidence.py` unchanged | Any edit after approval invalidates the approval row (it is not deleted: it is marked `invalidated_at`) |
| Send attempts (`pending/sent_unconfirmed/confirmed/failed`) | **Local SQLite**, a mirror of `evidence/send_attempts/*.json` but per dispute+seller in the app | As in the pilot: "sent" is never decided without a later confirmation by GET |
| Whether PayPal actually received the evidence | **PayPal** (`evidences[]`, `source=SUBMITTED_BY_SELLER`) | Never the model's text, never the local state alone |
| Payment/capture state | **PayPal** | The model never decides or asserts this |

Design invariant: the evidence send exists only behind the content-bound approval (the approval fingerprint over the exact body and attachment bytes), and accepting a claim exists on one surface, a page of its own behind the seller's typed amount phrase and a hash-bound approval; no accept-claim capability exists in the MCP server or the CLI; see `docs/MCP.md` section "Why".

## 4. Send state machine

It reuses the 4 states already tested in `pilot/step5_send_evidence.py` (the pilot's addendum, section A), with two app states added BEFORE `pending` (purely local; they never touch PayPal):

```
 [draft] --(seller approves, fingerprint computed)--> [approved]
    ^                                                  │
    │ (any edit to the package invalidates)             │ (seller presses "Send")
    │                                                  ▼
 [invalidated] <--(content changed)--  [pending] --atomic claim--> attempt created on disk/SQLite
                                           │
                                           ▼
                                    fresh GET of the dispute
                                           │
                        ┌──────────────────┼──────────────────┐
                        ▼                  ▼                  ▼
              content ALREADY in       preconditions         preconditions
              PayPal's evidences[]     fail                  OK
              (reconciled)                 │                    │
                        │                ▼                    ▼
                        │            [failed]           [sent_unconfirmed]
                        │           (retryable                │
                        │            if it was clean)         │ POST
                        ▼                                    ▼
                  [confirmed]                          2xx? ──no──> network exception:
                   (terminal,                            │          stays in sent_unconfirmed,
                   evidence                             yes         "result pending",
                   verified)                              ▼          never an automatic retry
                                                  verification GET
                                                            │
                                            ┌───────────────┴───────────────┐
                                            ▼                               ▼
                                   evidences[] shows it              does not show it yet
                                            │                               │
                                            ▼                               ▼
                                     [confirmed]                   [sent_unconfirmed]
                                                                  ("2xx but not confirmed")
```

**Recovery at startup (acceptance criterion #9):** before allowing any new action on a dispute with an attempt in `sent_unconfirmed`, the app makes a fresh GET and reconciles (exactly the logic already tested in `send()`: if the GET shows the content → `confirmed`; if not and the link is still available → retryable; if not and the link is gone → `pending verification`, blocked for new actions until a manual review). This also happens if the app restarts between the POST and the verification GET — the same case already tested with threads/simulation in the pilot.

## 5. Data model (SQLite, `data/copilot.db`, git-ignored)

Tables (a sketch, refined in code, not in this document):
- `documents(id, dispute_id, filename, content_type, sha256, extracted_text, extraction_ok, uploaded_at)`
- `packages(id, dispute_id, version, source, model, model_digest, prompt_sha256, raw_output, validation_ok, effective_next_step, review_flags_json, created_at)` — `source` = `"ai"` or `"human_edit"`
- `claims(package_id, evidence_index, claim_index, text, source_ids_json, basis, edited_by_human)`
- `package_attachments(package_id, document_id, sha256)` — which uploaded documents are attached to a specific send; `approval_fingerprint` is extended to include `sorted([sha256, ...])` of this table (review condition 2, section 11), with its own tests showing that changing the attachments invalidates the fingerprint just as changing the notes does
- `approvals(package_id, approval_sha256, approved_by, approved_at, invalidated_at, sufficiency_override)` — `sufficiency_override` is non-null only if the seller used "Mark as seller statement and continue" (condition 3); it is always audited in `audit_log`
- `send_attempts(dispute_id, package_sha256, state, request_id, http_status, ts, note)` — same contract as `pilot/step5_send_evidence.py`, now in SQLite instead of JSON on disk (one source of attempts, not duplicated). States: `pending/sent_unconfirmed/confirmed/failed` (pilot) plus `blocked` and `abandoned` (`copilot/core/attempt_store.py`, `copilot/core/send_service.py`) — a local rejection (no link, precondition) is never stored as `failed` (that is only a real 4xx from PayPal), and an attempt the seller explicitly abandoned is never stored as `confirmed`/`failed`, because what PayPal actually did with it stays unknown. **Documented limit:** `request_id` means only "this process got as far as attempting the POST" - never "PayPal accepted it". A `request_id` present with `state=sent_unconfirmed` is exactly the ambiguous case that "Check again" and the recovery read exist to resolve (a lost network response after a 5xx/429/timeout can mean PayPal did process the request); only `state=confirmed` (a row with `request_id` **and** an independent verification by GET, never just one of the two) means PayPal showed the exact content. This distinction is the basis of two rules: a send is confirmed only when a `request_id` is present, and an unconfirmed attempt for another package is never ignored just because of its age, because it may have reached PayPal.
- `audit_log(id, ts, actor, action, dispute_id, detail)` — who/what/when for every edit, approval, send

## 6. `EvidenceModel` (an interface so the model can be replaced)

```python
class EvidenceModel(Protocol):
    def propose(self, dispute: dict, documents: dict[str, str]) -> ModelResult: ...
    # ModelResult = {raw_output, parsed, checks, validation_ok, effective_next_step, review_flags}
```
v1 implementation: it wraps `step4_evidence_ai.run_with_docs` without changing its contract. If Ollama does not respond or validation fails, the app **keeps working in manual mode**: the seller can build/edit the package by hand from scratch (acceptance criterion #6), with each manual claim recorded as `basis: seller_statement`.

## 7. Screen sketches (text; sketches, not the screen text)

The wording in these sketches, and the examples quoted in sections 3 and 11, are design drafts: the screen text is in the templates and in `docs/JUDGE-RUN.md`.

### 7.1 Dispute list
```
Disputes                                          [Refresh from PayPal]  (last checked: 2 min ago)
┌───────────────────┬──────────────────────────┬──────────────────────┬────────────┐
│ ID                │ Reason                   │ Status                │ Due        │
├───────────────────┼──────────────────────────┼──────────────────────┼────────────┤
│ PP-R-DEMO-0000001 │ Item not received        │ WAITING_FOR_SELLER... │ 19d 23h    │
│ PP-R-DEMO-0000002 │ Not as described         │ UNDER_REVIEW           │ —          │
└───────────────────┴──────────────────────────┴──────────────────────┴────────────┘
[Load more]   (real pagination with next_page_token)
```

### 7.2 Dispute detail
```
PP-R-DEMO-0000001                                  Status: WAITING_FOR_SELLER_RESPONSE
Reason: Item not received        Amount: $25.00 USD      Due: 2026-10-24 20:13 CDMX (19d 23h)
(as of PayPal, checked 2 min ago — [Refresh])

Documents                                                          [+ Upload]
 - shipping_label.pdf   extracted OK   [view text]
 - receipt_scan.jpg     EXTRACTION FAILED — no text recovered  [view image]

[Generate evidence package]
```

### 7.3 Package review (core of the screen — validator alerts above the recommendation)
```
⚠ This package needs more information before it can be submitted.
   Code rule: no delivery confirmation attributable to the cited tracking source. (rule G16, the sufficiency ceiling)

Proposed evidence: PROOF_OF_FULFILLMENT
  Carrier: MEX_ESTAFETA    Tracking: DEMO8800123456     [source: shipping_label.pdf]
  Notes: "..."                                           [Edit]

Claims                                                    Basis
 "The item was shipped via Estafeta..."                   verified      [source: shipping_label.pdf]
 "Estafeta shows delivered at 13:42"                       seller_statement  [source: chat.txt]  [Edit]

Missing evidence: delivery confirmation
Conflicts: none

[ Details ▸ ]  (raw JSON, checks that passed/failed — hidden by default)

Recommended by model: submit     Enforced by code: need more information
                                                    [Add information]  [Mark as seller statement and continue]
```

### 7.4 Approval ("exactly what will be sent")
```
You are about to approve sending this EXACT content to PayPal:

Evidence type: PROOF_OF_FULFILLMENT
Carrier: MEX_ESTAFETA   Tracking: DEMO8800123456
Notes: "DEMO DATA (sandbox test with synthetic documents). ..."
Attachments: none

Fingerprint: a1c6d3a6...e791   (any change after this invalidates the approval)

[ ] I have reviewed the exact content above
[Approve]  (disabled until checked)
```

### 7.5 Sending and verification
```
Sending...  [spinner]
  ✓ Fresh GET confirmed preconditions (WAITING_FOR_SELLER_RESPONSE, link present)
  ✓ POST returned 200
  ⏳ Verifying PayPal received the content...
  ✓ Confirmed: evidences[] shows PROOF_OF_FULFILLMENT, SUBMITTED_BY_SELLER, tracking matches

Status: WAITING_FOR_SELLER_RESPONSE → WAITING_FOR_BUYER_RESPONSE
```
Ambiguous case (timeout/restart):
```
⚠ Send attempted - not confirmed by PayPal. Proofie asked PayPal three times and did not get a confirmation yet.
  Proofie will not send it again on its own. [Check again]
```

### 7.6 Recovery banner at startup (if there is a `sent_unconfirmed` attempt)
```
⚠ Resuming: dispute PP-R-DEMO-0000001 had a send attempt that was not confirmed. Checking PayPal
  before allowing new actions on this dispute...   [checking...]
```

## 8. The pilot's addendum (sections A–D) applied to this design

- **§A (idempotency):** the attempt registry is abstracted behind an `AttemptStore` interface (`claim`, `read`, `write`, all the operations `pilot/step5_send_evidence.py` already has). The pilot's file implementation (`O_CREAT|O_EXCL`) stays as a reference; this version adds a SQLite implementation (a `BEGIN IMMEDIATE` transaction + `UNIQUE(dispute_id, package_sha256)` for the atomic claim, a single uvicorn worker). **It is a storage change, not "no changes"**: the pilot's concurrency/crash tests (2 threads, 2 processes, crash between POST and GET, crash before the POST, ambiguous restart) are run again as they are against the SQLite implementation before it is used — the pilot's tests on JSON do not vouch for the new version. Recovery at startup remains an explicit design requirement (§4).
- **§B (provenance):** the `claims` table carries `basis` per row; the review screen (§7.3) shows it and "Details" exposes `effective_next_step` vs. the model's recommendation, never hiding the difference.
- **§C (approval by fingerprint):** `approvals.approval_sha256` reuses `approval_fingerprint` unchanged; screen 7.4 is the single control point where it is computed and shown.
- **§D (privacy):** **a publication requirement, not one for a functional v1** — before publishing the repo with this app, our internal privacy inventory is updated to cover what this version adds that is new (screenshots for the video/demo, any test document uploaded to `data/copilot.db`, which is already in `.gitignore`). The app itself never records real data: all demo documents are synthetic DEMO DATA, as in the pilot. `tools/scan_staged.py` (fail-closed) stays active in the hook for any commit of this code.

## 9. Out of v1, and what this release added
v1 designed none of these: messages to the buyer, refund offers, accepting a claim, escalation, webhooks (polling by design; see README, limits). This release adds, each behind a feature setting and under the same approval rule as the evidence package (the exact request shown, a fingerprint bound to its content, one send only after approval, a read-back from PayPal): an offer or a message to the buyer on the resolution page, a message draft page, tracking registration at a sale before any dispute, and the saved delivery facts of that registration (`copilot/features/resolution`, `draft`, `fulfilment`, `delivery_facts`; the pages are listed in `docs/JUDGE-RUN.md`). Accepting a claim is added as the seller's last option under the same rule (`copilot/features/accept_claim`); escalation remains out of the seller's flow: the app as shipped does not call it there. The appeal call is on: it appears only when PayPal's fresh read of the dispute offers an appeal, and it is sent once after the seller approves the exact request. It has not been exercised against PayPal, because no sandbox dispute of ours has offered an appeal. Webhooks are off in the local app; the hosted copy's Blueprint switches the receiver and its verification on, pending the owner's registration of its address (see README, limits).

## 10. Design review answers (approved with conditions, 2026-10-04)
1. `copilot/` lives at the repo root, next to `pilot/`.
2. `qwen3:14b` selectable by environment variable; default `qwen3.5:9b`; model and digest are stored in each package (`packages.model`, a new column in §5).
3. UI validation captures go to `data/ui-captures/` (git-ignored); only those reviewed for privacy are copied to `docs/media/` before publication.

## 11. Review conditions (to apply when coding, they do not reopen the design)
1. **AttemptStore is a real change, not "no changes"** (already corrected in §2/§4 above): the pilot's concurrency/crash tests are repeated as they are against the SQLite implementation before it is accepted.
2. **Attachments:** if the seller attaches files to the send, `approval_fingerprint` must include the sha256 of each attachment (extend the function with tests if it does not cover them today — it does not today, the pilot never attaches files) and screen 7.4 lists them. Type/size are validated against PayPal's documented limits (`.jpg .jpeg .gif .png .pdf`, <10 MB/file, ≤50 MB/call; developer.paypal.com, "Supported file types and sizes", read 2026-10-16) before approval is allowed.
3. **Human override of G16** ("Mark as seller statement and continue"): an explicit decision by the seller only, never triggerable by the model; it is recorded in `audit_log`; the approval screen (7.4) shows the warning; the claim is sent marked `seller_statement`, never as verified.
4. **Seller-facing language:** no "G16"/"Enforced by code"/"validator"/raw states in the main view (7.3 is rewritten in plain language when coding, e.g. "We can't submit yet: your tracking record doesn't show the package was delivered." / "Waiting for your response"); the technical content stays in "Details".
5. **Deadlines:** only if PayPal provides them; otherwise the literal "No verified deadline available", never a generic calculation.
6. **Local security:** bind to `127.0.0.1` only; CSRF on POST forms; upload size limits; sanitized file names; no secret in templates or logs.
7. **Traceability:** every call to PayPal from the app still goes through `SandboxClient` (sanitized JSONL evidence), with the running commit recorded.

With this approved, coding of `copilot/` began with `AttemptStore` (condition 1, the basis of everything else). The pre-send review (the exact content shown to the seller and approved before the first real send from the app) applies to every send, including the write pages this release added (section 9).
