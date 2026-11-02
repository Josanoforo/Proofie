# Proofie: visual design

Status: **implemented** in `copilot/web/static/app.css` (palette C and the review fixes). The contrast ratios below
are recomputed from that stylesheet on every test run by `tests/local/test_design_contrast.py` (WCAG 2.x relative luminance); the same
test checks that this document lists the exact hex values the CSS ships, that green/red/orange are used only where section 2 allows,
and that a secondary button stays secondary inside every banner. Layout and colour in a real browser are checked by `demo/ui_check.py`.
Everything here is original to this project: no stylesheet, palette or component was copied from another project or from PayPal's
brand, and no PayPal logo or exact PayPal brand colour is used (palette C was chosen by the owner; see `docs/DECISIONS.md` D-2026-10-05-03).
Plain CSS only: no framework, no build step, no CDN, no web fonts (the app runs on the seller's own machine).
Every page carries, in the footer of the shared base template, the visible note **"Independent project. Not affiliated with or endorsed by PayPal."**

## 1. Intent
A work tool for a small seller who has a dispute open and a deadline, not a marketing page. The screen should feel **calm, plain and
trustworthy**: the seller always sees what we know and where it comes from, what is missing, what does not match, what will be sent
and what PayPal itself confirmed. Consequences:
- Information first. Neutral surfaces, one accent colour for actions, colour for state used only together with a text label.
- The assistant is never the author of a fact. Anything suggested by the model looks different (dashed edge + the words
  "Suggested — check against the source") and never wears the "verified" look.
- Nothing decorative moves. No animation except the browser's own focus ring; no auto-playing anything.
- The word "sandbox" stays visible in the header: this build never touches live PayPal.

## 2. Colour tokens (palette C: own navy and blue, product orange)
Defined once as CSS custom properties on `:root` (light), overridden under `@media (prefers-color-scheme: dark)` and again under
`:root[data-theme="dark"]` / `[data-theme="light"]` so a person (or a test) can force a mode. Components use the tokens only, never a hex.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#f5f4f0` | `#121518` | page background |
| `--surface` | `#ffffff` | `#1b1f24` | cards, tables, inputs |
| `--surface-2` | `#ebe9e3` | `#252a31` | table header, excluded-document rows, code blocks |
| `--text` | `#1b1f23` | `#e8ebee` | body text |
| `--text-muted` | `#4a5159` | `#aab2bb` | secondary text, captions, "PayPal was read at" line, footer |
| `--border` | `#7a818a` | `#77818c` | outlines of controls (3:1 against every surface) |
| `--border-soft` | `#d5d2ca` | `#343b44` | decorative dividers only (never the only boundary of a control) |
| `--accent` | `#1558b0` | `#8ab8f5` | primary button fill |
| `--accent-text` | `#1558b0` | `#8ab8f5` | links and the label of a secondary button (text on a surface) |
| `--on-accent` | `#ffffff` | `#08162e` | label on the primary button |
| `--focus` | `#0b4aa8` | `#8ab8ff` | focus ring on the page |
| `--header-bg` | `#0b2f6b` | `#0d2554` | navy page header |
| `--header-fg` / `--header-muted` | `#ffffff` / `#c9d7f0` | `#ffffff` / `#bccbe8` | header text / its secondary text |
| `--header-focus` | `#ffffff` | `#ffffff` | focus ring inside the header |
| `--brand` | `#e8590c` | `#f47a2a` | the 4 px rule under the header (nothing else) |
| `--brand-mark` | `#f47a2a` | `#ff944d` | the small square before the product name (nothing else) |
| `--info-bg` / `--info-fg` / `--info-edge` | `#e7effa` / `#0b2f6b` / `#1558b0` | `#14284d` / `#d3e3fb` / `#8ab8f5` | neutral notices; sent or saved but not yet read back from PayPal; a statement backed by a source |
| `--ok-bg` / `--ok-fg` / `--ok-edge` | `#e4f2e8` / `#14532d` / `#1e7a3f` | `#16301f` / `#b7e4c4` / `#5cbf7d` | **only** verified and confirmed: a check that passes, "Evidence sent to PayPal" once the read-back confirms it, "recovered" |
| `--warn-bg` / `--warn-fg` / `--warn-edge` | `#fcefd0` / `#6b4300` / `#9a6200` | `#3a2c0c` / `#f5d791` / `#d9a63a` | not known yet, or needs your review (uncertain, partial, cannot verify, not confirmed by PayPal) |
| `--error-bg` / `--error-fg` / `--error-edge` | `#fae4e1` / `#8a1c14` / `#b3261e` | `#3b1916` / `#f6c3be` / `#ef7c72` | does not match, missing, blocked or refused: a mismatch or missing fact, a local block, a PayPal refusal, PayPal unreachable |

**One job per colour (enforced by a test that reads `app.css`):**
- **Green ✓** means verified or confirmed: a check that passes, or a banner that says PayPal's own record shows the content ("Evidence sent to PayPal" after the
  read-back, "recovered"). A statement backed by a source, or content sent or saved but not yet read back, is information (blue), not green.
- **Red ✕** means does not match, missing, blocked or refused: a conflict between your document and PayPal's data ("Does not match"), a missing required fact, a
  local block or abandoned attempt, a PayPal refusal, PayPal unreachable. The words say which, and whether PayPal was involved.
- **Amber !** means "we do not know yet" or "this needs your review" (cannot verify, not confirmed by PayPal, uncertain, partial; PayPal answered but the read-back did not confirm); it is never used for a mismatch or a refusal.
- **Grey —** is not applicable; **blue i** is sent or saved and not yet read back, or neutral information (a tracking file saved without registration is blue; registered and confirmed by the read-back is green; accepted by PayPal but not listed on the read-back is amber); a neutral tag ○ on the card fill is not started.
- Every state carries an icon and a text label with its colour, so colour is never the only cue.
- **Orange** appears only in the product mark and the header rule; never on a button, chip, banner or check row.

Values changed from the first drafts, and why: the neutral family moved from teal to the same blue hue family as the accent (the old teal no longer
matched the palette); dark-mode tones are derived from the same hues and lifted until they pass AA; nothing was changed from the starting values the owner's
choice implied (`#0b2f6b`, `#1558b0`, `#e8590c`, `#f47a2a`) - all four reach their targets as given.

State semantics: **amber means "we do not know yet"** (result uncertain, partial, cannot verify); it is never used for a refusal or a mismatch. This follows the six-state vocabulary the app has shown since the RC3 interface round (`copilot/web/templates/_components.html`, `copilot/web/static/app.css`).

## 3. Typography
System and local fonts only; nothing is downloaded.
- Text: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`.
- Technical values (ids, tracking numbers, fingerprints, folded technical details): `ui-monospace, "Cascadia Code", Consolas, "Liberation Mono", monospace`.
- Scale (rem, base 16 px): caption 0.875, body 1, lead 1.125, h3 1.125 (bold), h2 1.375, h1 1.75. Line height 1.5 for text, 1.25 for headings.
- Money, dates and numbers are printed exactly as the code computed them; the layout never reformats them.
- Text may be zoomed to 200 % without losing content or functions (no fixed heights on text containers).

## 4. Spacing, shape
- Spacing scale (px): 4, 8, 12, 16, 24, 32, 48 as `--space-1` ... `--space-7`. Page gutter 16 px on phones, 24 px from 640 px up.
- Content column max 960 px; the inbox and the checks table may use the full column.
- Radius 6 px (controls, cards), 999 px only for chips. Borders 1 px (controls, table lines), 2 px (focus ring offset), 6 px (left edge of a banner).
- One breakpoint that matters: **640 px**. Below it tables become stacked rows (each cell carries its `data-label`).

## 5. Components
Every component below already exists as markup in the templates (class names and `data-state` attributes are the contract with the tests);
the stylesheet only restyles them.

1. **Page frame.** Navy header (`--header-bg`) with a 4 px orange rule under it, the small orange square, the product name in white (link to the inbox),
   the same links on every page except the hosted demo's access-code page ("All disputes", and "Tracking at the sale" when that feature is on; the current one is underlined and marked `aria-current`)
   and the tag "PayPal sandbox · assistant only, you review everything". Inner pages show breadcrumbs under the header, from "All disputes" or "Tracking at
   the sale" to the current page (last, not a link); below 640 px only the parent crumb shows, after a "‹". `main` is a single column. A footer on every page (the shared base template)
   says: "Independent project. Not affiliated with or endorsed by PayPal."
2. **Inbox table** (`disputes_list.html`). Columns: Dispute, Status, Respond by, Amount, Next step. Rows at least 44 px tall; the dispute id is a link
   in monospace with the reason under it; Respond by shows PayPal's due date and the time left; Next step is a button or plain words, and only the first
   row that needs you gets a primary button. Under 640 px each row becomes a card and each cell shows its column name above its value
   (`data-label`). The "PayPal was read at" line sits above the table in `--text-muted` with its "Refresh from PayPal" link.
3. **State tag and chip.** A tag is an icon and a short text label on its state's `*-bg` fill with `*-fg` text (a not-started ○ tag such as "Draft" sits on the card fill): the status of a dispute, the time left, and the
   package's lifecycle words: "Draft" (or "Draft: needs attention"), "Approved, not sent", and "Sent to PayPal" only once PayPal's read-back confirms the send
   (until then "Send attempted, not confirmed by PayPal"). The label always says the state in words; colour is never the only cue. A chip (the basis
   of a statement) is a pill with a 1 px `*-edge` border; its border style is a second cue: solid = known, dashed = suggested by the assistant.
4. **Checklist row** (`package_review.html`, `tr[data-check-state]`). Left cell: the check label. State cell: a tag with an icon and the state in words, and a 6 px left edge (green for verified, amber for cannot verify, **red for missing and does not match**, grey for not applicable); the words are those the page shows ("Verified", "Missing", "Present, but Proofie cannot verify", "Does not match", "Not applicable"; an optional missing fact shows "Not started"),
   and below it the **basis** line ("Because: PayPal's case asks for this" / "PayPal Seller Protection policy" / "Proofie's own quality rule"),
   the "Why" text from the checklist, then the row's actions as links or small secondary buttons. A risk adds the words "Puts your Seller Protection at risk".
5. **Claim card** ("Your statements to PayPal"). The statement text; a source line ("Source: document label.pdf" / "Source: you" /
   "Source: PayPal's dispute record"); a basis chip (blue, in words: "Backed by a source", "Your own statement"). A model suggestion gets the dashed edge and the label "Suggested — check against the source"; confirming a reading is a
   secondary button ("Confirm this reading") under the line that names the field and value. An edited statement says "edited by you".
6. **Banner** (`.banner` + `data-state`). Left edge 6 px, title in bold (the state in words), then the explanation, then the action(s) inside the same box.
   `role="alert"` for errors, `role="status"` for the rest. Families: info, ok, warn, error as in section 2.
7. **Send banner** (the persistent status on the review page and the result page). Same component; the states and their families are:
   prepared / approved = info; result uncertain / partial / approval cleared = warn; confirmed / recovered = ok; blocked / abandoned / rejected / PayPal unreachable = error (the words say whether PayPal or this app refused). An abandoned attempt is red on every page (package chip, dispute task row, outcome history). While PayPal reviews a claim (CHARGEBACK, UNDER_REVIEW) the next step is "PayPal is reviewing this claim." and no page invites an action; while PayPal lists evidence from the seller the next step is "PayPal already has evidence from you." with the sentence "Proofie sends evidence once per dispute and will not send again.".
8. **Buttons.** Three kinds, drawn by the shared `button`, `link_button` and `post_button` macros. Primary, for the page's next step: `--accent` fill,
   `--on-accent` bold label, 1 px transparent border, always large (at least 56 px high, full width below 600 px). Secondary: transparent fill, `--accent-text`
   label, 1 px `--border` outline, min 44 x 44 px, 12 px x 16 px padding. Link: underlined `--accent-text` words, min 44 x 44 px. Every label says the action in words
   ("Send evidence to PayPal", "Add tracking in PayPal", "Check again"). While a send or a check is running, lower opacity is not enough: the label itself changes
   ("Sending to PayPal…", "Checking PayPal…") and the cursor shows progress. The send button is never the first or only large button on a page where something is still uncertain.
9. **Forms.** Every input has a visible label (or a `label` wrapping it), 44 px high, 1 px `--border` outline, `--surface` fill. File input and text input share the look.
10. **Details.** `<details>` for technical data (raw dispute, validation fields, technical error text); the summary is a real, focusable control; the content is monospace.
11. **Empty and error pages.** Same banner component; an empty state is info, an error page is error, both with a primary action.
12. **Task list and step note** (`ol.task-list`, `.step-note`). The dispute page lists its four tasks in order, each a card with its state tag ("Upload documents",
    "Prepare evidence package", "Review each statement and approve", "Send evidence to PayPal"); a task that cannot start yet says why. The step note is small muted
    text under the page title ("Step 2 of 4: Prepare evidence package"). Neither is an alert.
13. **Shipment form** (`.field`, `.field-hint`, `details[data-state="optional-details"]`). One column: the carrier select first ("Carrier (shipping company)"), then
    "Tracking number (from the carrier's receipt)". Each field reads label, hint in muted text, then the control (the hint is never the only place a rule is stated:
    the check row says it too). Required marker is text in the label, never colour. The optional group is a `<details>`. Selects share the text input's look.
14. **Quiet notes** (`approve-note`, `send-note`, `empty-guide`, `no-rule-set`, `model-off`). Plain muted text, 46 rem measure; the last two sit on `--surface-2`. They explain; they do not warn.
15. **Stage strip** (`.stage-strip`, dispute page). PayPal's four stages (Inquiry, Chargeback, Pre-arbitration, Arbitration) as an ordered list, a column below 640 px and a row from it. The current stage is told by its words ("(current stage)"), a bold weight and a 3 px edge in `--text` (the system highlight colour in forced-colours mode). The other stages carry no state, so nothing is predicted. On the dispute page a stage PayPal does not return draws no "Stage" fact and no strip, and a value the app does not know draws the fact "Stage not recognized" and no strip; only the outcome page's "Stage" row reads "Not returned by PayPal" when PayPal gave none.
16. **Site header on scroll.** The header stays in view when the window is at least 640 px wide and 520 px high (the guard is in `em`, so a larger text size turns it off) and scrolls away below that; `scroll-padding-top` of 6.5rem keeps an anchor or a focused control from landing under it.
17. **Error glyph and undo.** A message next to a field starts with "✕" (hidden from screen readers; the words carry the meaning). A banner with no status label of its own carries a glyph tag whose hidden text names its kind: "Done", "Warning", "Note" or "Error". A done banner can carry a link-styled "Undo" button inside the banner.

## 6. Accessibility (measured, not asserted)
Contrast is computed with the WCAG 2.x formula on the hex values in section 2, from `app.css`, by `tests/local/test_design_contrast.py`.
Targets: **4.5:1 for text, 3:1 for the outlines of controls, focus rings, banner edges and the orange graphics.**
Result for the shipped values (all pass; the lowest in each row is bold):

| Pair | Light | Dark | Need |
|---|---|---|---|
| text on page / card / tinted row | 15.06 / 16.58 / **13.65** | 15.31 / 13.84 / **12.07** | 4.5 |
| muted text on page / card / tinted row | 7.30 / 8.04 / **6.62** | 8.55 / 7.72 / **6.74** | 4.5 |
| link text on page / card / tinted row | 6.25 / 6.87 / **5.66** | 8.95 / 8.09 / **7.05** | 4.5 |
| primary button label on its fill | 6.87 | 8.81 | 4.5 |
| info / ok / warn / error banner text on its fill | 11.10 / 7.88 / **7.58** / 7.64 | 11.21 / 10.10 / **9.71** / 10.08 | 4.5 |
| header text / header secondary text on the navy | 12.86 / **8.86** | 14.90 / **9.11** | 4.5 |
| control outline on page / card / tinted row | 3.58 / 3.94 / **3.24** | 4.63 / 4.18 / **3.65** | 3 |
| focus ring on page / card / tinted row | 7.46 / 8.21 / **6.76** | 9.06 / 8.19 / **7.14** | 3 |
| header focus ring on the navy | 12.86 | 14.90 | 3 |
| banner edges (info / ok / warn / error) on their fill | 5.93 / 4.64 / **4.46** / 5.37 | 7.12 / 6.23 / 6.12 / **5.83** | 3 |
| primary button fill against the page | 6.25 | 8.95 | 3 |
| orange mark / orange rule on the navy | 4.69 / **3.59** | 6.81 / **5.44** | 3 |
| orange rule against the page | 3.25 | 6.69 | 3 |

An excluded document's row is drawn with `--surface-2` and `--text-muted` (plus its words, "No - excluded"); it never uses `opacity`, which would lower the
text and outline contrast below these figures. A secondary button inside any banner keeps a transparent fill, the banner's own text colour and a
`currentColor` outline (its selector out-ranks every banner button rule), so it measures the same as the banner text above.

Other requirements:
- **Focus is always visible**: 3 px solid `--focus` outline with 2 px offset on every link, button, input, summary (white inside the header; drawn inward, with a -3 px offset, for links and summaries inside a scrolling table region); never `outline: none` without a replacement.
- **Targets**: every control is at least 44 x 44 px (buttons, inputs, links used as actions - including links inside table cells such as the dispute id, the inbox's "Review the draft" button and a package version - and checkbox rows). Links inside running text keep the text size but are underlined.
- **State never by colour alone**: each state has words; each family also has a second cue (edge width/position, dashed vs solid for suggestions).
- **Keyboard**: all routes reachable by Tab in reading order; "Send evidence to PayPal" and "Check again" on the package and send-result pages are real buttons in forms (on the dispute page "Check again" is a link that reloads the page, which reads PayPal again); no keyboard trap; the double-submit guard keeps the button focusable.
- **Screen readers**: banners use `role="alert"`/`status`; tables keep `th` cells; stacked rows on phones keep the `data-label` text as visible labels, not as hidden attributes.
- **No horizontal page scroll at 375 px**; wide technical text wraps (`overflow-wrap: anywhere` inside `<pre>` and monospace ids).
- **Answers to a POST.** The confirm-not-received answer is post-redirect-get: the POST answers with a redirect and the answer is a page read with GET, so reloading or going back does not repeat it. The hosted-demo refusals, the 4xx re-renders and the appeal preview shown while the appeal setting is off still render from the POST, on purpose.
- **Times** on screen are written in one fixed English format in Mexico City time, e.g. `16 Oct 2026, 10:10 pm (Mexico City time)`, with the exact ISO value and its offset in the `<time datetime>` attribute (fixed month names, never the operating system's zone name or language), so the "PayPal was read at" line and PayPal's due date can be compared directly. Date-only values in form messages, in the draft text, on the account page and on the triage page, for example, use the long month ("5 October 2026"; `copilot/web/form_fields.py` `date_words`, `copilot/web/presentation.py` `dates_in_words`); the dates on the dispute page, the inbox and the delivery facts use the short form ("5 Oct 2026").
- **Not tested with real assistive technology**: NVDA, VoiceOver and TalkBack have not been run on these pages. The checks are structural (the browser's accessibility tree) and visual, run offline on every page state.
- **Motion and preferences**: respects `prefers-color-scheme` and `prefers-reduced-motion` (there is no motion to reduce); `color-scheme: light dark` is declared so form controls follow the theme.

## 7. How this is checked (`demo/ui_check.py`, run on request)
- Playwright (already in `requirements-dev.txt`) renders the inbox, a dispute, the package review and a send result at **1280 px and 375 px, in light and dark**
  against a local app wired to fake clients with synthetic or sandbox data; captures go to `data/ui-captures/` (not versioned).
- The same run checks: no horizontal scroll at 375 px, the focus ring is visible on each control (computed outline), every control is at least 44 px,
  and the double-click guard (including the `pageshow` restore) behaves in a real browser.
- A unit test recomputes the contrast ratios from `app.css` so a later colour edit cannot silently break AA.
- A separate offline Playwright harness renders every page state (180 on the RC4.2 candidate) and checks structure and accessible names, target size, contrast in light, dark and forced colours, reflow at 320 and 390 px, text spacing, and keyboard order at 1280, 768 and 390 px. Its second run on the merged RC4.2 code (2026-10-18) reported no failing check; the deviations kept on purpose (for example one more Tab stop on three keyboard journeys, for two links added on purpose) are recorded as waivers. A reading pass over the same page states then found seven wording and language-marking problems, fixed before the cut. The harness is an in-house rule check, not axe-core, and it is not part of this repository; the contrast unit test and `demo/ui_check.py` above are the parts you can run.
- Screenshots are not proof of a send: a send result is only closed by the network log and PayPal's own GET.
