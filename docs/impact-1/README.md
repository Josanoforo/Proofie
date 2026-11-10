# IMPACT-1: results, per case, by code

This folder holds the frozen results of IMPACT-1, a measurement run in PayPal's sandbox. The four result files are
byte copies of the snapshot frozen on 2026-11-09; nothing in them was edited for this folder.

## What was measured

The same sales and the same disputes were answered twice, as pairs. Each pair is one sale design sold two times, with
one dispute opened on each sale.

- **Arm A, "The model alone, no checks":** the local model writes the answer and it is sent as written.
- **Arm B, "With Proofie's checks and her approval":** the same model writes the draft, Proofie's checks run on it,
  and a simulated seller follows what the product shows before she approves and sends.

A pair counts when both of its members are closed. 70 pairs are in the headline: 58 in MXN and 12 in USD.

## Headline, per currency

Taken as written from `score.md`. Amounts are the dispute settlement debits; fees are shown beside them in that file
and are not inside these figures. Currencies are never converted or added together.

- **MXN, 58 closed pairs, sold per arm MXN 313,601.14.** Kept by the model alone: MXN 127,641.14. Kept with Proofie's
  checks and her approval: MXN 296,643.90. Difference, kept with Proofie minus kept by the model alone: **MXN 169,002.76**.
- **USD, 12 closed pairs, sold per arm USD 4,557.37.** Kept by the model alone: USD 442.14. Kept with Proofie's checks
  and her approval: USD 3,881.99. Difference, kept with Proofie minus kept by the model alone: **USD 3,439.85**.

`score.md` also shows, on their own lines and outside the headline: the stricter reading where Proofie's warnings are
not followed and its hard stops alone apply, the pairs closed before their send, the one pair that was not scored,
and the misses by class for each arm.

## How the outcomes and the money were read

- Every outcome is read from PayPal's sandbox record of the dispute. The model never states an outcome or a number.
- Money is computed by code, in integer cents per currency.
- 161 members are closed. 154 of them were resolved on PayPal's record. The other 7 were scored by the rule, with no
  PayPal resolution on record; `score.md` counts them per arm in the last column of each table.
- These are sandbox disputes. The figures describe this run and are not a forecast of live results.

## Files

| File | What it is | SHA-256 |
| --- | --- | --- |
| `score.md` | The score, readable | `74778c55fa7341cf27d7369ed2623561e043c33d310cd0ce57296e388b518a74` |
| `score.json` | The same score, as data | `fe474adabc26f8755262359b3a45adca90793371904e9125afb46ff6d7fe88e7` |
| `results-fields.md` | The named fields quoted elsewhere, readable | `248b575cc6ec4fee623d355c30b60db33178059597573f2a0fc34a713f80cec6` |
| `results-fields.json` | The same fields, as data | `5c6d7e89ddf051c84c2f1c4dd1e00274ad6e8768cd68ba1d6f94aaff2231bf95` |

The files cite `closed.jsonl:<line>` and `evidence/...jsonl:<line>`. Those are the private per-case records of the run;
they hold PayPal response bodies and are not published. The program that computes the score reads those records, so
it stays out of this repository too.
