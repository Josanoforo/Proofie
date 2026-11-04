# Proofie brand identity manual

Version 1.0. The reference for every Proofie surface: app, README, Devpost page and gallery, video, social preview. Source files sit next to this manual in `docs/brand/`. Product tokens and measured contrast come from `copilot/web/static/app.css` and `docs/DESIGN.md`; ratios marked *computed* use the same WCAG 2.x formula on the hex values shown.

**Independent project. Not affiliated with or endorsed by PayPal.**

---

## 1. Brand idea and platform

**Two sides, one proof both can see.** A PayPal "item not received" dispute has two sides. Proofie works for the seller, and at some steps (a message, an offer, a refund she decides on) what she sends reaches the buyer too. The mark says it plainly: the seller's bracket and the buyer's bracket enclose one check both can read.

| | |
|---|---|
| **Purpose** | Help a small seller answer a PayPal dispute with evidence she can stand behind, and keep the decision in her hands. |
| **Promise** | Evidence you can stand behind, sent the way you approved it, with PayPal's record read back. |
| **Positioning** | The seller's evidence desk for PayPal disputes: check the evidence, approve the exact package, read PayPal's record back. |
| **One-liner** | Proofie helps a small seller answer a PayPal dispute with evidence she approves, then reads PayPal's own record back to show what it received. |

| Personality | Is | Is not |
|---|---|---|
| Plain-spoken | Short sentences; the actor named ("You approve") | Jargon, chatter |
| Exact | Numbers and dates as the code printed them; the source named | Rounding, "about" |
| On the seller's side | Her case, her decision; fair to the buyer and PayPal's process | Combative ("fight", "beat") |
| Shows its work | Limits stated once; the model's measured failures published | Self-praise, hedging |
| Calm | Steady under a deadline; red reserved for a real mismatch | Alarm, urgency marketing |

**Values.** (1) The person decides: nothing is sent until the seller approves the exact package, and the assistant has no approve tool. (2) Every statement has a source: a document, a PayPal field, or "Your own statement". (3) Say what PayPal confirmed: "Sent to PayPal" turns green after the read-back. (4) Limits in daylight: sandbox build, the model is an assistant, its evaluation is dated with failures. (5) Local by default: the app and its model run on the seller's machine.

**Relationship to PayPal: works with, not part of.** Name PayPal descriptively ("a PayPal dispute", "PayPal's record", "the PayPal sandbox"), never as partner, sponsor or endorser. Never use a PayPal logo, button, badge, typeface or brand colour, nor "Pay", "PP" or a "P" monogram. Never print ™ or ®. The line **"Independent project. Not affiliated with or endorsed by PayPal."** appears verbatim in the app footer, README, social preview, gallery caption bar and video end card (the Devpost thumbnail is exempt; the page around it carries the line).

---

## 2. Logo

### 2.1 Construction (64-unit grid)
Every stroke: width 7.5 units, round caps and joins, no fill. The brackets mirror each other about x = 32; the check is centred on (32, 32).

| Part | Path (`proofie-mark.svg`) | Geometry | Meaning |
|---|---|---|---|
| Seller bracket, coral `#E4572E` | `M21 9h-6.5A6.5 6.5 0 0 0 8 15.5v33A6.5 6.5 0 0 0 14.5 55H21` | Stem x = 8, y 9–55; arms to x = 21; radius 6.5 | The seller, who authors the evidence |
| Buyer bracket, teal `#12827A` | `M43 9h6.5A6.5 6.5 0 0 1 56 15.5v33a6.5 6.5 0 0 1-6.5 6.5H43` | Mirror: stem x = 56, arms to x = 43 | The other side, reading the same record |
| Check, ink `#102A43` (white on dark) | `M21.5 32.5l7.5 7.5 13.5-16` | (21.5, 32.5) → (29, 40) → (42.5, 24) | The sourced proof, in neutral ink: neither side's colour |

The 22-unit gap between the arms keeps the brackets open; a closed box would read as a checkbox.

**Small-size variant** (`proofie-mark-16.svg`, 16–23 px): stroke 9, radius 5, stems x 8 and 56 (y 8–56), arms to x 19 and 45, check (20, 33) → (28.5, 41.5) → (44, 23).

**Wordmark:** lowercase "proofie", weight 700. In the lockup (`viewBox 0 0 260 64`): 38 units, letter-spacing −0.6, baseline y = 45, starting at x = 76 (12 units after the mark box). In running text write "Proofie"; the lowercase form belongs to the wordmark.

### 2.2 Files

| File | Use |
|---|---|
| `proofie-mark.svg` | Full colour on light, 24 px and up |
| `proofie-mark-16.svg` | Small-size mark, 16–23 px |
| `proofie-mark-on-ink.svg` | On ink or dark backgrounds (white check) |
| `proofie-mark-mono-ink.svg` | One colour: single-colour print, embossing, light photos |
| `proofie-mark-mono-white.svg` | One colour on dark fields where colour is unavailable |
| `proofie-lockup.svg` / `proofie-lockup-on-ink.svg` | Horizontal lockup on light / on dark |
| `proofie-social-preview.svg` | Repository social preview, 1280 × 640 |
| `proofie-devpost-thumbnail.svg` | Devpost thumbnail, 1200 × 800 |
| `preview.html` | Visual check of every file, the palette and the header |

The SVGs name Inter with a system fallback. Before exporting a PNG, render with Inter installed or convert the text to outlines.

### 2.3 Clear space, minimum size, colour versions
- **Clear space:** 16 units (a quarter of the mark box) on every side of the 64-unit box, for mark and lockup.
- **Minimum size:** standard mark 24 px; 16–23 px use `proofie-mark-16.svg`; lockup 32 px tall (below that, mark plus live-text wordmark, as in the app header). Print: mark 6 mm, lockup 10 mm.

| Version | Background | Check | Coral | Teal |
|---|---|---|---|---|
| Full colour | White `#FFFFFF` | 14.64 | 3.68 | 4.67 (computed) |
| Full colour | Paper `#F7F5F0` | 13.44 | 3.38 | 4.28 (computed) |
| On ink | Ink `#102A43` | 14.64 | 3.98 | 3.14 (`DESIGN.md` §6) |
| On ink | GitHub dark `#0D1117` | 18.92 | 5.14 | 4.06 (computed) |

Graphics need 3:1; every pair passes. Mono versions go on any field where their single colour reaches 3:1.

### 2.4 Misuse (never)
1. **A green check.** Green is the product's "verified" state; the mark would read as a status.
2. **A seal, shield, badge, rosette or stamp around it.** It would read as a certification.
3. **A "P", "Pay", PayPal's name or any PayPal asset.** It would imply affiliation.
4. **Swapped sides.** Seller coral is left, buyer teal right, matching the header rule; a swap breaks the meaning.
5. **Closed, joined or overlapping brackets.** A box reads as a checkbox; overlaps recall a card network's mark.
6. **A bracket alone or the check alone as an icon.** A lone check reads as a state.
7. **Stretching, rotation, outlines, shadows, glow, gradients or animation.** The mark is flat and still, like the product.
8. **The wordmark in caps, title case, italic or another typeface.**
9. **The full-colour mark on coral, teal, mid-tones or busy screenshots.** Use paper, white or ink behind it.
10. **The wrong size file.** The 16 px variant above 23 px looks heavy; the standard mark below 24 px blurs.

**Rejected directions:** overlapping circles (resembles a card network's mark); overlapping documents (reads as the "copy" icon); the earlier sheet-and-check (generic).

---

## 3. Colour

### 3.1 Brand palette

| Colour | Hex | Role |
|---|---|---|
| Ink | `#102A43` | Text, header, primary button, the check, dark fields |
| Seller coral | `#E4572E` | Seller bracket, left half of the split rule. Graphics, never text |
| Seller coral text | `#C2410C` | When coral must be text (5.18 on white, 4.75 on paper); never on ink (2.83) |
| Buyer teal | `#12827A` | Buyer bracket, right half of the split rule, annotation discs; text on white (4.67) |
| Paper | `#F7F5F0` | Marketing canvas |
| Slate | `#486581` | Secondary text, captions, footers |

### 3.2 Product tokens (shipped; `app.css` lines 6–52, `DESIGN.md` §2)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--text` / `--text-muted` | `#102a43` / `#486581` | `#e8ebee` / `#aab2bb` | Body (ink) / secondary (slate) |
| `--accent` / `--on-accent` | `#102a43` / `#ffffff` | `#9fb3c8` / `#0b1a2a` | Primary button fill / label |
| `--accent-text`, `--focus` | `#0e6e67` | `#7dd3c8` | Links, focus ring: a darker teal for text on the page |
| `--header-bg` / `--header-muted` | `#102a43` / `#bcccdc` | `#0b1a2a` / `#b6c4d1` | Ink header / its secondary text |
| `--brand` | `#e4572e` | `#f07a55` | Seller coral: mark and left half of the header rule, nothing else |
| `--brand-mark` | `#12827a` | `#3db5a8` | Buyer teal: mark and right half of the header rule, nothing else |
| `--bg` / `--surface` | `#f5f4f0` / `#ffffff` | `#121518` / `#1b1f24` | Page / cards (the page is a shade darker than marketing paper) |

### 3.3 State colours stay separate from brand colours

| State | Glyph | Light bg / fg / edge | Words |
|---|---|---|---|
| Verified, confirmed by PayPal | ✓ | `#e4f2e8` / `#14532d` / `#1e7a3f` | "Verified", "Sent to PayPal" |
| Not known yet, needs review | ! | `#fcefd0` / `#6b4300` / `#9a6200` | "Present, but Proofie cannot verify" |
| Does not match, missing, blocked | ✕ | `#fae4e1` / `#8a1c14` / `#b3261e` | "Does not match", "Missing" |
| Neutral, sent but not read back | i | `#e6eef4` / `#102a43` / `#486581` | "Approved, not sent" |

**Coral versus the red error.** Coral `#E4572E` and the error edge `#B3261E` differ by 1.77:1 (computed), so hue is not trusted to separate them. The rules do: coral never appears on a button, chip, banner, check row, annotation or text, and always appears paired with teal; red always comes with ✕, its words, its tinted fill and a 6 px edge. A test that reads `app.css` enforces this in the product.

### 3.4 Contrast (text 4.5:1; large text and graphics 3:1)

| Pair | Ratio | Source | Use |
|---|---|---|---|
| Ink on white / paper | 14.64 / 13.44 | computed | Any text |
| Slate on white / paper | 6.08 / 5.58 | computed | Secondary text |
| Teal `#12827A` on white / paper | 4.67 / 4.28 | computed | Text on white; large text and graphics on paper (body text on paper: `#0E6E67`, 5.59) |
| Coral text `#C2410C` on white / paper | 5.18 / 4.75 | computed | Text, sparingly |
| Coral `#E4572E` on white / paper | 3.68 / 3.38 | computed | Graphics, never text |
| White / `#D9E2EC` / `#9FB3C8` on ink | 14.64 / 11.18 / 6.80 | computed | Text on ink |
| Header text / secondary on ink | 14.64 / 8.93 | `DESIGN.md` §6 | Product header |
| White on ink at 88 % over white (`#2D445A`) | 10.07 | computed | Video lower third |

---

## 4. Typography

**Product: system stack, no web fonts by design.** The app runs offline on the seller's machine. Text: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`. Mono (ids, tracking numbers, fingerprints): `ui-monospace, "Cascadia Code", Consolas, "Liberation Mono", monospace`.

**Marketing surfaces** (gallery, thumbnail, social preview, video cards) may use **Inter** (SIL Open Font License 1.1) if available, falling back to `"Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif`. Fonts are rendered into images and never enter the repository. Use tabular figures; print money and dates exactly as the product does ("15.00 USD", "28 Oct 2026, 2:30 pm (Mexico City time)").

| Surface | Role | Size / line height | Weight |
|---|---|---|---|
| Product UI (`app.css`, base 16 px) | h1 / h2 / h3 | 28 / 22 / 16 px, 1.25 | bold |
| | Body / small | 16 / 14 px, 1.5 | 400 |
| | Header wordmark | 18 px, −0.01em, lowercase | 700 |
| Gallery 1800 × 1200 | Headline (≤ 8 words) / sub-line (≤ 14 words) | 64/76 / 32/44 px | 700 / 400 |
| | Callout numeral / caption bar | 28 / 22 px | 700 / 400 |
| Video 1920 × 1080 | Wordmark / card line / secondary | 120 / 48/60 / 36/48 px | 700 / 500 / 400 |
| | Lower third / sandbox tag | 36 / 28 px | 600 |
| | Captions (≤ 42 characters a line, 2 lines) | 40 px | 500 |

---

## 5. Voice and tone

**Principles.** Seller, case, action, in that order. Say what is true today, in the sandbox. One term per concept; active voice, the actor as subject. Numbers come from code, never rounded. Name a limit once, next to the claim it limits. 15–20 words a sentence, 25 at most; sentence-case headings; no exclamation marks, rhetorical questions, emoji or ALL CAPS.

| Use | Avoid |
|---|---|
| dispute, statement, package, approval, fingerprint (a SHA-256 hash), read-back | ticket, bundle, sign-off, signature, seal, "evidence id" |
| prepares, drafts, checks, reads, shows its source | automatically sends, autonomous, autopilot |
| answer a dispute, respond | fight, beat, win, defeat |
| "Verified" for a passing code check. "Confirmed by PayPal" is the product's own state label on the sent page: PayPal received the evidence and the read-back lists it. In our copy (Devpost, video, gallery, captions) say "received" and "read back", never "confirms" | "verified" for the model, a document's authenticity or PayPal's decision |
| PayPal's record, the PayPal sandbox | PayPal partner, PayPal-approved, official, certified |
| a person reviews every send | AI-powered, smart, magic, seamless, guarantee |

**Talking about AI, three layers in this order.** What it does: "The local model reads your documents and drafts statements." Who decides: "Code checks the numbers, dates and IDs against PayPal's record. You review and approve the exact package." Its limit, once: "It is an assistant. A person approves every send." Label for drafted content: "Drafted by the local model. You review and edit it." Never "AI checked" or "AI verified".

**Sensitive lines.**
- Accept-claim: "Proofie never accepts a claim on its own. Accepting is the seller's last option, behind a check page of its own, and no assistant has a tool for it."
- Scope: "Built and tested on the PayPal sandbox, with synthetic documents."
- Approval: "Nothing reaches PayPal until the seller approves the exact package. Approving sends nothing; sending is a separate click."
- Hash: "A hash shows the package did not change after approval; it does not prove the documents are authentic."

**Claim rules for public text.** Never "only", "first", "nobody", "unlike others" or "the best"; no win rate, success rate, recovered money, time saved or speed claim; no reliability or accuracy claim about the model, and no model percentage in the video or a pitch. Name no rivals. Write "PayPal sandbox build", not "sandbox only".

**Words we use** (Jonás, 2026-11-02; marketing copy: Devpost, gallery, video, social preview).
- PayPal's side: "received" and "read back". Never "accepted", "approved" or "confirms" in our copy; "approve" belongs to the seller.
- The header tag reads "Proofie prepares - you decide".
- "Evidence", not "proof".
- Each required disclaimer (the sandbox tag, the non-affiliation line, the limits) appears once per surface.
- No stacked caveats: a limit sits next to the claim it limits, once.

| Context | English | Español (México, "tú") |
|---|---|---|
| Approval | Approving sends nothing. Sending is a separate step. | Aprobar no envía nada. Enviar es un paso aparte. |
| Mismatch | Does not match: your receipt says 03100; PayPal's record says 06600. | No coincide: tu recibo dice 03100; el registro de PayPal dice 06600. |
| AI | The local model drafts. You review and approve every send. | El modelo local hace un borrador. Tú revisas y apruebas cada envío. |
| Scope | Runs in the PayPal sandbox, for item-not-received disputes. | Funciona en el sandbox de PayPal, para disputas por artículo no recibido. |
| Independence | Independent project. Not affiliated with or endorsed by PayPal. | Proyecto independiente. No está afiliado a PayPal ni cuenta con su respaldo. |

PayPal receives the English statements; Spanish text serves the seller's own review.

---

## 6. Applications

### 6.1 Product surfaces
Inventory from the product team (2026-11-02). Every page that extends the shared base template inherits the header, so one rule covers the inbox, the case workspace, the judge pages, the walkthrough, the hub, the presentation page, the access-code page and the error pages.

| Surface | Spec |
|---|---|
| Header (every base-template page) | Ink field; padding 8 × 16 px (8 × 24 px from 640 px wide, 4 px top and bottom under 640 px); row at least 44 px; 4 px bottom rule split 50/50, coral left, teal right; sticky from 40 em. Mark 24 × 24 px (`static/brand/proofie-mark-on-ink.svg`), 8 px gap, wordmark 18 px bold, lowercase by CSS (the HTML text stays "Proofie"). Environment tag "PayPal sandbox · Proofie prepares - you decide", 14 px `#bcccdc` (8.93 on ink). Dark mode: field `#0b1a2a`, coral `#f07a55`, teal `#3db5a8`. |
| Case workspace pages | The same ink header and split rule; rail top borders use the ink token, never a separate navy. |
| Favicon and touch icon | SVG `static/brand/proofie-mark.svg`; PNG fallback `favicon-32.png` (32 × 32, transparent, from the small-size geometry); `apple-touch-icon.png` (180 × 180, ink field, mark about 76 % of the box). |
| Link preview (Open Graph, Twitter) | `static/brand/proofie-social-1280x640.png` (1280 × 640, under 1 MB), absolute URL in the base template head. |
| Evidence package print view (the seller's local copy) | A4, 18 mm margins, 174 mm content width, Arial 11 pt. First-sheet header: one-colour ink mark about 10 mm (`proofie-mark-mono-ink.svg`) and the text "Proofie"; colours from the ink tokens. |
| Package insert (printed and placed in the parcel; the buyer reads it) | Printed with the same base template: a print stylesheet hides navigation, menu and sandbox tag and prints the header in ink on white (browsers drop backgrounds, so a white wordmark would vanish). |
| Buyer status page | No header; a small one-colour mark in the footer, no new words. |
| Page titles | One separator style: "Page - Proofie" (a spaced hyphen, the product name last). |
| MCP server | The client id stays `proofie` (renaming would break registered clients); its instructions text carries no branding. |

**Rule: what PayPal receives carries no Proofie brand.** Statements, notes and attached files sent to PayPal are the seller's evidence, not a tool document: no mark, no wordmark, no footer.

### 6.2 Devpost thumbnail, 1200 × 800 (3:2)
Source `proofie-devpost-thumbnail.svg`: paper field, mark and wordmark top-left, headline 54 px bold ink, supporting lines 34 px slate, 20 px split rule along the bottom. Export PNG under 5 MB; at 300 px wide the mark and name must still read.

### 6.3 Gallery images, 3:2 (1800 × 1200)

| Zone | Box (x, y, w, h) | Content |
|---|---|---|
| Grid | — | 96 px margins left, right, top; 12 columns, 24 px gutters in 1608 px |
| Headline band | 96, 96, 1608, 168 | Headline 64 px ink; sub-line 32 px slate |
| Screenshot | 96, 304, 1608, 744 | Real screen on a white mat, 1 px `#D5D2CA` border, 6 px radius, no shadow or device frame |
| Split rule | 0, 1098, 1800, 6 | Coral left half, teal right half |
| Caption bar | 0, 1104, 1800, 96 | Ink; mark on ink 40 px at x = 96; "Proofie · PayPal sandbox build" 22 px white; non-affiliation line right-aligned, 22 px `#D9E2EC` |

**Screenshots:** real build, light theme, 1440 px window, 100 % zoom, cropped to the decisive region; no browser chrome, PayPal page or logo, third-party window, personal data or retouched state. Smallest product text at least 20 px tall.

**Annotations:** at most three. Numbered disc 48 px in teal `#12827A`, white 28 px bold numeral (4.67); 3 px ink leader line; 3 px ink outline at 4 px offset around the element. Discs sit outside the element, never on a state tag. **Never coral, red, amber or green**: an annotation must not read as an error or a state. The words go in the caption and alt text.

### 6.4 Social preview, 1280 × 640
Source `proofie-social-preview.svg`: ink field, mark on ink and wordmark 112 px white, two lines 34 px `#D9E2EC`, non-affiliation footer 24 px `#9FB3C8`, 14 px split rule along the bottom. Content stays 64 px inside the side edges. Export PNG under 1 MB.

### 6.5 Video, 1920 × 1080
Text inside title-safe (96 px left and right, 54 px top and bottom); nothing essential outside action-safe (67 px, 38 px). Captions centred, bottom at y = 1026.

| Card | Spec |
|---|---|
| Title card (≤ 2 s) | Ink; mark on ink 240 px centred; wordmark 120 px white below; one line 48 px `#D9E2EC`; 12 px split rule along the bottom |
| Lower third | Ink strip at 88 % opacity, 84 px tall, left x = 96, bottom y = 846 (clear of captions); 4 px split rule on its lower edge; white 36 px weight 600, 28 px padding |
| Sandbox tag | "PayPal sandbox · synthetic demo data", 28 px, same strip, top-right inside title-safe; never over a product state label |
| End card | Stacked lockup on ink; closing line 36 px; repository URL in mono 36 px once public; non-affiliation line 28 px `#D9E2EC`; no third-party logos |

Straight cuts; crop-ins up to 150 % for legibility; no zoom-blur, glow or music bed.

### 6.6 README header
Use PNG exports of the two lockups (light and on ink) inside a `<picture>` element that switches on `prefers-color-scheme`: an SVG shown as an image cannot load fonts, so the `<text>` lockups would fall back to whatever font the reader has until the wordmark is outlined.
Then the one-liner and the non-affiliation line. No headline baked into an image.

---

## 7. Asset checklist and summary

| Asset | Size | Source | State |
|---|---|---|---|
| Mark, all versions; lockups | 64-unit / 260 × 64 SVG | `proofie-mark*.svg`, `proofie-lockup*.svg` | Shipped |
| Favicon and touch icon | SVG, 32 PNG, 180 PNG | `static/brand/` | Shipped to the product folder; linked by the product |
| Social preview / Devpost thumbnail | 1280 × 640 / 1200 × 800 PNG | Their SVGs | Shipped: `static/brand/proofie-social-1280x640.png`, `docs/brand/proofie-devpost-thumbnail.png` |
| README lockups | 520 × 128 PNG (2x) | `proofie-lockup*.svg` | Shipped: `proofie-lockup.png`, `proofie-lockup-on-ink.png` |
| Gallery images | 1800 × 1200 PNG | Template 6.3 | To produce |
| Video title, lower third, tag, end card | 1920 × 1080 PNG | Spec 6.5 | To produce |
| README header | 260 × 64 | Lockups | Spec 6.6 |

Before any export: no claim-rule words; no PayPal or third-party mark; non-affiliation line where required; contrast pairs from 3.4; every state still reads in greyscale; every word in the brand SVGs is outlined in Inter (InterDisplay Bold for the wordmark and headlines, Inter Regular for lines; SIL Open Font License 1.1), so they render the same on any machine.

| Do | Don't |
|---|---|
| Seller coral left, buyer teal right, ink check | Swapped sides, green check, lone check as an icon |
| Coral and teal as a pair, in the mark and the split rule | Coral on buttons, chips, banners, annotations or text |
| Teal discs and ink lines for annotations | Red, amber, green or coral annotations |
| States with glyph, words and their own colour | Colour as the sole cue; brand colours as states |
| System fonts in the app; Inter or fallback on marketing | Web fonts in the app |
| "Proofie" in text, "proofie" in the wordmark | Caps, italics or another typeface for the wordmark |
| PayPal named plainly, with the non-affiliation line | PayPal logos, colours, badges or endorsement wording |
| "A person approves every send" | Claim-rule words, win rates, model reliability |
