# Design decisions

The product decisions behind Proofie, curated from our internal decision log. The ids are kept, so each entry can be matched to the full record.
- Process decisions (how we organised the work) are left out.
- Figures come from [AI-USAGE.md](AI-USAGE.md).
- Evidence references (`EV-nn`) point to [EVIDENCE-INDEX.md](../EVIDENCE-INDEX.md).

## D-2026-10-04-01: Default local model `qwen3.5:9b`, as an assistant only
- **Decision.** `qwen3.5:9b` through Ollama replaces `qwen3:14b` as the default model. The 14B model stays available for comparison.
- **Why:**
  - **Higher validation rate** on the same 16 cases (3 seeds, 48 runs each): 42/48 against 28/48.
  - **No degenerate outputs.** `qwen3:14b` produced degenerate outputs on adversarial cases; `qwen3.5:9b` did not.
  - **Smaller and with vision.** It is smaller on disk and in VRAM, and it can read images.
  - **Injection resistance:** no difference; in our 12 injection-case runs neither model obeyed an injected instruction (an observation, not a proof of absence of risk).
- **Limit, stated plainly.** Neither model met our own reliability bar: `qwen3.5:9b` passed every validator check in 42 of 48 synthetic runs (88 %) and in 19 of 24 on the heldout set (79 %). So the model is never the source of truth:
  - whether a claim is true is decided by code, against the literal sources;
  - whether the evidence is sufficient is also decided by code;
  - a human approves every send.

## D-2026-10-04-02: A fresh, frozen heldout set for the final evaluation
- **Decision.** The original heldout cases had been run and read case by case, and were later used to diagnose a bug in our validator (deterministic code, not the prompt). So six new heldout cases were written and frozen by hash, and then run **once**, with nothing tuned afterwards. Details: `pilot/eval/EXPOSURE.md`.
- **Why.** Evaluating the final prompt and validator on cases we had already looked at would not be a clean measurement.
- **Results:** [AI-USAGE.md](AI-USAGE.md), section 3.7.

## D-2026-10-05-01: Sanitise identifiers at the source
- **Decision.** The single PayPal client redacts merchant and account identifiers, phone numbers and every address field before anything is logged. It covers both address shapes the app reads (Orders and Transaction Search). Account numbers were added later the same day.
- **Why.** A privacy review found identifiers that the first version did not redact. Fixing the client itself protects every caller, the app included, from that point on.

## D-2026-10-05-02: A deterministic `PayPal-Request-Id` on writes, and a send-once guard of our own
- **Decision.** The request id is derived from the dispute and a hash of the exact content (UUIDv5; for evidence, from the statements and the tracking, with the attached files added only when a send takes over an earlier refused attempt; for a tracker, from the hash of its body). Proofie sends it on its writes and logs it with the call. A retry of the same content after a confirmed rejection therefore reuses the same id.
- **Why.** The same content always carries the same id, so a request can be traced in the log. Proofie does not rely on the header to prevent a duplicate: in the sandbox PayPal did not deduplicate `send-message` by it (a repeated click sent the same id twice and PayPal recorded two seller messages, observed 2026-10-16).
- **Consequence.** The guard is Proofie's own. It refuses a second send of the same approved request before any second POST to PayPal: for a message, an offer or a tracker through a reservation in the local database, taken before the POST (a repeat makes no PayPal call at all); for evidence through the send-attempt registry (a read of the dispute may come first). A definite refusal by PayPal frees the request again; for a message or a tracker, so does a later complete read, at least 10 minutes after the POST, that shows it missing; for evidence, the seller's own check of the Resolution Center lets a changed package go out only when PayPal lists no evidence from the seller, because Proofie sends evidence once per dispute and never when PayPal already lists evidence from the seller. A seller who wants to say something different writes a different text and approves it again. The reservations live in the installation's own database: the hosted demo starts a new database on every start, so they do not survive a restart. Since rc4.3 the message path also reads PayPal's message list before the POST and refuses a message that the list already shows with the same text; one that the list does not show yet can still be approved and sent again while PayPal still offers send-message (evidence is not affected, because PayPal's own record refuses a second send). This entry was updated on 2026-10-17 after the sandbox observation of 2026-10-16, on 2026-10-18 for evidence sent once per dispute, and on 2026-10-20 for the hosted demo's records and the message-list check.

## D-2026-10-05-03: Own palette, no PayPal brand assets
- **Decision.** A navy and blue interface with an orange product accent.
- **Rules that come with it:**
  - **No PayPal brand assets:** no logo, no exact brand colours, no design elements.
  - "PayPal" appears only descriptively (for example "Send evidence to PayPal").
  - The footer says the project is independent and not affiliated with or endorsed by PayPal.
  - Orange is never used for buttons or states. Amber means "not known yet" and must not be confused with the brand.
  - AA contrast is measured by a test.
- **Why.** The official rules reserve the sponsor's marks and designs.

## D-2026-10-05-05: Product name "Proofie"
- **Decision.** "Proofie" replaces the working name everywhere a user sees it, and the MCP server id is `proofie`. The internal Python package keeps its name for now.
- **Limit.** Only a public web search was done, and it found no product with this exact name in its result lists. No trademark search or clearance was made, because this is a pilot project.

## D-2026-10-05-06: An untrusted document cannot support a delivery claim
- **Decision.** A document cannot be the source of a delivery, signature or receipt claim in either of two cases:
  - its text reads like instructions to an AI (the integrity check flags it);
  - it has an illegible field.
- **Exception.** An image the app could not read at all is not untrusted. Its only text is the line the seller confirmed from the picture.
- **Why.** This closes an attack in which an injected phrase in a document was counted as support, without silently removing the ability to use a screenshot the seller confirmed.

## D-2026-10-05-08: PayPal's official MCP, in a composition demo
- **Decision.** In the demo, a seller's assistant (Claude) reads disputes through PayPal's official MCP server and asks Proofie's MCP server to build the sourced evidence file. The seller approves in Proofie's page before any send.
- **What stays the same:**
  - Proofie's own code keeps using the PayPal REST API.
  - An optional in-product path through PayPal's MCP is on hold.
- **Claim limit.** We do not claim that the local model drives MCP tools; that has not been measured.
- **The gap this composition rests on** (`github.com/paypal/agent-toolkit`, `python/paypal_agent_toolkit/shared/tools.py`, read 2026-10-05). The toolkit's dispute tools are exactly `list_disputes`, `get_dispute` and `accept_dispute_claim`; `provide-evidence`, make-offer, appeal and send-message are not exposed. In the demo PayPal's server does what it can (list and read), Proofie does what it cannot (the sourced evidence file, approved by a human), and Proofie's own server exposes accept-claim to no agent.

## D-2026-10-05-11: No PayPal API responses in the public repository
- **Decision.** Under a strict reading of the PayPal Developer Agreement, which treats information received through API calls as confidential, this repository publishes **no PayPal response bodies**, not even sanitised ones.
- **What is published instead:**
  - tests use hand-written synthetic fixtures;
  - what we ran against the sandbox is described in [EVIDENCE-INDEX.md](../EVIDENCE-INDEX.md) by hashes, line counts and our own words.
- **Judges use their own sandbox account.** The same agreement does not allow us to share our API credentials.

## D-2026-10-05-12 (product parts): Safer defaults for the MCP server and for output
- **Decision.** Proofie's MCP server returns no claim or document text by default. A client asks for it explicitly (`include_text=true`).
- **Why.** Whatever an MCP tool returns goes to the client's model provider, so the default exposes as little as possible.

## D-2026-11-01-B3: accept-claim stays available to the seller
- **Decision.** Accepting the claim stays in Proofie as the seller's last-resort option (`COPILOT_FEATURE_ACCEPT_CLAIM`): offered while PayPal's fresh read shows the action, never suggested, on a check page of its own, typed amount phrase, hash-bound approval, separate send. The assistant still has no such tool.
