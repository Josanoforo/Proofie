# Third-party components

How this list was made (2026-10-05):
- **Versions** come from `requirements.lock.txt`, the full lock (54 packages).
- **Licenses** were read once from the installed package metadata (`importlib.metadata`: `License-Expression`, then `License` or the trove classifiers) in a reference environment built from that lock.
- **Ollama and the model:** licenses were read at their source pages on the same date.

Nothing below is vendored or copied into this repository, with one exception: the PayPal OpenAPI schema extracts described in their own section. Every package is installed from PyPI by `pip`, and every model is downloaded by the user.

This project's own license is Apache-2.0 (see `LICENSE`). Every license below is permissive and compatible with distributing this project under Apache-2.0. The one weak-copyleft license, MPL-2.0 (`certifi`), applies only to that package's files, which we use unmodified and do not redistribute.

## Python packages (all 54 in `requirements.lock.txt`)

"Role" says why each package is there:
- **runtime**: needed to run the app; listed in `requirements-app.txt` (`pilot/requirements.txt` only includes that file);
- **dev**: tests and UI checks only; listed in `requirements-dev.txt`;
- **dep**: installed because another package requires it;
- **Windows**: needed only on Windows.

| Package | Version | License (package metadata) | Role |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | dep |
| annotated-types | 0.8.0 | MIT | dep |
| anyio | 4.15.1 | MIT | dep |
| attrs | 26.1.0 | MIT | dep |
| certifi | 2026.7.22 | MPL-2.0 | dep (unmodified) |
| cffi | 2.1.1 | MIT-0 | dep |
| click | 8.5.0 | BSD-3-Clause | dep |
| colorama | 0.4.6 | BSD (classifier) | dep |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | dep |
| fastapi | 0.142.2 | MIT | runtime |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | dep |
| h11 | 0.16.0 | MIT | dep |
| httpcore | 1.0.9 | BSD-3-Clause | dep |
| httpcore2 | 2.13.1 | BSD-3-Clause | dep |
| httptools | 0.8.0 | MIT | dep |
| httpx | 0.28.1 | BSD-3-Clause | runtime |
| httpx2 | 2.13.1 | BSD-3-Clause | dep |
| idna | 3.20 | BSD-3-Clause | dep |
| iniconfig | 2.3.0 | MIT | dep |
| Jinja2 | 3.1.6 | BSD (classifier) | runtime |
| jsonschema | 4.26.0 | MIT | dep |
| jsonschema-specifications | 2025.9.1 | MIT | dep |
| MarkupSafe | 3.0.4 | BSD-3-Clause | dep |
| mcp | 2.3.0 | MIT | runtime (MCP server) |
| mcp-types | 2.3.0 | MIT | dep |
| opentelemetry-api | 1.45.0 | Apache-2.0 | dep |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | dep |
| pillow | 12.3.0 | MIT-CMU (HPND) | runtime |
| playwright | 1.63.0 | Apache-2.0 | dev (UI checks; it downloads a Chromium build to the user's profile, not redistributed) |
| pluggy | 1.6.0 | MIT | dep |
| pycparser | 3.0 | BSD-3-Clause | dep |
| pydantic | 2.13.5 | MIT | dep |
| pydantic_core | 2.46.5 | MIT | dep |
| pyee | 13.0.1 | MIT | dep |
| Pygments | 2.21.0 | BSD-2-Clause | dep |
| PyJWT | 2.15.1 | MIT | dep |
| pypdfium2 | 5.14.0 | BSD-3-Clause, Apache-2.0, plus the licenses of its bundled PDFium | runtime |
| pytest | 9.1.1 | MIT | dev |
| pytest-timeout | 2.4.0 | MIT | dev |
| python-dotenv | 1.2.4 | BSD-3-Clause | runtime |
| python-multipart | 0.0.32 | Apache-2.0 | runtime |
| pywin32 | 312 | PSF | Windows |
| PyYAML | 6.0.3 | MIT | dep |
| referencing | 0.37.0 | MIT | dep |
| rpds-py | 2026.9.1 | MIT | dep |
| sse-starlette | 3.5.0 | BSD-3-Clause | dep |
| starlette | 1.7.0 | BSD-3-Clause | dep |
| truststore | 0.10.4 | MIT | dep |
| typing-inspection | 0.4.4 | MIT | dep |
| typing_extensions | 4.16.0 | PSF-2.0 | dep |
| tzdata | 2026.5 | Apache-2.0 | runtime |
| uvicorn | 0.54.0 | BSD-3-Clause | runtime |
| watchfiles | 1.3.0 | MIT | dep |
| websockets | 17.2 | BSD-3-Clause | dep |

## Local AI runtime and model (optional, not redistributed)

| Component | Version / identifier | License | Source (read 2026-10-05) |
|---|---|---|---|
| Ollama | 0.35.1 (the version on the development machine) | MIT | <https://github.com/ollama/ollama/blob/main/LICENSE> |
| `qwen3.5:9b` (default model) | 6,550,825,550 bytes, digest `56671c2ab938...` | Apache-2.0 | the license shown on <https://ollama.com/library/qwen3.5:9b> |
| `qwen3:14b` (comparison model, used only in our evaluation) | 9,276,198,565 bytes | Apache-2.0 | <https://ollama.com/library/qwen3:14b> (read 2026-10-04) |

The user downloads the model weights with `ollama pull`; they are not part of this repository.

## PayPal APIs

All calls go to `https://api-m.sandbox.paypal.com` through `pilot/client.py`, which is fixed to the sandbox. No PayPal SDK is bundled.

Use of these APIs is subject to PayPal's developer terms, in particular the PayPal Developer Agreement (<https://www.paypal.com/us/legalhub/paypal/xdeveloper-full>). Following its confidentiality section, this repository publishes no PayPal API response bodies. What we ran is described in `EVIDENCE-INDEX.md` by hashes only.

Endpoints used:
- `POST /v1/oauth2/token`: authentication;
- `GET /v1/customer/disputes` and `GET /v1/customer/disputes/{id}`: list and detail;
- `POST /v1/customer/disputes/{id}/provide-evidence`: send evidence, then reconcile by GET;
- `POST /v1/customer/disputes/{id}/send-message` and `POST /v1/customer/disputes/{id}/make-offer`: a message or a refund offer to the buyer, each sent once after the seller approves the exact request, then read back by GET (`copilot/features/resolution/`);
- `GET /v1/reporting/transactions` (Transaction Search) and `GET /v1/shipping/trackers` (Tracking): read for context (`copilot/core/context.py`);
- `POST /v1/shipping/trackers` (Tracking): register a tracker at the sale, once after the seller approves the exact request, then read back (`copilot/features/fulfilment/`); the setup script `tools/sandbox_seed.py` also adds one DEMO tracker to its first test sale;
- `POST /v1/notifications/verify-webhook-signature` (Webhooks): verifies a webhook event with PayPal before it is trusted; called only when the webhook receiver is switched on, which the hosted copy's Blueprint does and the local app does not (`copilot/features/webhooks/verify.py`);
- `POST /v1/customer/disputes/{id}/accept-claim`: accept a claim as the seller's last option, sent once after the seller types the amount phrase and approves the exact request, then read back by GET (`copilot/features/accept_claim/`);
- `POST /v1/customer/disputes/{id}/appeal`: the appeal call, on, sent once after the seller approves the exact request and not exercised against PayPal (`copilot/features/outcome/appeal.py`);
- `POST /v1/customer/disputes/{id}/escalate`: the sandbox judge re-arm, through the link PayPal lists on the case (`copilot/features/judge_pool/service.py`, `tools/sandbox_dispute_state.py`);
- `POST /v1/customer/disputes/{id}/adjudicate` and `POST /v1/customer/disputes/{id}/require-evidence`: state changes the sandbox offers for testing, in the helper `tools/sandbox_dispute_state.py`;
- `POST /v1/customer/disputes/{id}/provide-supporting-info`: one supporting note on the card-route case (`tools/sandbox_card.py`);
- `POST /v2/customer-support/process-chargeback`: the sandbox chargeback call that opens the card-route dispute, and `GET /v2/payments/captures/{id}`: read the capture of the test sale (`tools/sandbox_card.py`, `tools/sandbox_vault.py`);
- `GET /v3/vault/setup-tokens/{id}` and `POST /v3/vault/payment-tokens`: the one-time test-card setup (`tools/sandbox_vault.py`);
- `GET /v1/notifications/webhooks-events` (`copilot/features/events/evlog.py`) and `POST /v1/notifications/webhooks` (`tools/register_webhook.py`): the webhook event list and the address registration;
- `GET /v1/shipping/trackers/{id}`: read a tracker for context (`copilot/core/context.py`);
- `POST /v2/checkout/orders`, `POST /v2/checkout/orders/{id}/capture` and `GET /v2/checkout/orders/{id}`: create a sandbox test payment (`tools/sandbox_orders.py`, used in the judge guide).

## PayPal OpenAPI schema extracts

`copilot/features/conformance/fixtures/` holds five minimal extracts of PayPal's published OpenAPI documents. They are subsets kept for offline contract checks: the conformance page and its tests validate the requests that Proofie's own code builds, and synthetic response fixtures, against them, with no network call. Each extract keeps only the parts the checks need, listed as JSON pointers in its own `provenance` block, with descriptions, titles, examples, external links and `x-` annotations left out and every validation keyword kept as published. © PayPal; reproduced as published, cut to the parts the checks need.

| File | API and version | Source page (fetched 2026-10-06) | sha256 of the full published document |
|---|---|---|---|
| `disputes.json` | Customer Disputes v1, document version 1.12 | <https://developer.paypal.com/api/customer-disputes/v1/schema.json> | `fee5e04ab606f8961b95744e65c4189fc7c4613e79ba2f944d2deab79e834330` |
| `tracking.json` | Tracking v1, document version 1.10 | <https://developer.paypal.com/api/tracking/v1/schema.json> | `7d6f992eb95d6ab4be23755a00455cd3d4fbcf4da0d2abafe72e934529f5f0f1` |
| `transactions.json` | Transaction Search v1, document version 1.10 | <https://developer.paypal.com/api/transaction-search/v1/schema.json> | `85909abeb33b12e184a11d404beb6494ad51115523e6d534ee26ef865d3f9a6d` |
| `webhooks.json` | Webhooks v1, document version 1.11 | <https://developer.paypal.com/api/webhooks/v1/schema.json> | `a10f9d6d44b4732393bf07001979110e0f6a584efad07e31d0d4ad76d6300ca3` |
| `orders.json` | Orders v2, document version 2.36 | <https://developer.paypal.com/api/orders/v2/schema.json> | `5bcb2242a4158a307dceff74f74352a078777f9afb9b0fb407ce10e392ee96b7` |

The versions and hashes are pinned in `copilot/features/conformance/pins.py`, and each extract repeats them in its `provenance` block (source URL, document version, fetch date, full-document sha256). PayPal also publishes its REST API specifications under the Apache-2.0 license at <https://github.com/paypal/paypal-rest-api-specifications> (checked 2026-10-16); the versions there may differ, and these extracts were cut from the developer.paypal.com documents above, not from that repository.

## Reference material (not code)

`reference/getting-started-with-paypal-sandbox` is a local clone of <https://github.com/paypaldev/getting-started-with-paypal-sandbox>, used for reference only. It is git-ignored and not part of the published tree.
- When checked on 2026-10-04, it held only a `README.md` and one image, with no license file and no code, so no code could have been copied from it.
- Its text was not compared line by line with our docs. Our judge guide only links to it.

## Provenance of project files

- **Code:** `copilot/`, `pilot/` and `tools/` are original code written for this hackathon, with AI assistance (see `docs/AI-USAGE.md`).
- **Data:** the evaluation cases, test fixtures and demo documents are synthetic ("DEMO DATA").
- **Interface assets:** the interface loads no external fonts, scripts or images (`copilot/web/static/` has only `app.css` and `app.js`). The Proofie mark in `docs/brand/` is original to this project and uses no PayPal brand assets.
- **Video tools:** OBS Studio, ffmpeg and Clipchamp (capture and editing; used only as local tools, not distributed; OBS Studio and ffmpeg are GPL). The narration is planned to be synthesised with ElevenLabs from the author's own recorded voice, a hosted service used under its terms; nothing from it is in this repository. The video is planned with no music; this line is confirmed once the video is recorded.
