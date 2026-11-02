# Seller Protection rule sets

One JSON file per country and policy version: `<cc>_seller_protection_<effective-date>.json`
(`effective-date` = the "last updated" date printed on the official PayPal document).

- `copilot/core/checklist.py` keeps the registry `RULE_SETS` (country code -> file).
  `load_rules(country="US")` selects by the installation's setting (`COPILOT_RULES_COUNTRY`);
  `load_rules()` still returns MX. An unknown country raises `UnknownCountryError`; callers in the UI path must show that message,
  never fall back to another country silently.
- Each file records `source_url`, `source_title`, `effective_date` and a `verification_note`
  (who read the official document, when, and its SHA-256 if it is a file).
- Every check has a `basis`. `published_policy` is allowed only when the official text says it, and its
  `citation` must name the section or page. Our own checks stay `app_quality_rule`.
- If a country has no signature requirement, set `"signature_required": false` (the check becomes
  `not_applicable` with a message) instead of deleting the check. Otherwise give `signature_limits`.
- A new policy version is a new dated file: do not edit an old one. Re-read the source, update the
  registry, and add tests. Anything not confirmed on the page is written down as unverified.

## `eligibility_notes` (claim-level exclusions, informational)

Some published terms are about the claim itself, not the evidence: a claim type or channel the page says is
not eligible, or a condition for one claim type. They live in `eligibility_notes`, never in `checks`:

- `rows[]`: `id`; `trigger` (dispute-object field -> list of values; every listed field must match one of its
  values; only `reason` and `dispute_channel`, with the names and enum values of PayPal's Customer Disputes API
  document named in `trigger_source`); `quote` (the page's own sentence, verbatim, in the page's language, named
  by `quote_lang`; never paraphrased); optional `lead_in` (the page's own list heading, verbatim, when the quote
  is a list item); `citation` (`page_title`, `page_date` = the page's "last updated" date, `section` = the
  heading as printed, `url`, `read_date`); `basis` = `published_policy`; `effect` = `informational`.
- `copilot/core/eligibility.py` matches rows against the dispute object PayPal returned (code only, no model).
  The review page shows matching rows under "Eligibility notes"; with no match the block is absent.
- A note never changes a check, never adds an action and never says whether PayPal will grant protection.
  Only add a row for a sentence the page states; where a page states nothing for a situation, add no row.

## `clause` (the published words behind a check, optional)

A `published_policy` check may carry a `clause` object: the page's sentence that the check rests on, shown under
the check on the package and triage pages while `COPILOT_FEATURE_CLAUSES` is on.

- Keys, in this order: `quote` (verbatim, in the page's language), `quote_lang`, `section` (the heading as printed),
  `page_title`, `page_date` (the page's "last updated" date), `url`, `read_date`. The same fields as an
  eligibility row's quote and citation, flattened.
- `citation` stays as it is; the clause is added beside it, never in its place.
- `copilot/features/clauses/service.py` `clause_for(rules, check_id)` reads it. An `app_quality_rule` check shows
  "Proofie's own rule"; `case_request` shows "PayPal asked for this". A `published_policy` check with no `clause`
  shows nothing, never a paraphrase.
- Every quote is checked against the kept page extract by `tests/features/clauses/`. US: six checks, from the
  2026-10-06 page text. MX: six checks, in Spanish (`quote_lang` "es"), from the 2026-10-06 extract of the MX legal
  hub page (the same version as the PDF), not from a re-read of the PDF.

Sets: MX 2025-01-13 (PDF, case study), US 2026-01-26 (legal hub page).
