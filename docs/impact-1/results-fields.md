# IMPACT-1 results fields (for the money card; rewritten by code after every closed member)

integer cents (minor units) per currency, never converted; headline policy is B_follow, B_hard beside it.

Fees: fees_cents: 'fees on PayPal's record, equal to the sale's PayPal fee' (never 'chargeback fee'): the SELLER debits of reason DISPUTE_SETTLEMENT_FEE read from PayPal's record of each lost dispute (money_movements). Whether the seller is charged that fee a second time or the sale's fee is only marked as not returned is not settled yet; the report says only what the record lists. DIFF_with_fees uses them. fees_published_cents is PayPal Mexico's published chargeback fee per lost case (MXN 110.00 / USD 10.00, FEES.md (a), (a2), commit b52ed6f2), shown apart and never added to the recorded fee.

## MXN

| Field | Value | Evidence |
|---|---|---|
| N closed pairs | 58 | closed.jsonl:175-333 (116 rows) |
| T sold per arm | MXN 313,601.14 | closed.jsonl:175-333 (116 rows) |
| A kept without Proofie | MXN 127,641.14 | closed.jsonl:175-310 (58 rows) |
| B lost without Proofie | MXN 185,960.00 | closed.jsonl:175-310 (58 rows) |
| C kept with Proofie | MXN 296,643.90 | closed.jsonl:176-333 (58 rows) |
| D lost with Proofie | MXN 16,957.24 | closed.jsonl:176-333 (58 rows) |
| C kept, hard stops only | MXN 235,532.93 | closed.jsonl:176-333 (58 rows) |
| D lost, hard stops only | MXN 78,068.21 | closed.jsonl:176-333 (58 rows) |
| DIFF = C - A (amounts only, the headline) | MXN 169,002.76 | closed.jsonl:175-333 (116 rows) |
| beside it: DIFF with fees on PayPal's record | MXN 176,154.10 | closed.jsonl:175-333 (116 rows) |
| DIFF, hard stops only | MXN 107,891.79 | closed.jsonl:175-333 (116 rows) |
| DIFF with fees, hard stops only | MXN 113,609.75 | closed.jsonl:175-333 (116 rows) |

- without Proofie: PayPal's record shows MXN 185,960.00 debited from the seller in 38 cases, plus MXN 7,955.80 in fees on PayPal's record, equal to the sale's PayPal fee (published fee, apart: MXN 4,180.00); lost amount from the seller debit on PayPal's dispute record in 37, from the dispute amount in 1; resolved on PayPal's record in 57, scored by the rule only (no PayPal resolution on record) in 1
- with Proofie: PayPal's record shows MXN 16,957.24 debited from the seller in 6 cases, plus MXN 804.46 in fees on PayPal's record, equal to the sale's PayPal fee (published fee, apart: MXN 660.00); lost amount from the seller debit on PayPal's dispute record in 6, from the dispute amount in 0; resolved on PayPal's record in 56, scored by the rule only (no PayPal resolution on record) in 2
- with Proofie, hard stops only: PayPal's record shows MXN 78,068.21 debited from the seller in 10 cases, plus MXN 2,237.84 in fees on PayPal's record, equal to the sale's PayPal fee (published fee, apart: MXN 1,100.00); lost amount from the seller debit on PayPal's dispute record in 8, from the dispute amount in 2; resolved on PayPal's record in 54, scored by the rule only (no PayPal resolution on record) in 4

## USD

| Field | Value | Evidence |
|---|---|---|
| N closed pairs | 12 | closed.jsonl:246-331 (24 rows) |
| T sold per arm | USD 4,557.37 | closed.jsonl:246-331 (24 rows) |
| A kept without Proofie | USD 442.14 | closed.jsonl:246-289 (12 rows) |
| B lost without Proofie | USD 4,115.23 | closed.jsonl:246-289 (12 rows) |
| C kept with Proofie | USD 3,881.99 | closed.jsonl:247-331 (12 rows) |
| D lost with Proofie | USD 675.38 | closed.jsonl:247-331 (12 rows) |
| C kept, hard stops only | USD 2,778.74 | closed.jsonl:247-331 (12 rows) |
| D lost, hard stops only | USD 1,778.63 | closed.jsonl:247-331 (12 rows) |
| DIFF = C - A (amounts only, the headline) | USD 3,439.85 | closed.jsonl:246-331 (24 rows) |
| beside it: DIFF with fees on PayPal's record | USD 3,599.50 | closed.jsonl:246-331 (24 rows) |
| DIFF, hard stops only | USD 2,336.60 | closed.jsonl:246-331 (24 rows) |
| DIFF with fees, hard stops only | USD 2,496.25 | closed.jsonl:246-331 (24 rows) |

- without Proofie: PayPal's record shows USD 4,115.23 debited from the seller in 8 cases, plus USD 191.28 in fees on PayPal's record, equal to the sale's PayPal fee (published fee, apart: USD 80.00); lost amount from the seller debit on PayPal's dispute record in 8, from the dispute amount in 0; resolved on PayPal's record in 12, scored by the rule only (no PayPal resolution on record) in 0
- with Proofie: PayPal's record shows USD 675.38 debited from the seller in 2 cases, plus USD 31.63 in fees on PayPal's record, equal to the sale's PayPal fee (published fee, apart: USD 20.00); lost amount from the seller debit on PayPal's dispute record in 2, from the dispute amount in 0; resolved on PayPal's record in 12, scored by the rule only (no PayPal resolution on record) in 0
- with Proofie, hard stops only: PayPal's record shows USD 1,778.63 debited from the seller in 3 cases, plus USD 31.63 in fees on PayPal's record, equal to the sale's PayPal fee (published fee, apart: USD 30.00); lost amount from the seller debit on PayPal's dispute record in 2, from the dispute amount in 1; resolved on PayPal's record in 11, scored by the rule only (no PayPal resolution on record) in 1

## Film cases (on their own, not in the figures above)

- IMP1-905 (The model alone, no checks): PayPal's record shows MXN 500.00 debited from the seller, plus MXN 27.54 in fees (RESOLVED_BUYER_FAVOUR, route_1_sandbox_adjudication); amount kept MXN 0.00; at the sale gross MXN 500.00, PayPal fee MXN 27.54, net MXN 472.46 (decide/evidence/impact_decide_IMP1-905.jsonl:58); debits on the dispute's record {"DISPUTE_SETTLEMENT": [50000], "DISPUTE_SETTLEMENT_FEE": [2754]} in cents (decide/evidence/impact_decide_IMP1-905.jsonl:57); decide/closed.jsonl:173; dispute GET decide/evidence/impact_decide_IMP1-905.jsonl:57; capture GET decide/evidence/impact_decide_IMP1-905.jsonl:58
- IMP1-901 (With Proofie's checks and her approval): the seller keeps the sale; PayPal's record shows no debit to her (RESOLVED_WITH_PAYOUT, route_1_sandbox_adjudication); amount kept MXN 500.00; at the sale gross MXN 500.00, PayPal fee MXN 27.54, net MXN 472.46 (decide/evidence/impact_decide_IMP1-901.jsonl:61); debits on the dispute's record {} in cents (decide/evidence/impact_decide_IMP1-901.jsonl:60); decide/closed.jsonl:324; dispute GET decide/evidence/impact_decide_IMP1-901.jsonl:60; capture GET decide/evidence/impact_decide_IMP1-901.jsonl:61
- IMP1-903 (The model alone, no checks, earlier document set, not the film's twin): PayPal's record shows MXN 500.00 debited from the seller, plus MXN 27.54 in fees (RESOLVED_BUYER_FAVOUR, route_1_sandbox_adjudication); amount kept MXN 0.00; at the sale gross MXN 500.00, PayPal fee MXN 27.54, net MXN 472.46 (decide/evidence/impact_decide_IMP1-903.jsonl:127); debits on the dispute's record {"DISPUTE_SETTLEMENT": [50000], "DISPUTE_SETTLEMENT_FEE": [2754]} in cents (decide/evidence/impact_decide_IMP1-903.jsonl:126); decide/closed.jsonl:174; dispute GET decide/evidence/impact_decide_IMP1-903.jsonl:126; capture GET decide/evidence/impact_decide_IMP1-903.jsonl:127
- held, not adjudicated: IMP1-902

## Own line, never merged into the headline: closed before its send (pairs W01-08, W01-11, W01-21)

- IMP1-08: BUYER_FAVOR (route_1_sandbox_accept_claim_then_adjudication), closed.jsonl:189
- IMP1-108: SELLER_FAVOR (route_1_sandbox_adjudication), closed.jsonl:190
- IMP1-11: BUYER_FAVOR (route_1_sandbox_accept_claim_then_adjudication), closed.jsonl:194
- IMP1-111: SELLER_FAVOR (route_1_sandbox_adjudication), closed.jsonl:195
- IMP1-21: BUYER_FAVOR (route_1_sandbox_adjudication), closed.jsonl:210
- IMP1-121: BUYER_FAVOR (route_1_sandbox_accept_claim_then_adjudication), closed.jsonl:211
- IMP1-221: BUYER_FAVOR (route_2_rule_only_posted_not_resolved_in_time), closed.jsonl:317
- MXN: 3 pairs closed, sold per arm MXN 20,547.00; without Proofie lost MXN 20,547.00 in 3; with Proofie lost MXN 13,499.00 in 1
- MXN, arm-B members with a statement typed by the simulated seller (in the headline): 7 members, MXN 57,877.62; with those counted as lost, DIFF is MXN 111,125.14
- MXN, members resumed once after a PayPal 5xx on a read (in the headline): without Proofie 0, with Proofie 0, hard stops only 0; their time per case is reported apart from the medians
- MXN, arm-B members where the seller removed a statement Proofie refused (in the headline): 0 members, MXN 0.00; with every member under either flag counted as lost, DIFF is MXN 111,125.14
- MXN, arm-A members lost because the model itself did not send (in the headline): 10 members, MXN 62,218.60; by class P 3 (MXN 29,575.05), G 7 (MXN 32,643.55); DIFF without those pairs: MXN 106,784.16 over 48 pairs
- MXN, clean packages the model withheld (in the headline): 0 pairs, MXN 0.00; without them, lost without Proofie is MXN 185,960.00 in 38
- USD, arm-B members with a statement typed by the simulated seller (in the headline): 1 members, USD 141.23; with those counted as lost, DIFF is USD 3,298.62
- USD, members resumed once after a PayPal 5xx on a read (in the headline): without Proofie 0, with Proofie 0, hard stops only 1; their time per case is reported apart from the medians
- USD, arm-B members where the seller removed a statement Proofie refused (in the headline): 0 members, USD 0.00; with every member under either flag counted as lost, DIFF is USD 3,298.62
- USD, arm-A members lost because the model itself did not send (in the headline): 2 members, USD 1,112.84; by class P 1 (USD 514.23), G 1 (USD 598.61); DIFF without those pairs: USD 2,327.01 over 10 pairs
- USD, clean packages the model withheld (in the headline): 0 pairs, USD 0.00; without them, lost without Proofie is USD 4,115.23 in 8

## Own line, never merged into the headline: sale not yet in PayPal's transaction report: the address check could not run

- no pair on this line
