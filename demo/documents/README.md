# Demo documents

All **DEMO DATA** - fictitious people, addresses and tracking numbers; none resolve with a real carrier.
Derived from `copilot/fixtures/m1`'s own synthetic cases (the checklist test fixtures) so a judge's
real sandbox run and this repository's own test suite exercise the exact same scenarios. Upload any one
of these on a real sandbox dispute's detail page to see the matching behaviour described below.

All five files are written by `tools/make_demo_documents.py` (standard library only, no network, under a second), with dates
relative to the day it runs; the committed files carry the dates of the day they were generated. Run it before uploading, so that
the demo delivery date is later than the sale you created (`docs/JUDGE-RUN.md`, section 4, step 2).

| File | Scenario | What you should see |
|---|---|---|
| `01_coherent_delivery.pdf` | A complete, coherent delivery (`c01_complete`) | Every required field is read from the document. Its dates are relative to the day the generator ran (delivered three days after it). If they still differ from the sale, the check is shown in red and the review asks for a note before approval, instead of blocking (`docs/JUDGE-RUN.md`, section 4, step 4). |
| `02_wrong_order_tracking_and_postal_code.pdf` | A document from another order (`c03_other_order`) | Tracking number (on a sale that carries a tracker) and postal code **conflict** with PayPal's own data; the review blocks approval until the conflict is fixed on the package page (exclude the document or correct the data). |
| `03_in_transit.pdf` | Shipped but not yet delivered (`c05_in_transit`) | An honest partial response is allowed - "in transit" is never treated as a failure. |
| `04_injected_instruction.pdf` | A document carries a prompt injection aimed at an AI reader (`c07_injection`) | In our evaluation runs the model did not follow injected instructions of this kind (an observation, not a guarantee; see `docs/AI-USAGE.md` section 3.4); the checklist treats document text as data and flags `document_integrity` for review. |
| `05_illegible_scan.png` | A document that cannot be read at all (an image; this app never performs OCR) | Every required field it might have answered becomes "illegible", never "missing"; the review screen names the file and asks you to type the values or upload a text PDF. |
