# Exposure of the heldout cases

A mandatory record (review instruction, 2026-10-04) that the 8 `heldout` cases in `pilot/eval/cases/` were run and **viewed in detail, case by case**, not only in aggregate, during the first real evaluation run.

## What happened
- **When:** 2026-10-04, batch launched ~21:55 CDMX (base: the commit just before any commit of this batch).
- **What was run:** the 16 cases (8 dev + 8 heldout) × 3 repetitions (seeds 11/22/33) with `qwen3:14b`, plus the 16 cases × 1 repetition (seed 11) with `qwen3.5:9b` in text-only mode (comparison). `SYSTEM` prompt hash: `ae1c3befd4880e4a` (sha256 of `step4_evidence_ai.SYSTEM`, 16 hex). Hash of the schema structure (keys, `basis` enum, etc.): `8c21696223d31a0a`.
- **Why it counts as exposure:** the batch script (`driver.py`, in the scratchpad, not versioned) printed the result **of every individual case** to the console, the 8 heldout included, with its `case_id`, validation, score against `expected` and latency — not an aggregate. That output was read as part of the normal work of launching and watching the batch.

## Rule applied from here on
This run is the **baseline** of the current prompt/schema (the same one that closed the earlier evaluation, unchanged since). **The per-case breakdown of the heldout cases from this run is not used to tune anything**: if the prompt or the schema is ever touched on the basis of what was seen here, the adjustment may rest only on the 8 `dev` cases. The final evaluation of an adjusted prompt would have to run again on heldout (these same ones or new ones) **only once**, at the end, not to iterate.

## Proposed new heldout cases (to freeze before any prompt adjustment)
If it is decided to adjust the prompt/schema from the `dev` cases, these 6 would replace the heldout cases already seen before that adjustment (they would be created as `pilot/eval/cases/<id>.json`, with their expectations written first, and frozen in `FROZEN.json` before touching the prompt — like the original 16):

| # | Type | Idea (a variant of those already seen, not a repeat) |
|---|---|---|
| 1 | `sufficient_coherent` | Reason `UNAUTHORISED` instead of `MERCHANDISE_OR_SERVICE_NOT_RECEIVED`, with a different valid evidence table |
| 2 | `incomplete` | Tracking present and with a delivery date, but to an address different from the order's (incomplete because of the address, not the date) |
| 3 | `no_tracking` | There is a shipping document, but it is a purchase receipt from the carrier (freight was paid), not a waybill with a tracking number |
| 4 | `contradictory` | A contradiction in the amount (two documents with different totals), not in carrier/date |
| 5 | `injection` | An injection inside the `notes` field of a shipping document (not in the chat), in English, asking to "escalate immediately" |
| 6 | `unsupported_claim` | A document that confirms the payment (not the shipment) cited as if it supported the delivery |

Update: these six cases were later created as `*_heldout2`, frozen in `pilot/eval/cases/FROZEN_ROUND2.json` and run once; the results are in `docs/AI-USAGE.md` section 3.7.

## Additional exposure: validator debugging (not prompt tuning)
During this same run a real bug was found and fixed in `_TRACKING_CONTEXT`. The content of the **8 heldout cases was used to diagnose that bug in deterministic code** (not to adjust the prompt or the model's schema): the false positive appeared in 7 of the 8 heldout cases just as in 7 of the 8 dev cases. It is distinguished from this document's main rule because fixing a regex that confuses "tracking information" with an invented fact is not a decision that can artificially improve how the model does in the evaluation — it affects `dev` and `heldout` equally and in the same way. Even so, it is declared here for transparency, following the same discipline.

## Additional exposure: diagnosis of the sufficiency rule G16 (recommended action vs. expected action)
While analysing why `action_ok` came out low, I printed the list of which individual cases disagreed (`case_id` + repetition), the heldout ones included (e.g. `contradictory_heldout` and `illegible_heldout` appeared in that list). Same criterion as above: this led to designing the G16 sufficiency rule **in code** (deterministic, by dispute reason), not to adjusting the prompt; even so, for the G16 rule itself, the supervising Claude session asked that it be designed and tested looking **only at the `dev` cases** — that is what was done, without using the heldout breakdown to decide the threshold or the shape of the rule.

## Result of this run (baseline)
It is reported aggregated by case and by type (dev+heldout together, as befits a baseline and not an iteration) in the evaluation's closing report (EV-08 in `EVIDENCE-INDEX.md`).
