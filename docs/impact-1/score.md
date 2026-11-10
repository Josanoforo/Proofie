# IMPACT-1 score (recomputed by code after every closed member)

Closed pairs (all members closed): **70**. Money in integer cents per currency, never converted. Sources: `closed.jsonl:<line>` rows (each lists its evidence file:line); `score.json` has the per-arm line lists.

Members closed: 161; resolved on PayPal's record: 154; scored by the rule only, no PayPal resolution on record: 7.
seller_protection.status on the captures (a fact of the record, 161 members): {"NOT_ELIGIBLE": 161}.

## MXN: 58 closed pairs, sold per arm MXN 313,601.14

The headline difference is amounts only (the DISPUTE_SETTLEMENT debits). Fees here are 'fees on PayPal's record, equal to the sale's PayPal fee': the SELLER debits of reason DISPUTE_SETTLEMENT_FEE on PayPal's record of each lost dispute, shown beside the headline, never inside it. PayPal Mexico's published chargeback fee (MXN 110.00 per lost case, FEES.md (a) MX merchant-fees page row "Peso mexicano | 110.00 MXN") is shown apart and never added.

| Arm | Kept | Lost | In | Fees on PayPal's record, equal to the sale's PayPal fee | Published fee (apart) | Exposed | Resolved on PayPal's record / by the rule only (no PayPal resolution on record) |
|---|---|---|---|---|---|---|---|
| The model alone, no checks (A) | MXN 127,641.14 | MXN 185,960.00 (37 seller debit on PayPal's record, 1 dispute amount) | 38 of 58 | MXN 7,955.80 (37 of 38 lost with a fee debit) | MXN 4,180.00 | MXN 123,741.40 | 57 / 1 |
| With Proofie's checks and her approval (B_follow) | MXN 296,643.90 | MXN 16,957.24 (6 seller debit on PayPal's record, 0 dispute amount) | 6 of 58 | MXN 804.46 (6 of 6 lost with a fee debit) | MXN 660.00 | MXN 16,957.24 | 56 / 2 |
| With Proofie's checks and her approval, hard stops only (beside the headline) (B_hard) | MXN 235,532.93 | MXN 78,068.21 (8 seller debit on PayPal's record, 2 dispute amount) | 10 of 58 | MXN 2,237.84 (8 of 10 lost with a fee debit) | MXN 1,100.00 | MXN 78,068.21 | 54 / 4 |

- **DIFF** (B_follow, amounts only, the headline) = kept with Proofie minus kept by the model alone = MXN 169,002.76; beside it, with fees on PayPal's record, equal to the sale's PayPal fee: (Kept_B - Fees_B) - (Kept_A - Fees_A) = MXN 176,154.10. A lost MXN 169,002.76 more (MXN 7,151.34); per 100 orders (floor): MXN 291,384.06 (MXN 12,329.89).
- **DIFF** (B_hard, amounts only, the headline) = kept with Proofie minus kept by the model alone = MXN 107,891.79; beside it, with fees on PayPal's record, equal to the sale's PayPal fee: (Kept_B - Fees_B) - (Kept_A - Fees_A) = MXN 113,609.75. A lost MXN 107,891.79 more (MXN 5,717.96); per 100 orders (floor): MXN 186,020.32 (MXN 9,858.55).

Misses by class (lost / n; amount lost):

| Class | A | B_follow | B_hard |
|---|---|---|---|
| C | 0/20 (MXN 0.00) | 0/20 (MXN 0.00) | 0/20 (MXN 0.00) |
| T | 8/8 (MXN 28,902.35) | 0/8 (MXN 0.00) | 0/8 (MXN 0.00) |
| D | 8/8 (MXN 13,272.84) | 0/8 (MXN 0.00) | 0/8 (MXN 0.00) |
| P | 7/7 (MXN 90,686.02) | 0/7 (MXN 0.00) | 4/7 (MXN 61,110.97) |
| G | 9/9 (MXN 36,141.55) | 0/9 (MXN 0.00) | 0/9 (MXN 0.00) |
| M | 6/6 (MXN 16,957.24) | 6/6 (MXN 16,957.24) | 6/6 (MXN 16,957.24) |

Off the expected outcome (kept, not re-run): IMP1-2226, IMP1-223, IMP1-224.

- A sources: closed.jsonl lines 175-310 (58 rows)
- B_follow sources: closed.jsonl lines 176-333 (58 rows)
- B_hard sources: closed.jsonl lines 176-333 (58 rows)

## USD: 12 closed pairs, sold per arm USD 4,557.37

The headline difference is amounts only (the DISPUTE_SETTLEMENT debits). Fees here are 'fees on PayPal's record, equal to the sale's PayPal fee': the SELLER debits of reason DISPUTE_SETTLEMENT_FEE on PayPal's record of each lost dispute, shown beside the headline, never inside it. PayPal Mexico's published chargeback fee (USD 10.00 per lost case, FEES.md (a2) MX merchant-fees page row "Dolar estadounidense | 10.00 USD") is shown apart and never added.

| Arm | Kept | Lost | In | Fees on PayPal's record, equal to the sale's PayPal fee | Published fee (apart) | Exposed | Resolved on PayPal's record / by the rule only (no PayPal resolution on record) |
|---|---|---|---|---|---|---|---|
| The model alone, no checks (A) | USD 442.14 | USD 4,115.23 (8 seller debit on PayPal's record, 0 dispute amount) | 8 of 12 | USD 191.28 (8 of 8 lost with a fee debit) | USD 80.00 | USD 3,002.39 | 12 / 0 |
| With Proofie's checks and her approval (B_follow) | USD 3,881.99 | USD 675.38 (2 seller debit on PayPal's record, 0 dispute amount) | 2 of 12 | USD 31.63 (2 of 2 lost with a fee debit) | USD 20.00 | USD 675.38 | 12 / 0 |
| With Proofie's checks and her approval, hard stops only (beside the headline) (B_hard) | USD 2,778.74 | USD 1,778.63 (2 seller debit on PayPal's record, 1 dispute amount) | 3 of 12 | USD 31.63 (2 of 3 lost with a fee debit) | USD 30.00 | USD 1,778.63 | 11 / 1 |

- **DIFF** (B_follow, amounts only, the headline) = kept with Proofie minus kept by the model alone = USD 3,439.85; beside it, with fees on PayPal's record, equal to the sale's PayPal fee: (Kept_B - Fees_B) - (Kept_A - Fees_A) = USD 3,599.50. A lost USD 3,439.85 more (USD 159.65); per 100 orders (floor): USD 28,665.41 (USD 1,330.41).
- **DIFF** (B_hard, amounts only, the headline) = kept with Proofie minus kept by the model alone = USD 2,336.60; beside it, with fees on PayPal's record, equal to the sale's PayPal fee: (Kept_B - Fees_B) - (Kept_A - Fees_A) = USD 2,496.25. A lost USD 2,336.60 more (USD 159.65); per 100 orders (floor): USD 19,471.66 (USD 1,330.41).

Misses by class (lost / n; amount lost):

| Class | A | B_follow | B_hard |
|---|---|---|---|
| C | 0/4 (USD 0.00) | 0/4 (USD 0.00) | 0/4 (USD 0.00) |
| T | 1/1 (USD 766.36) | 0/1 (USD 0.00) | 0/1 (USD 0.00) |
| D | 2/2 (USD 457.40) | 0/2 (USD 0.00) | 0/2 (USD 0.00) |
| P | 2/2 (USD 1,617.48) | 0/2 (USD 0.00) | 1/2 (USD 1,103.25) |
| G | 1/1 (USD 598.61) | 0/1 (USD 0.00) | 0/1 (USD 0.00) |
| M | 2/2 (USD 675.38) | 2/2 (USD 675.38) | 2/2 (USD 675.38) |

Off the expected outcome (kept, not re-run): IMP1-2224.

- A sources: closed.jsonl lines 246-289 (12 rows)
- B_follow sources: closed.jsonl lines 247-331 (12 rows)
- B_hard sources: closed.jsonl lines 247-331 (12 rows)

## Own line, never merged into the headline: resolved, but PayPal's record fits neither pattern: not scored (pairs W02-17)


Lost members whose record is not exactly one settlement debit equal to the dispute amount plus one fee debit (MXN): none.

Lost members whose record is not exactly one settlement debit equal to the dispute amount plus one fee debit (USD): none.

## Own line, never merged into the headline: PayPal's record differs from the rule's outcome (adjudicated by mistake): not scored (pairs W02-17)

- IMP1-2017 (MXN 294.78, closed.jsonl:270): the rule gives BUYER_FAVOR (failed ['2_tracking']); PayPal's record reads kept

## Own line, never merged into the headline: closed before its send (pairs W01-08, W01-11, W01-21)

The decider took a member of these pairs to an outcome while a send was still due for it (DESIGN-v2 Addendum 7).

- IMP1-08: BUYER_FAVOR (route_1_sandbox_accept_claim_then_adjudication), closed.jsonl:189
- IMP1-108: SELLER_FAVOR (route_1_sandbox_adjudication), closed.jsonl:190
- IMP1-11: BUYER_FAVOR (route_1_sandbox_accept_claim_then_adjudication), closed.jsonl:194
- IMP1-111: SELLER_FAVOR (route_1_sandbox_adjudication), closed.jsonl:195
- IMP1-21: BUYER_FAVOR (route_1_sandbox_adjudication), closed.jsonl:210
- IMP1-121: BUYER_FAVOR (route_1_sandbox_accept_claim_then_adjudication), closed.jsonl:211
- IMP1-221: BUYER_FAVOR (route_2_rule_only_posted_not_resolved_in_time), closed.jsonl:317
- MXN: 3 pairs closed, sold per arm MXN 20,547.00; The model alone, no checks: lost MXN 20,547.00 in 3; With Proofie's checks and her approval: lost MXN 13,499.00 in 1

Arm-B members where the product refused the model's draft and the simulated seller typed one fixed sentence (MXN, in the headline): 7 members, MXN 57,877.62. With those counted as lost: Proofie loses MXN 74,834.86 in 13, DIFF MXN 111,125.14, with fees MXN 118,276.48.

Arm-B members where Proofie refused approval over a statement the model wrote and the simulated seller removed exactly that statement (MXN, in the headline): 0 members, MXN 0.00. With every member under either flag counted as lost: Proofie loses MXN 74,834.86 in 13, DIFF MXN 111,125.14.

Arm-A members lost because the model itself did not send (MXN, in the headline): 10 members, MXN 62,218.60; by class P 3 (MXN 29,575.05), G 7 (MXN 32,643.55). Without those pairs: 48 pairs, DIFF MXN 106,784.16 (hard stops only MXN 45,673.19).

Clean packages the model withheld (MXN, in the headline): 0 pairs, MXN 0.00 of arm A's loss; without them arm A loses MXN 185,960.00 in 38.

Arm-B members where the product refused the model's draft and the simulated seller typed one fixed sentence (USD, in the headline): 1 members, USD 141.23. With those counted as lost: Proofie loses USD 816.61 in 3, DIFF USD 3,298.62, with fees USD 3,458.27.

Arm-B members where Proofie refused approval over a statement the model wrote and the simulated seller removed exactly that statement (USD, in the headline): 0 members, USD 0.00. With every member under either flag counted as lost: Proofie loses USD 816.61 in 3, DIFF USD 3,298.62.

Arm-A members lost because the model itself did not send (USD, in the headline): 2 members, USD 1,112.84; by class P 1 (USD 514.23), G 1 (USD 598.61). Without those pairs: 10 pairs, DIFF USD 2,327.01 (hard stops only USD 1,223.76).

Clean packages the model withheld (USD, in the headline): 0 pairs, USD 0.00 of arm A's loss; without them arm A loses USD 4,115.23 in 8.

## Money saved per check (MXN), split by whether PayPal's transaction report had the sale

- check `tracking_number` stopped 8 answers worth MXN 28,902.35 [report_did_not_have_sale: 8 worth MXN 28,902.35] (also fired on the same members: {'unsupported_statement': 8}; pairs W01-13, W01-14, W01-15, W01-16, W01-33, W02-13 ...)
- check `destination` stopped 8 answers worth MXN 13,272.84 [report_had_sale: 8 worth MXN 13,272.84] (also fired on the same members: {'unsupported_statement': 8}; pairs W01-17, W01-18, W01-19, W01-20, W01-34, W02-19 ...)
- check `signature` stopped 4 answers worth MXN 61,110.97 [report_did_not_have_sale: 4 worth MXN 61,110.97] (also fired on the same members: {'unsupported_statement': 1}; pairs W01-22, W01-35, W02-23, W02-25)
- check `delivery_status` stopped 3 answers worth MXN 29,575.05 [report_did_not_have_sale: 3 worth MXN 29,575.05] (also fired on the same members: {'delivery_date': 3, 'unsupported_statement': 1}; pairs W01-23, W01-24, W02-26)
- check `changed_after_approval` stopped 9 answers worth MXN 36,141.55 [report_did_not_have_sale: 9 worth MXN 36,141.55] (also fired on the same members: none; pairs W01-25, W01-26, W01-27, W01-28, W01-36, W02-28 ...)
- false alarms on clean pairs: none

## Money saved per check (USD), split by whether PayPal's transaction report had the sale

- check `tracking_number` stopped 1 answers worth USD 766.36 [report_did_not_have_sale: 1 worth USD 766.36] (also fired on the same members: {'unsupported_statement': 1}; pairs W02-15)
- check `destination` stopped 2 answers worth USD 457.40 [report_had_sale: 2 worth USD 457.40] (also fired on the same members: {'unsupported_statement': 2}; pairs W02-18, W02-21)
- check `delivery_status` stopped 1 answers worth USD 514.23 [report_did_not_have_sale: 1 worth USD 514.23] (also fired on the same members: {'delivery_date': 1}; pairs W02-24)
- check `signature` stopped 1 answers worth USD 1,103.25 [report_did_not_have_sale: 1 worth USD 1,103.25] (also fired on the same members: none; pairs W02-27)
- check `changed_after_approval` stopped 1 answers worth USD 598.61 [report_did_not_have_sale: 1 worth USD 598.61] (also fired on the same members: none; pairs W02-30)
- false alarms on clean pairs: none

## Film case FILM-01 (MXN 500.00, class D; shown on its own, not in the figures above)

- IMP1-905 (The model alone, no checks): BUYER_FAVOR (route_1_sandbox_adjudication); kept MXN 0.00, lost MXN 500.00 (seller_debit_on_dispute_record), fee on PayPal's record MXN 27.54; closed.jsonl:173; evidence/impact_decide_IMP1-905.jsonl:56 (decide_rule); evidence/impact_decide_IMP1-905.jsonl:57 (decide_dispute_read_final); evidence/impact_decide_IMP1-905.jsonl:58 (decide_capture_read_final)
- IMP1-901 (With Proofie's checks and her approval): SELLER_FAVOR (route_1_sandbox_adjudication); kept MXN 500.00, lost MXN 0.00 (no loss), fee on PayPal's record MXN 0.00; closed.jsonl:324; evidence/impact_decide_IMP1-901.jsonl:59 (decide_rule); evidence/impact_decide_IMP1-901.jsonl:60 (decide_dispute_read_final); evidence/impact_decide_IMP1-901.jsonl:61 (decide_capture_read_final)
- IMP1-903 (The model alone, no checks, earlier document set, not the film's twin): BUYER_FAVOR (route_1_sandbox_adjudication); kept MXN 0.00, lost MXN 500.00 (seller_debit_on_dispute_record), fee on PayPal's record MXN 27.54; closed.jsonl:174; evidence/impact_decide_IMP1-903.jsonl:125 (decide_rule); evidence/impact_decide_IMP1-903.jsonl:126 (decide_dispute_read_final); evidence/impact_decide_IMP1-903.jsonl:127 (decide_capture_read_final)
- held, not adjudicated: IMP1-902

## Members resumed once after a PayPal 5xx on a read (in the headline; time apart from the medians)

- IMP1-2224: {"arm": "B_hard", "to_readback_ms": 32617, "to_ready_ms": 15585}

## Time (machine time, local instance with a simulated seller; arm B from the run's own timestamps)

- to ready for approval: n 82, median 14.9 s, total 2088.8 s
- to read-back confirmed: n 82, median 35.0 s, total 3492.5 s
- the model alone: model seconds per case, n 74, median 7.6 s, total 611.1 s

Method: decided by applying PayPal's published Seller Protection rules in code to the package that reached PayPal, recorded with PayPal's sandbox adjudication and read back from PayPal's sandbox record; each pair shares amount, documents and planted error; fee per lost case from PayPal's Mexico merchant-fees page.
