# AI usage

Status: **Shipped with the v1.1.1 cut. The evaluation updates dated 2026-10-19 and 2026-10-20 are from an earlier kit; they are not figures of this cut.** First written 2026-10-04; draft checks and section 3.8 added 2026-10-13; model card (3.0) and the six failures listed one by one (3.3) added 2026-10-16; section 1 labels aligned to the interface on 2026-10-17; the strings quoted in section 1 were re-checked against the merged code on 2026-10-18 (every quoted label was found in the templates or the code). Every number below is copied from a file in this repository or from a private record named next to it; the
source is cited next to it. Nothing here claims reliability, accuracy in production, or time saved.

## 1. AI inside the product

### What the local model does
When you turn it on, a local open-weight model runs through Ollama; without it, Proofie runs in manual mode, up to and including the send, and nothing below is required to use the app. The default is `qwen3.5:9b`, selectable with `COPILOT_MODEL`; it is called with `think: false`, a JSON schema and a short `keep_alive`. Sources: `pilot/step4_evidence_ai.py`, `copilot/core/evidence_model.py`, and our recorded model decision of 2026-10-04 (kept internal). It does four things only:

1. **Extract** candidate facts from the text of documents the seller uploaded: carrier, tracking number and dates.
2. **Draft** the wording of the evidence notes and a *suggested* next step.
3. **Read images** (vision, `copilot/core/vision_model.py`). From a screenshot or scan it suggests what it literally sees about tracking, carrier, delivery and destination.
   - Each reading is a claim with basis `model_interpretation`, labelled "Suggested — check against the source".
   - The model is told never to read amounts. An independent code filter also drops any suggestion that mentions money.
4. **Draft buyer-message text** (the draft page, `/disputes/<id>/draft`, feature flag on in this release). When the seller presses "Ask the local assistant for a draft (English and Spanish)", it asks the local model for two short texts, one in English and one in Spanish, built only from facts the code lists.
   - The message box comes first and starts as the fixed template, written by code. The model button is secondary, and AI text is never prefilled: it is labelled "Written by AI (local model)", is text to review, and enters the box only when the seller chooses "Use this AI text as my starting point". Nothing is sent from that page; a send happens only after the seller approves the exact request on the resolution page.
   - The prompt asks the model to end the text with the sentence that the buyer can reply on the dispute and the seller will look into it right away; it promises no refund. Code refuses drafts containing refund wording and bounded lexical echoes of free-text facts, in addition to the existing number, date and tracking checks. These checks do not establish resistance to arbitrary injection; the seller reviews every draft. No reliability claim.

If you use the model: before any model call, the app checks that the GPU and the model are free (`preflight()`), and when the check fails the app continues in manual mode. Zero paid-API budget: nothing leaves the machine except the PayPal sandbox calls (and, if you connect the optional MCP server, what its tools return to your MCP client; see MCP.md).

### What code does (the model is never the source of truth)
Source of truth per data item is in `docs/ARCHITECTURE.md` section 3. In short:

- **Numbers, dates and IDs are validated by code against the literal source text** (`validate()`). A tracking number or date the model writes must appear in the cited source.
- **Every claim carries a source and a `basis`**: `verified`, `seller_statement`, `buyer_statement` or `model_interpretation`.
- **Delivery claims need a source that says so** (`find_unsupported_delivery_assertions()` in `copilot/core/evidence_model.py`). Any sentence asserting delivery, a signature or receipt must cite a source that contains it. Otherwise that sentence is blocked, not the whole package.
  - The rule applies the same way whether the model or the seller wrote it.
  - An honest "shipped, in transit" answer is never blocked.
  - A document whose tracking number or destination conflicts with PayPal's data can never back a delivery claim.
- **Missing proof of delivery is a warning, not a block.** For "item not received", the code lowers the model's "submit" recommendation and the seller sees why. The code can lower that recommendation, never raise it.
- **Checklist against PayPal data and published Seller Protection rules.** The checklist has five states: `verified`, `missing`, `present_unverifiable`, `conflict` and `not_applicable`. It lives in `copilot/core/checklist.py`, with the rule sets in `copilot/rules/` (Mexico 2025-01-13, United States 2026-01-26).
  - Every check states its basis: case request, published policy (with page or section) or Proofie's own quality rule.
  - The checks are deterministic, with no LLM.
- **Dispute and payment state come only from PayPal responses**, with a GET after every write, never from model text.
- **Money is integer cents, converted to string by code.**

### Prompt-injection handling
Documents are untrusted data. The system prompt tells the model to treat their text as data, and code checks the output afterwards (`injection_not_followed`). The model has no tools and cannot call PayPal, approve or send, so an injected instruction has no tool to call; it can only change the text the model writes, which code checks and the seller reviews. A detected injection appears on the review screen as a flag, and approval then requires an explicit note from the seller (route-level test in `tests/local/`). See results in section 3.

### The model never approves or sends
Approval is a human action bound to a SHA-256 fingerprint. The fingerprint covers the dispute id, the action, the exact content and every attachment's hash, so any edit invalidates it.
- For evidence, sending is a separate human click; on the offer, message and tracking check pages the approval of the exact request is the send, and it goes out once. The app records the attempt before the network call; for a dispute it also re-reads the dispute with a GET just before sending.
- "Received by PayPal" is shown only after PayPal's own `evidences[]` and `documents[]` show the content (attached files are matched by name and by how many times each name was attached).
- There is no automatic retry. An ambiguous outcome is shown as "Send attempted - not confirmed by PayPal" (on the result page; a send refused because another send for the dispute is still unconfirmed also says so there and tells you to use Check again first), and checking again only reads from PayPal.
- The background auto-preparation of new "item not received" disputes drafts only. A dedicated test checks that it never approves or sends.

### The ten local pages and the assistant
The ten pages listed in the README under "Ten more local pages, on by default" make no model call. Nine of them make no PayPal call either. The local autopilot page makes one read-only GET of the dispute's current snapshot from the PayPal sandbox when a simulation is prepared; nothing is written to PayPal or sent. The one part that faces an assistant is the separate stdio server behind the local MCP contracts page. It has three tools (`list_local_cases`, `read_local_case` and `prepare_local_note`), it serves only the cases that a local allowlist assigns to its configured actor, and it registers no send, approve or accept-claim tool. A note it prepares is saved locally with the status "prepared locally; not sent". The hosted demo has that page off.

### Manual mode
If Ollama is absent, fails, or extraction fails, the seller types the shipment details in a form that feeds the same checklist. The original document is kept. The interface shows: "The AI assistant is not available right now. Continuing in manual mode - type the values yourself, or try again in a moment." The model never fills in data.

A document the app cannot read (an image without vision or an image-only PDF) is reported as "Proofie could not read the text in <file>", and a field like "NULL" as "Part of a document could not be read". Neither is reported as missing.
### Privacy by contract
Under the PayPal Developer Agreement (last updated 2024-11-18), section 11.1, information received through an API call is PayPal's Confidential Information (section 11.1 (i)). When used, the AI step runs locally through Ollama (manual mode has no AI step); the evaluation below was run on sandbox data; and this repository publishes no PayPal response bodies: the evidence index carries hashes, line counts and our own descriptions instead.

## 2. AI in development

This project was built with two AI coding agents, **Claude Code** (Anthropic) and **Codex** (OpenAI). Roles, disclosed plainly:

- **Opus** sessions acted as supervisor/reviewer: wrote frozen briefs, verified deliveries, recorded decisions and integrated branches (these process records are kept internal and are not part of the published repository).
- **Sonnet** sessions acted as executors: wrote code, tests and documents (including this one) and ran scripts, within the briefs and gates.
- **Codex** sessions planned and supervised two parallel development lanes from briefs written by the Claude product session, implemented feature rounds on separate branches, verified each round in a session that had not written it, and cut the earlier release kits; later kits were exported and checked by Sonnet sessions supervised by the Claude product session, one exporting and one verifying independently. A round reached the main branch only after that verification; a Claude session then verified the merged result again on a fresh copy. The send-once guards and the interface round of 2026-10-17 were built mostly by Claude Sonnet sessions, with integration fixes by the Claude product session, and the kit was exported by a Sonnet session; the Claude product session supervised and integrated the work, independent Claude review agents verified it, and the product session checked the kit. No Codex session worked on that round. The round of 2026-10-18 was built by Claude Sonnet sessions, one per area of the code and documents, with integration fixes by the Claude product session; independent Claude review agents reviewed each area before the product session merged it, a Claude agent read every rendered page state for plain language, and separate Claude sessions exported the kit and ran the test suite with the clock set ahead to catch dates that go stale. No Codex session worked on that round either.
- **Jonás (the owner)** decided scope, approved every external or irreversible action, opened the early sandbox disputes by hand (later disputes come from the one-command card route, `tools/sandbox_card.py`),
  and is the person who enters passwords and publishes; no AI session does either.
- **Demo video narration.** The narration of the demo video is an AI-generated clone of the author's voice, made with ElevenLabs from the author's script. It is not a live recording.

Working rules that constrain the AI sessions are kept in our internal instructions file (no secrets in output, evidence appended to JSONL,
payment states only from PayPal, no push or publish without approval). AI-written code is not trusted by default:
it was checked by tests, by the secret scanner (`tools/scan_staged.py`) and by supervisor review, and AI-session errors
were recorded rather than hidden (for example the validator bug in section 3 and the byte-count slip in our environment notes).

## 3. Bounded evaluation of the local model (as reported)

Source of all numbers: evidence EV-08 (closing report, section 2) and EV-07 (pilot log) in `EVIDENCE-INDEX.md`, and `pilot/eval/EXPOSURE.md`.
Cases: `pilot/eval/cases/` (16 synthetic "DEMO DATA" cases = 8 types x dev/heldout). `FROZEN.json` holds a hash for each of the 8 heldout files (the 8 dev files have none) and `FROZEN_ROUND2.json` one for each of the 6 round-2 files. These hashes do not attest the exact text the runs used: on 2026-10-05 a real sandbox id in the case text was replaced by a synthetic one and the hashes were then recomputed (with line endings normalised). The 8 original heldout cases are a baseline, not a clean test: they were viewed case by case in the first run, then used to diagnose a validator bug, and a later diagnosis printed which heldout cases disagreed with the expected action (`pilot/eval/EXPOSURE.md`). Timeline: the 48 runs were made on the evening of 2026-10-04 (Mexico City time), on the prompt and checks of that evening. One clarification was added to the system prompt right after them (`tracking_info` stays empty when no tracking number can be read; section 3.2), and the checks around the model changed afterwards (claim-level delivery rules on 2026-10-04, the injection phrase list on 2026-10-05). The six-case round of section 3.7 ran on 2026-10-05, and EV-07 and EV-08 were recorded on 2026-10-05. The full historical series was not re-run, so its figures describe the prompt and checks as they were then; the two later single runs inside rehearsals are EV-19 and EV-20. Section 3.10 reports a separate 2026-10-20 diagnostic of the six former failures.
Metric `validation_ok` = the output passed all production validator checks (structure, literal grounding of facts, tracking grounded, injection not followed).
It measures validator-level soundness, **not** whether the package would win a dispute.

### 3.0 Model card (one page)

| | |
|---|---|
| Model | `qwen3.5:9b` through Ollama, 9.0B parameters, Q4_K_M quantisation, 6,550,825,550 bytes on disk (about 6.6 GB), as Ollama lists it locally (read 2026-10-16). It is the default; `COPILOT_MODEL` selects another. |
| Role | Assistant only: it extracts candidate facts, drafts wording, reads images and drafts buyer-message text (section 1). Code checks every number, date and id; a person approves every send; human review is mandatory. It has no tools and cannot call PayPal, approve or send. |
| Dated measurements | Evaluation v2 baseline, 2026-10-19 (section 3.9): held-out **36/72 = 50%**, Wilson 95% [38.75%, 61.25%]. After the prompt change, W21 on 2026-10-20 (section 3.10): **54/72 = 75%**, [63.91%, 83.56%]; validators 64/72, right action 58/72. The same 24 frozen cases and three seeds; 18 failures remain, including four W21 regressions. On contradictory documents, all 9 W21 outputs validate but 6 wrongly recommend sending. A separate diagnostic of six historical failures now passes 6/6; it is not pooled with v2. A distinct candidate-specific cut72 measurement is reported in section 3.11. Baseline, W21 and cut72 remain separate; human review remains mandatory. |
| Memory | Peak 7,897 MiB of the 12,227 MiB of the development GPU (RTX 5070), section 3.6. |
| Speed | Median 6,294 ms, 95th percentile 15,600 ms over 64 evaluation runs (section 3.6). Two measured runs inside end-to-end rehearsals (EV-19, EV-20): 2026-10-15, 84.6 s wall time, of which 61.7 s was loading the model from cold, and the output passed the checks; 2026-10-16, 59.5 s wall time, of which 53.6 s was loading, and the output did not pass the checks (it failed one check: an evidence item rested only on the model's own interpretation). How much of the wait is loading depends on whether the model is already in memory. Two runs, not an average. |
| Not measured | Real scanned PDFs and photographs at case level; any production dispute; whether a package wins; any model other than `qwen3.5:9b` and the comparison model `qwen3:14b` (smaller `qwen3.5` variants were run only to measure memory and speed, not on the cases). |
| Verdict | Below our own reliability bar (section 3.6): measured, not promised. Without the model, manual mode does the whole flow. |

### 3.1 Overall (after the validator bug fix, GPU runs, seeds 11/22/33, 16 cases each)

| Model | Runs | validation_ok | Wilson 95 % CI |
|---|---|---|---|
| `qwen3:14b` | 48 | 28/48 = 58 % | [44 %, 71 %] |
| `qwen3.5:9b` (text-only) | 48 | 42/48 = 88 % | [75 %, 94 %] |

`qwen3.5:9b` by set: dev 23/24 = 96 % [80 %, 99 %]; heldout 19/24 = 79 % [60 %, 91 %].

### 3.2 Per case type (validation_ok)

The `qwen3.5:9b` column is the single seed-11 run made after the validator fix (15/16, EV-07); the three-seed series of section 3.1 is the one the model card quotes.

| Type | `qwen3:14b` (3 seeds, 6 runs) | `qwen3.5:9b` seed 11 only (2 runs) |
|---|---|---|
| sufficient_coherent | 6/6 | 2/2 |
| contradictory | 6/6 | 2/2 |
| injection | 6/6 | 2/2 |
| incomplete | 3/6 | 1/2 |
| irrelevant_docs | 3/6 | 2/2 |
| unsupported_claim | 3/6 | 2/2 |
| no_tracking | 1/6 | 2/2 |
| illegible | 0/6 (a later one-sentence clarification in the system prompt partly fixed it; 1/1 on a single re-run of `illegible_dev`) | 2/2 |

### 3.3 Per case, `qwen3.5:9b` over 3 seeds
All cases passed 3/3 **except**: `contradictory_heldout` 1/3 (dates rewritten in a format no document uses, e.g. "Oct 1", and once an item backed only by the model's own interpretation),
`incomplete_heldout` 0/3 (model converted "shipped yesterday" into a date; date arithmetic is forbidden and the validator caught it),
`no_tracking_dev` 2/3. (EV-07, the qwen3.5:9b repetitions.)

The six historical failures, one by one, with the validator check that caught each (our reading of the private log, EV-07). A separate 2026-10-20 diagnostic of these six case/seed combinations is in section 3.10; the original series below remains unchanged:

| Case | Seed | Check that failed | What the model did |
|---|---|---|---|
| `incomplete_heldout` | 11 | claims grounded in the cited source | turned "shipped yesterday" in the buyer chat into a calendar date and cited the chat for it |
| `incomplete_heldout` | 22 | claims grounded in the cited source | the same |
| `incomplete_heldout` | 33 | claims grounded in the cited source | the same |
| `contradictory_heldout` | 22 | evidence item has non-interpretation support; text facts literal in the sources | proposed an evidence item backed only by its own interpretation, and wrote an order reference into its reviewer note that no document contains |
| `contradictory_heldout` | 33 | text facts literal in the sources | rewrote the documents' dates as "Oct 1", "Oct 2", "Sept 28" in its reviewer note, a format no document uses |
| `no_tracking_dev` | 33 | claims grounded in the cited source | cited the order confirmation for an order reference that is not written in it |

In each case the production validator refused the output. In the app, such an answer is shown as "The assistant's answer could not be checked" and you continue by hand.
The complete per-case x per-seed records remain in the private evaluation log (EV-07 in `EVIDENCE-INDEX.md`, generated in the public export). The public document reports the summary above; raw per-run records are not attached because the evidence contains sandbox identifiers or data. The index records provenance and hashes, not the private contents. The figures in this historical table describe the prompt and checks as they were then (see the start of this section). The full series was not re-run; section 3.10 reports the separate 2026-10-20 diagnostic of these six former failures.

### 3.4 Injection resistance
12/12 injection-case runs clean (6 `qwen3:14b` + 6 `qwen3.5:9b`, on the injection dev case and the injection heldout case), 0 injections obeyed, using the production check `injection_not_followed`.
Zero observed failures does not prove absence of risk.

### 3.5 Recommendation vs expected action
Before the code rule G16 (the sufficiency ceiling on the recommended step), the model's recommended step matched the expected action in 12/28 (`qwen3:14b`) and 4/15
(`qwen3.5:9b`, one seed) of the runs that validated. The review-flag rule caught 25 of 27 mismatches; the 2 silent ones
motivated G16. After G16, replaying 43 stored valid runs lowered 20/43 recommendations from "submit" to "need more information" (12/27 dev, 8/16 heldout).
In plain words: the model often over-recommends sending, and code is what holds it back.

### 3.6 Honest limits
- **n is small.** 16 cases, 3 seeds; per-type cells have 2-6 runs. Confidence intervals are wide.
- **Heldout exposure.** The 8 original heldout cases were run and seen case by case, and later used to diagnose a deterministic validator bug
  (`_TRACKING_CONTEXT`); details and the rule that follows are in `pilot/eval/EXPOSURE.md`. They are therefore a baseline, not a clean test. The hashes in `FROZEN.json` and `FROZEN_ROUND2.json` were recomputed on 2026-10-05 after a real sandbox id in the case text was replaced by a synthetic one, so they do not attest the exact text the series ran on.
  Six replacement heldout cases (`*_heldout2`, `pilot/eval/cases/FROZEN_ROUND2.json`) were run once at the end; results are in section 3.7.
- **Bug disclosed.** A validator bug of ours (`_TRACKING_CONTEXT`) made early numbers look worse. The same composition, run before and after the fix: `qwen3:14b` 7/48 = 15 % then 28/48 = 58 %; `qwen3.5:9b` in text-only mode, seed 11 only, 8/16 = 50 % then 15/16 = 94 % (EV-07). The 3-seed series of section 3.1 (42/48 = 88 %) and every number above are post-fix.
- **Neither model meets the reliability bar** set by us (>=90 % validation to propose a default change; `qwen3.5:9b` is at 88 %, heldout 79 %). The
  default was chosen on the other evidence in our internal decision record (smaller, no degenerate outputs, adds vision) and it is documented as **assistant only, human review mandatory**.
- **Synthetic text only.** Real scanned PDFs and photographs of documents were not evaluated at case level (listed as gaps in EV-08, section 4: one partly addressed, one open);
  the vision check was a synthetic receipt (amount field 0/5 because of our own schema, other fields 5/5).
- **Latency/VRAM** (EV-08, section 2.4): median 6294 ms, p95 15600 ms over 64 runs; `qwen3.5:9b` peak 7897 MiB of 12227 MiB on the development GPU (RTX 5070).
- **Earlier pilot on a real sandbox dispute:** 6/7 packages validated and the injection was not obeyed in 9/9, n = 7, no reliability claim (EV-07).
- Everything is sandbox/synthetic. No production dispute was ever processed.

### 3.7 Final clean run: round-2 heldout (2026-10-05)
Six heldout cases never seen before (hashed in `FROZEN_ROUND2.json`, recomputed on 2026-10-05 as described in section 3.6) were run **once** with `qwen3.5:9b` (seed 7, GPU pre-checked, nothing tuned afterwards).
Sources: evidence EV-06 (the raw per-case output of this run) and EV-05 (the run log) in `EVIDENCE-INDEX.md`; both stay private because they contain PayPal sandbox identifiers or data, and the table above is our summary of them.

| Dimension | Result |
|---|---|
| Conflicts and gaps detected | 6/6 |
| Injected instructions obeyed | 0/6 |
| Dangerous invented facts / unsupported delivery claims | 0/6 |
| Recommended action matches the expected action | **4/6** |

**Failures:** `incomplete_heldout2` and `contradictory_heldout2`. In both, the model correctly reported the conflict (different address, different amount) but still recommended sending. This is a model failure.

**Product-level mitigation (separate from the model verdict):** in the app, any checklist item in `conflict` blocks approval until the seller explicitly corrects the value, removes the document or explains the difference (tested end to end in `tests/local/test_integral_review.py`, `test_s2_a_document_from_another_order_conflicts_and_blocks_approval_until_decided`, and `tests/local/test_routes_package.py`, `test_c03_other_order_conflict_gating_end_to_end`). So this model error does not by itself lead to a send: a conflict blocks approval until a person decides what to do about it. It does not change the model's score.

In two cases the automatic forbidden-term check fired on substrings that are not inventions ("DEMO" inside an allowed order number; "tracking" in a sentence denying it exists). Our pilot log (EV-07) keeps the automatic verdict and the manual reading in separate columns.

n = 6, single run: no reliability claim.

### 3.8 Draft page: separate series with a code guard
A separate series used five labelled synthetic cases (four distinct fact-list prompts) with seeds 11/22/33: 10 of 15 drafts passed all checks; 5 were refused by the code guard and are not valid answers. The earlier 48-attempt series remains 42/48 on 16 labels / 15 distinct prompts. `pilot/eval/draft_cases/FROZEN.json` lists the whole case set, 17 files; this series used five of them. These bounded lexical observations do not demonstrate reliability or resistance to arbitrary injection; human review remains mandatory.

### 3.9 Evaluation v2: 40 new synthetic cases, three runs each, scored by code, beside a rules-only baseline (2026-10-19)
Re-runnable from the kit: `pilot/eval_v2.py` (the runner and scorer, this series' only driver), the cases in `pilot/eval/v2/cases/`, the public results in `pilot/eval/v2/results/` (scores and hashes for every run; the model's raw text stays in the private series log), the command in `docs/JUDGE-RUN.md` section 5. The series of sections 3.1-3.7 stays as it was reported; this one does not replace it

The draft step's held-out set can be re-scored with `python tools/eval_draft.py --heldout` (recorded outputs in `pilot/eval/draft_cases/recorded/`, synthetic cases only). Optional: if a local model already serves at 127.0.0.1:11434, the evaluation uses it; otherwise it replays the recorded model outputs, and no model runs. The judge produces the numbers: the report lists every failure with its manual-fallback mark., and the two are not pooled.

- **Cases.** 40 synthetic "DEMO" cases, 8 types x 5 (16 dev, 24 held-out), "item not received" disputes, English documents with US carriers and Spanish documents with Mexican carriers (FedEx, UPS, USPS, DHL, Estafeta, Redpack). The 24 held-out cases were frozen (`pilot/eval/v2/cases/FROZEN.json`) before any run, and nothing was tuned on them. Every case carries an `expected` block written before the runs, scored by code: the action after the code's own sufficiency ceiling, each expected missing item and conflict (any of its listed words, in the model's own lists), forbidden assertions (in what the output asserts: claims, notes, tracking numbers), the tracking number a submission must cite, and the injection check.
- **Run.** `qwen3.5:9b` through Ollama, `think` off, temperature 0.4, an 8,192-token context, seeds 11, 22 and 33: 120 calls on 2026-10-19 between 22:56 and 23:12 (Mexico City time), on the prompt, schema and validators the app ships, with a free-GPU check before each seed. Prompts were 840 tokens at the median (947 at most), outputs 484 (728 at most); every call ended normally. Wall time 6,330 ms at the median, 8,400 ms at the 95th percentile; three calls included a cold model load of up to 44.6 s. With the model loaded, the development GPU (RTX 5070, 12,227 MiB) showed about 7,870 MiB in use in total.

| Arm | Runs | Passed every validator check | Right action | Case passed (everything above) |
|---|---|---|---|---|
| `qwen3.5:9b`, dev | 48 | 39/48 = 81 % [68 %, 90 %] | 37/48 = 77 % [63 %, 87 %] | 34/48 = 71 % [57 %, 82 %] |
| `qwen3.5:9b`, held-out | 72 | 52/72 = 72 % [61 %, 81 %] | 45/72 = 62 % [51 %, 73 %] | **36/72 = 50 % [39 %, 61 %]** |
| `qwen3.5:9b`, all | 120 | 91/120 = 76 % [67 %, 83 %] | 82/120 = 68 % [60 %, 76 %] | 70/120 = 58 % [49 %, 67 %] |
| Rules only (no model), all | 40 | n/a | 37/40 = 92 % [80 %, 97 %] | 36/40 = 90 % [77 %, 96 %] |

Brackets are Wilson 95 % intervals. Source: `pilot/eval/v2/results/EVAL-V2-QWEN35-9B.json` and `baseline-rules-only.json`.

| Case type (5 cases) | Model: validators | Model: right action | Model: case passed | Rules only: case passed |
|---|---|---|---|---|
| `sufficient_coherent` | 15/15 | 15/15 | 15/15 | 4/5 |
| `incomplete` | 12/15 | 12/15 | 4/15 | 5/5 |
| `no_tracking` | 12/15 | 12/15 | 11/15 | 4/5 |
| `contradictory` | 10/15 | 1/15 | 1/15 | 5/5 |
| `irrelevant_docs` | 10/15 | 10/15 | 9/15 | 4/5 |
| `illegible` | 11/15 | 11/15 | 11/15 | 5/5 |
| `injection` | 10/15 | 10/15 | 8/15 | 4/5 |
| `unsupported_claim` | 11/15 | 11/15 | 11/15 | 5/5 |

What the numbers say, failures kept:
- **Contradictions are the model's weak point.** On documents that contradict each other but still show a delivery, the model recommended sending in 14 of 15 runs; in 7 of the 10 outputs that passed the validators it had itself listed the conflict. The code's sufficiency ceiling does not stop this, because each document on its own proves a delivery; the seller's approval screen shows the conflict the model reported. The same pattern appeared in the earlier round (section 3.7).
- **On the "ask the seller" types the right action comes from code, not from the model.** In 57 of the 120 runs the model recommended sending and the code's sufficiency ceiling lowered the recommendation to asking the seller, because no cited tracking source showed a delivery with a date: 12 runs on `incomplete`, 12 on `no_tracking`, 11 on `illegible`, 11 on `unsupported_claim`, 10 on `irrelevant_docs`, 1 on `contradictory`. The "right action" column counts the action after that ceiling, which is what the seller sees.
- **Incomplete evidence:** the right action in 12 of 15 runs, all through the ceiling; the model named the missing delivery confirmation in only 4.
- **Injection:** no output that passed the validators followed an injected instruction. Five outputs that proposed what the injected text asked were refused by the validators and would never reach the seller as a package. The scorer is strict: two of its three "forbidden assertion" failures are outputs that reported the buyer's injected sentence as a fact ("Buyer attempted to instruct seller to accept the claim"), not outputs that followed it; they are counted as failures as written. The third cited a supplier's tracking number from a different shipment as the seller's.
- **Held-out is lower than dev** (50 % against 71 % of cases passed), which is why the held-out row is the one quoted.
- **The rules-only arm scores higher on these cases, and it is favoured:** the same session wrote its rules and the 40 cases, so the cases follow the labelled layouts its field extraction reads; real documents would not. It has one known defect, kept: it reads "No tracking purchased" as the tracking number "purchased" (`no_tracking_dev1`). It makes no claims of its own beyond a tracking number it found literally, and it cannot read free-form text the way the model can. The comparison says that on tidy, labelled documents a few rules already decide the action; it does not say the rules would do as well on real documents.
- **Limits.** 40 synthetic cases, three seeds, one model, one GPU; per-type cells hold 15 runs. The expectations and the scorer were written by the same session as the cases. Nothing here measures whether a package would win a dispute, and nothing here is called reliable: a person reviews and approves every send, and manual mode works without the model.


### 3.10 After the prompt change: held-out measurement on 2026-10-20 (not the final cut figure)

The change asks the model to identify missing evidence and conflicts before proposing evidence, to abstain on contradictions or incomplete delivery proof, and to avoid rewriting literal facts. Schema property order changed; schema content, validator functions, patterns, constants, sufficiency ceiling and application checks stayed unchanged, pinned by `pilot/test_step4_frozen_checks.py`. Code measured: `9ce51d6bd36320101443472e81dfd546a4fd8fd2`. Tuning used the DEV split only; its tuning score is not presented as a public performance figure. No tuning followed the held-out measurement.

One series measured the same 24 frozen held-out cases with seeds 11/22/33: 72 calls, `qwen3.5:9b`, temperature 0.4, `think: false`, context 8192. GPU availability was checked before starting and by the runner before each seed. Each case/seed combination ran once; no failure was repeated to obtain a pass. Case hashes match the baseline. Results contain scores and hashes; raw model text and private evidence paths are not published.

| Held-out metric | Before (2026-10-19) | After (2026-10-20) |
|---|---|---|
| All case checks passed | 36/72 = 50.0% [38.75%, 61.25%] | **54/72 = 75.0% [63.91%, 83.56%]** |
| Every validator passed | 52/72 = 72.2% [60.95%, 81.24%] | 64/72 = 88.9% [79.58%, 94.26%] |
| Right action after the code's sufficiency ceiling | 45/72 = 62.5% [50.95%, 72.78%] | 58/72 = 80.6% [69.97%, 88.05%] |

Brackets are Wilson 95% intervals over runs. Repeated seeds share synthetic cases; these are not independent-case intervals or evidence of performance on real disputes. Sources: `pilot/eval/v2/results/EVAL-V2-QWEN35-9B.json` (unchanged baseline), `EVAL-V2-W21-HELDOUT.json` (after) and `W21-MODEL-CARD.json` (comparison and validator hashes).

**Failures and regressions are retained.** Of the same 72 combinations, 22 changed from fail to pass, 4 from pass to fail, 32 still pass and 14 still fail. The 18 after-failures are listed by case, seed and reason in the after-result JSON and the comparison card. The four regressions are `injection_heldout3` seed 11, `irrelevant_docs_heldout2` seed 11, `irrelevant_docs_heldout3` seed 33 and `no_tracking_heldout2` seed 33.

**Contradictions remain a substantial weakness.** Within held-out, case passes rose from 0/9 to 2/9 and correct actions from 0/9 to 3/9. All 9 after-outputs passed the validators, yet 6 wrongly recommended sending. Passing validators therefore does not establish that sending is appropriate. The earlier 1/15 contradiction figure in section 3.9 includes DEV and must not be compared directly with this held-out subset. Human review and approval remain mandatory; the model is not reliable.

The after-run has 8 validator refusals: literal facts (5), grounded claims (2), injection-following (1). No output accepted by the validators followed an injected instruction; the one that followed it was refused. This bounded observation does not establish resistance to arbitrary injection.

**Separate historical diagnostic, not pooled with v2.** None of the three historical inputs exactly matches a v2 input. After the v2 series, the six previously failing case/seed combinations were run once with the committed prompt, after another free-GPU check. All six passed the validators and returned `need_more_information`:

| Historical case | Seeds | Validators after change |
|---|---|---|
| `incomplete_heldout` | 11, 22, 33 | 3/3 |
| `contradictory_heldout` | 22, 33 | 2/2 |
| `no_tracking_dev` | 33 | 1/1 |

Source: `pilot/eval/v2/results/W21-HISTORICAL-SIX.json`. These cases were selected for past failures, so 6/6 is a diagnostic, not an unbiased benchmark. As section 3.6 records, identifiers in the historical cases were sanitized after the original series; today's frozen hashes do not attest its exact original input text. The six original failures remain reported in section 3.3.

This is the dated W21 development measurement, not the candidate-specific cut72 result in section 3.11. The rules-only baseline remains unchanged and is not pooled with either model series; its limitations are in section 3.9. These synthetic text results do not measure scanned documents, production disputes or whether evidence wins a dispute.

### 3.11 Candidate-specific held-out cut72 measurement (2026-10-21)

This separate held-out series used 24 frozen synthetic cases and seeds 11, 22 and 33 with `qwen3.5:9b` (72 runs). The source frozen for this measurement is commit `98d04875e42edf3df97e4563dc49736fdb274e57`, tree `edb989c508fd95fd68fe482b961d5e4e60f3622b`; this identifies the measurement source, not the final documentation/export source. The source scorer projection `heldout-public.json` (schema `proofie-eval-results/2`) has SHA-256 `CE62DE19558B18F60F72BA3F9326236966BFAB29290BBBE03D9E84CDF20052FF`; its 72 run rows and metrics are copied to `pilot/eval/v2/results/EVAL-V2-CUT72-HELDOUT.json` unchanged. The published `cases_frozen_sha256` identifies the LF `FROZEN.json` as shipped (`83d0f42c2b7faffd54815a796c9ff2977660f628553cda274bf5ce48d0a66cc8`), not the historical CRLF run input; `cases_hash_scope` says so, and the exact historical digest remains in private raw evidence. The run and score processes exited 0. After scoring, the outer launcher exited 1 while serializing final metadata because PowerShell rejected integer-key `seed_counts`; EV later reconciled that metadata without rerunning the model or scorer. These exits do not change the measured results. Run once more on 2026-10-24: the same 52 of 72, 62 validators, 55 expected actions, `injection_followed` 0; runner and scorer exit 0; re-scored byte-for-byte by a second AI session. Since the measured source above, the prompt, the response schema and checks (`pilot/step4_evidence_ai.py`, `copilot/core/evidence_model.py`, `copilot/core/vision_model.py`) and the frozen cases (`pilot/eval/cases/FROZEN.json`) have not changed. Two small edits came before the 2026-10-24 run (section 3.12): the field extraction the scorer imports (`copilot/core/field_extraction.py`) gained the USPS carrier name and a stricter capture-date pattern, and the scorer (`pilot/eval_v2.py`) gained a separate `checklist` command. The model series was not run again on the final cut. Receipts are private.

| Measure | Result |
|---|---:|
| Passed every scored check | 52/72 (72.2%); run-level Wilson 95% interval 60.95%–81.24% |
| Passed every validator | 62/72 |
| Chose expected action after code ceiling | 55/72 |
| Failed runs | 20 |
| Contradictory bucket: passed every scored check | 1/9 |
| Injection bucket: passed scored checks; `injection_followed` | 9/9; 0 |

The bucket figures do not mean that 1/9 contradictory outputs recommended sending, nor do they establish general injection resistance. The projection has no paired comparison to W21 or the original-prompt baseline, so this text makes no per-case improvement or regression claim. W21 (2026-10-20, 54/72, including 22 fail-to-pass and four regressions) and the original-prompt baseline (2026-10-19, 36/72) remain distinct historical series and are not pooled with this cut72 result. The interval is run-level across repeated seeds, not an independent-case interval or evidence of real-world reliability. A person reviews every send; this measurement makes no claim about dispute outcomes.

### 3.12 Deterministic checklist arms (frozen-set runs done)

The deterministic checklist arms (no model, no GPU) were run on the frozen set on 22 October 2026 and again on 24 October 2026, after the ten local pages were integrated. The two runs produced identical output. They show that the extraction and checklist code behaves the same on the frozen set, not that it is correct, and they are not a measure of model reliability.

On 21 October 2026, after a read-only review that had read the held-out cases, three extraction fixes were made: a trailing full stop is now kept out of tracking numbers, USPS was added to the carrier names, and the Spanish capture-date label no longer collides with 'ID de captura'. These fixes change only how documents are read into fields. The available records do not establish that model-alone figures were unaffected, that prompt hashes were equal before and after, or that a test pins that equality. Because the reviewer had seen the held-out cases, read the product-arm figure on the frozen set with that in mind: the fixes were made after that exposure, not before it.
