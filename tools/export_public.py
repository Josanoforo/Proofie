"""Public export builder for a frozen Git source.

  python tools/export_public.py DEST --commit <commit-ish> --private-terms FILE [--repo ROOT] [--evidence-dir DIR]
  python tools/export_public.py --counts-only --commit <commit-ish> [--repo ROOT]

How it works:
- ONE commit. The ref is resolved to a full 40-hex SHA and every byte is read from git objects (`git ls-tree`,
  `git cat-file`): the working tree is never read, so uncommitted changes can not leak in or out.
- DEFAULT DENY. A tracked file is published only if an INCLUDE rule matches and no EXCLUDE rule does (EXCLUDE
  wins); anything that matches no rule is excluded and counted under the rule `default-deny`.
- NO PAYPAL RESPONSE BODY IS PUBLISHED, raw or sanitised (D-2026-10-05-11, PayPal Developer Agreement s.11):
  `evidence/**` and `pilot/fixtures/recorded/**` are EXCLUDED. The sanitiser path (SANITIZED, which loads
  `sanitize()` from `pilot/client.py` AT THE EXPORTED COMMIT) stays in the tool, EMPTY and unused, for a later and
  explicit decision.
- EVIDENCE-INDEX.md is GENERATED instead: per private evidence file a sha256, a line count and a byte count,
  and per claim of `docs/evidence-claims.json` the sha256 of the cited lines and our own wording. All hashes and
  counts are over the GIT BLOB bytes at the commit (LF), never a working-tree copy (CRLF would change them). No
  evidence content is copied; the generator fails if a claim cites a missing file or a line out of range, or if a
  claim's wording reproduces a cited line. A claim may cite any tracked file that the rules EXCLUDE (EV-06);
  `"lines": null` means the whole file. docs/evidence-claims.json is REQUIRED. `--evidence-dir` reads the files from an external directory instead
  (hashed exactly as stored; the manifest records it only as the literal `external`).
- MANIFEST.json: deterministic (relative POSIX paths, source_commit, rules_version, tool_sha256, a sha256 per file;
  no timestamps, no absolute paths, no user or host names). It names NO excluded path: `excluded` holds only a
  count, a count per rule and the sha256 of the sorted "path<TAB>rule" lines. The full list is written to
  `<DEST>.EXCLUDED.json` NEXT TO the export directory (like the scan report), never inside it, so it can not reach a
  ZIP of the tree.
- FINAL SCAN that BLOCKS: every PATTERNS/DETECT_ONLY_PATTERNS entry of `tools/redact.py` AT THE COMMIT, plus
  identifier keys carrying a value (merchant_id, payer_id, account_id, account_number, email_address), e-mail
  addresses, local paths, credential shapes and private terms read from a file OUTSIDE the repository (argument
  --private-terms or the environment variable named in PRIVATE_TERMS_ENV). Without that file the export FAILS
  CLOSED. The terms never appear in this tool, its tests, the output or the report. Exceptions are listed by path
  path, kind AND line in ALLOWED_EXCEPTIONS (an entry that matched nothing is reported as stale), plus
  the synthetic-data path globs in SCAN_EXCEPTIONS, each with its justification. The report
  `<DEST>.REDACT-SCAN.txt` (path, line, pattern name, never a value) is written NEXT TO the export directory, in its
  parent folder, not inside it.
- T14 (warning, not a block): a hex token in an exported .md file that resolves to a commit of the private repo.
- T15 (warning): a plain-text path that names an excluded file, including the old docs/ locations that
  internal/PATH-MAP.md lists (the briefs and logs the files moved from).
- T18 (warning): build-round, work-item, finding and review ids, role and process words, the owner's first name,
  private commit ids and unresolved decision ids in the comments, docstrings and prose of a published text file,
  with an explicit, reasoned allowlist (T18_ALLOWLIST). T16 and T17 are blocking gates.

Exit codes: 0 exported and clean; 1 the final scan blocked (the tree is left for inspection and has NO manifest, so
it is not releasable); 2 the export could not be built (bad input, an evidence claim that does not match its file,
a missing terms file ...), nothing is written.

This tool only reads the repository. It publishes, pushes and uploads nothing.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

SCHEMA = "proofie-export/1"
CLAIMS_SCHEMA = "proofie-evidence-claims/1"
PRIVATE_TERMS_ENV = "PROOFIE_PRIVATE_TERMS"
REPORT_SUFFIX = ".REDACT-SCAN.txt"          # <DEST>.REDACT-SCAN.txt, beside the export folder
EXCLUDED_SUFFIX = ".EXCLUDED.json"          # <DEST>.EXCLUDED.json, the private full list, beside the export folder
MIN_TERM_LENGTH = 3
INDEX_NAME = "EVIDENCE-INDEX.md"
CLAIMS_PATH = "docs/evidence-claims.json"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


# Publication is an explicit source edit after the human gate; runtime flags cannot change it.
PUBLISHED_NAMESPACES: list[str] = ['accept_claim', 'account_cliff', 'accounting_line', 'active_approval', 'appeal_workspace', 'bilingual', 'buyer_note', 'buyer_page', 'call_script', 'carrier_directory', 'carrier_required', 'case_history', 'case_reentry', 'claims', 'clauses', 'conformance', 'deadlines', 'declared_limits', 'delivery_facts', 'demo_documents', 'do_nothing_path', 'draft', 'evaluation_workbench', 'events', 'evidence_dossier', 'explainable_queue', 'first_dispute_guide', 'fulfilment', 'judge_as_buyer', 'judge_pool', 'judge_walkthrough', 'ledger', 'lessons_exchange', 'local_autopilot', 'local_demo_hub', 'lost_package_paths', 'mcp_local_contracts', 'mock_adjudicator', 'negotiation_drafts', 'offline_market_mode', 'outcome', 'package_insert', 'package_print', 'presentation_mode', 'read_aloud_review', 'replay', 'resolution', 'sale_lesson', 'sale_readiness', 'season_prep', 'second_opinion', 'ship_moment_capture', 'ship_notice', 'status_link', 'third_party_statement', 'timeline', 'trade_profiles', 'transit_delivered_guide', 'triage', 'two_clocks', 'webhooks']
KNOWN_NAMESPACES = (
    "accept_claim", "account_cliff", "accounting_line", "active_approval", "appeal_workspace", "bilingual", "buyer_note", "buyer_page",
    "call_script", "carrier_directory", "carrier_required", "case_history", "case_reentry", "claims", "clauses", "conformance", "deadlines",
    "declared_limits", "delivery_facts", "demo_documents", "do_nothing_path", "draft", "evaluation_workbench", "events",
    "evidence_dossier", "explainable_queue", "first_dispute_guide", "fulfilment", "judge_as_buyer", "judge_pool", "judge_walkthrough", "ledger",
    "lessons_exchange", "local_autopilot", "local_demo_hub", "lost_package_paths", "mcp_local_contracts", "mock_adjudicator", "negotiation_drafts",
    "offline_market_mode", "outcome", "package_insert", "package_print", "presentation_mode", "read_aloud_review", "replay",
    "resolution", "sale_lesson", "sale_readiness", "season_prep", "second_opinion", "ship_moment_capture", "ship_notice", "status_link",
    "third_party_statement", "timeline", "trade_profiles", "transit_delivered_guide", "triage", "two_clocks", "webhooks",
)

SEED_DEMO_ADDRESS_CONTEXT = {
    "path": "tools/sandbox_seed.py", "binding": "SHIPPING_ADDRESS.address_line_1",
    "sha256": "00973e7e9f101b2d8d03b3d16e421a4e5c6d4e933555f9d05771a7fea1601de3",
    "marker": "# DEMO DATA - fictitious address and name;",
}

NAMESPACE_DEPENDENCIES = {
    'accept_claim': frozenset(['resolution']),
    'account_cliff': frozenset([]),
    'active_approval': frozenset(['claims', 'carrier_required']),
    'bilingual': frozenset([]),
    'carrier_required': frozenset([]),
    'case_history': frozenset([]),
    'case_reentry': frozenset([]),
    'claims': frozenset([]),
    'clauses': frozenset([]),
    'conformance': frozenset(['account_cliff', 'fulfilment', 'outcome', 'resolution', 'webhooks']),
    'deadlines': frozenset(['events']),
    'demo_documents': frozenset([]),
    'delivery_facts': frozenset(['draft']),
    'draft': frozenset(['resolution']),
    'events': frozenset([]),
    'fulfilment': frozenset([]),
    'ledger': frozenset([]),
    'outcome': frozenset(['fulfilment', 'resolution']),
    'package_print': frozenset([]),
    'replay': frozenset([]),
    'resolution': frozenset(['account_cliff']),
    'timeline': frozenset([]),
    'triage': frozenset(['claims', 'resolution', 'clauses']),
    'webhooks': frozenset([]),
    'appeal_workspace': frozenset([]),
    'evaluation_workbench': frozenset([]),
    'evidence_dossier': frozenset([]),
    'explainable_queue': frozenset(['webhooks']),
    'judge_walkthrough': frozenset([]),
    'local_autopilot': frozenset(['resolution']),
    'local_demo_hub': frozenset(['local_autopilot', 'mcp_local_contracts']),
    'mcp_local_contracts': frozenset([]),
    'negotiation_drafts': frozenset(['local_autopilot', 'resolution']),
    'sale_readiness': frozenset(['fulfilment']),
    'accounting_line': frozenset(['account_cliff', 'ledger', 'outcome']),
    'buyer_note': frozenset(['carrier_directory', 'fulfilment', 'negotiation_drafts', 'sale_readiness', 'trade_profiles']),
    'buyer_page': frozenset(['negotiation_drafts', 'resolution']),
    'call_script': frozenset([]),
    'carrier_directory': frozenset([]),
    'declared_limits': frozenset([]),
    'do_nothing_path': frozenset([]),
    'first_dispute_guide': frozenset([]),
    'judge_as_buyer': frozenset([]),
    'judge_pool': frozenset([]),
    'lessons_exchange': frozenset(['sale_lesson', 'trade_profiles']),
    'lost_package_paths': frozenset(['carrier_directory', 'fulfilment']),
    'mock_adjudicator': frozenset([]),
    'offline_market_mode': frozenset([]),
    'package_insert': frozenset(['carrier_directory', 'fulfilment', 'lessons_exchange', 'trade_profiles']),
    'presentation_mode': frozenset(['evaluation_workbench']),
    'read_aloud_review': frozenset([]),
    'sale_lesson': frozenset(['outcome']),
    'season_prep': frozenset(['carrier_directory']),
    'second_opinion': frozenset([]),
    'ship_moment_capture': frozenset([]),
    'ship_notice': frozenset(['buyer_note', 'fulfilment', 'sale_readiness']),
    'status_link': frozenset([]),
    'third_party_statement': frozenset([]),
    'trade_profiles': frozenset([]),
    'transit_delivered_guide': frozenset([]),
    'two_clocks': frozenset(['sale_readiness']),
}

# The measurement harness imports the shipped draft prompt/validator. Its synthetic
# cases and tests therefore follow that feature's publication scope, never a waiver.
DRAFT_EVAL_PATHS = ("tools/eval_draft.py", "tools/test_eval_draft.py", "pilot/eval/draft_cases/**")

# Test folders that test the code of a namespace under another folder name: they are published with that namespace
# (the stdio protocol tests of the local MCP server belong to mcp_local_contracts).
TEST_FOLDER_ALIASES = {"mcp_local_contracts": ("tests/features/mcp_local_protocol/**",)}


def effective_exclude():
    selected = set(PUBLISHED_NAMESPACES)
    if selected - set(KNOWN_NAMESPACES):
        raise ExportError("Unknown publication namespace")
    if any(not NAMESPACE_DEPENDENCIES[name] <= selected for name in selected):
        raise ExportError("Publication selection is not closed under namespace dependencies")
    if not selected:
        return EXCLUDE
    roots = ("copilot/features/", "copilot/web/templates/features/", "tests/features/")
    rows = EXCLUDE[:-3] + [(root + name + "/**", [])
                          for name in KNOWN_NAMESPACES if name not in selected for root in roots]
    # Unknown and root-level feature files remain denied; only the package initializer is required.
    rows += [(root + "**", [root + name + "/**" for name in sorted(selected)]
              + ([root + "__init__.py"] if root != "copilot/web/templates/features/" else [])
              + ([alias for name in sorted(selected) for alias in TEST_FOLDER_ALIASES.get(name, ())] if root == "tests/features/" else []))
             for root in roots]
    if "draft" in selected:
        rows = [row for row in rows if row[0] not in DRAFT_EVAL_PATHS]
    return rows

def effective_t15():
    return [entry for entry in T15_FUNCTIONAL_LITERALS if classify(entry["names"])[0] == "excluded"]


class ExportError(Exception):
    """The export can not be built (exit 2). Messages never contain file contents or private terms."""


# ------------------------------------------------------------------------------------------------------ rules
# Patterns are path globs on POSIX-style repo-relative paths: `**` crosses directories, `*` and `?` do not.
INCLUDE = [
    "LICENSE", "README.md", "THIRD-PARTY.md", "render.yaml", "render.free.yaml",                                # R1
    ".env.example", ".gitignore", ".gitattributes", ".mcp.json.example",                          # R2
    "conftest.py", "pytest.ini", "requirements*.txt",                                             # R3 (root only: `*` stops at `/`)
    "copilot/**", "pilot/**", "demo/**", "tests/**", "tools/**",                                  # R4-R8 public roots
]

# T17: R9, the ONLY way into docs/. Any other docs/ path is excluded as `docs:not-public`.
PUBLIC_DOCS = ["docs/AI-USAGE.md", "docs/JUDGE-RUN.md", "docs/MCP.md", "docs/ARCHITECTURE.md", "docs/DESIGN.md",
               "docs/brand/**", "docs/DECISIONS.md", "docs/CONFORMANCE.md", "docs/impact-1/**"]

# The sanitised-derivative mechanism is kept and UNUSED (empty) since D-2026-10-05-11. A path listed here (and not
# removed by EXCLUDE) is published as a sanitised derivative through sanitize() at the exported commit.
SANITIZED: list[str] = []

# (pattern, exceptions): a path that matches an exception pattern is not removed by that pattern.
EXCLUDE = [
    ("internal/**", []),                   # X1: everything internal; first, wins over every INCLUDE (T16)
    ("evidence/**", []),                   # X2 (belt)
    ("CLAUDE.md", []), ("AGENTS.md", []), (".claude/**", []), ("tools/hooks/**", []), ("tools/env/**", []),   # X3
    ("tools/flags_on_matrix.py", []),      # private preparation runner; not part of the judge route
    ("tools/pre-push", []),                # X3: the push refusal has no place in a public repository (tools/** would publish it)
    ("REVIEW.md", []), ("STATUS.md", []),  # X4
    (".env", []), (".env.*", [".env.example"]), ("data/**", []), ("reference/**", []),     # X5
    ("research/**", []),                   # X6
    ("docs/evidence-claims.json", []),     # X7 (the docs gate also excludes it; named for T13)
    ("pilot/fixtures/recorded/**", []), (".venv*/**", []), ("**/__pycache__/**", []), ("**.pyc", []),   # X8 belts
    *((path, []) for path in DRAFT_EVAL_PATHS),
    # Files that no public sentence and no judge-run step depends on stay in the private repository. A removal is
    # accepted only after a dry export without the file and one run of both test suites on the result. docs/DESIGN.md,
    # docs/DECISIONS.md, pilot/step2_order.py, the brand SVGs and render.free.yaml stay: a test or a public page uses them.
    ("tools/video/**", []), ("tools/check_video.ps1", []),                       # video production (a placeholder caption file)
    ("tools/estimate_narration.py", []), ("tools/test_estimate_narration.py", []),
    ("tools/hosted_smoke.py", []),                                                # operator smoke test of the hosted demo
    # the automated walk (D-88): an internal harness with seeded ids and local paths; the vendored axe copy would also need its MPL notice
    ("tools/auto_run.py", []), ("tools/auto_run_pages.py", []), ("tools/auto_run_dry_app.py", []),
    ("tools/auto_run_d101_p2.py", []), ("tests/tools/test_auto_run_d101_p2.py", []),
    ("tools/auto_run_d101_p2_hosted.py", []), ("tests/tools/test_auto_run_d101_p2_hosted.py", []),
    ("tools/auto_run_d101_seller.py", []), ("tools/test_auto_run_d101_seller.py", []),
    ("tools/auto_run_d101_r3.py", []), ("tests/tools/test_auto_run_d101_r3.py", []),
    ("tools/auto_run_d101_p1.py", []), ("tools/test_auto_run_d101_p1.py", []),
    ("tools/auto_run_d101_hands.py", []), ("tests/tools/test_auto_run_d101_hands.py", []), ("tools/auto_run_d101_b5.py", []), ("tests/tools/test_auto_run_d101_b5.py", []), ("tests/tools/test_auto_run_port_free.py", []), ("tests/tools/test_auto_run_prepare_reason.py", []), ("tests/tools/test_auto_run_ports_label.py", []), ("tests/tools/test_auto_run_dry_send.py", []), ("tests/tools/test_auto_run_walk_header.py", []), ("tests/tools/test_auto_run_step_numbers.py", []),
    ("tests/tools/test_auto_run.py", []), ("tools/auto_run_d101_p4.py", []), ("tests/tools/test_auto_run_d101_p4.py", []), ("tools/auto_run_d101_p5.py", []), ("tests/tools/test_auto_run_d101_p5.py", []), ("tools/vendor/axe-core-4.10.2/**", []),
    ("tools/auto_run_accept_app.py", []), ("tools/auto_run_d101_b3.py", []), ("tools/test_auto_run_d101_b3.py", []), ("tools/auto_run_d101_r1.py", []), ("tools/test_auto_run_d101_r1.py", []),
    ("tools/auto_run_d101_seed.py", []), ("tests/tools/test_auto_run_d101_seed.py", []),
    ("tests/tools/test_auto_run_d101_vault.py", []),
    ("tools/check_pending.py", []), ("tools/test_check_pending.py", []),          # it reads files that are not published
    # IMPACT-1 run tools (the measured-contrast harness): internal run tooling, not part of the judge route; they import feature packages
    ("tools/impact_run.py", []), ("tools/impact_decide.py", []), ("tools/impact_docs.py", []), ("tools/impact_fake.py", []), ("tools/impact_seed.py", []),
    ("tools/test_impact_run.py", []), ("tools/test_impact_decide.py", []), ("tools/test_impact_seed.py", []),
    ("pilot/step1_auth.py", []), ("docs/brand/preview.html", []), ("render.yaml", []),
    # feature namespaces that are not published yet (they win over the public roots)
    ("copilot/features/**", []), ("copilot/web/templates/features/**", []), ("tests/features/**", []),
]

# T15 exemptions: hits that sit in a functional literal (a path table or a fixture a test or tool uses), so the
# text can not be reworded. One entry per hit; skipped only when path, literal AND the named file all match. An entry
# that matches nothing is reported as stale, so the table can not rot silently.
T15_FUNCTIONAL_LITERALS = [
    {"path": "tests/local/test_hosts_static.py", "literal": '("copilot/features/conformance/pins.py", "developer.paypal.com"): DOC_REASON,', "names": "copilot/features/conformance/pins.py",
     "reason": "path table the test looks up"},
    {"path": "tests/local/test_hosts_static.py", "literal": '("copilot/features/outcome/appeal.py", "developer.paypal.com"): DOC_REASON,', "names": "copilot/features/outcome/appeal.py",
     "reason": "path table the test looks up"},
    {"path": "tests/local/test_hosts_static.py", "literal": '"tools/hooks/guard.py", "tools/hooks/test_guard.py", "tools/export_public.py",', "names": "tools/hooks/guard.py",
     "reason": "path table the test looks up"},
    {"path": "tests/local/test_hosts_static.py", "literal": '"tools/hooks/guard.py", "tools/hooks/test_guard.py", "tools/export_public.py",', "names": "tools/hooks/test_guard.py",
     "reason": "path table the test looks up"},
    {"path": "tests/local/test_hosts_static.py", "literal": '("tools/check_pending.py", "help.devpost.com"): DOC_REASON,', "names": "tools/check_pending.py",
     "reason": "path table the test looks up (the private repository keeps the tool; a row removed here would fail its host test)"},
    {"path": "tests/local/test_proofie_name.py", "literal": '"copilot/web/templates/_base.html", ".mcp.json.example", "docs/MCP.md", "docs/UX-CRITERIA.md",', "names": "docs/UX-CRITERIA.md",
     "reason": "path table the test looks up"},
    {"path": "tools/test_scan_staged.py", "literal": '("docs/x.md", False), ("tests/x.py", False), (".claude/settings.json", False),', "names": ".claude/settings.json",
     "reason": "fixture path a test asserts"},
    {"path": "tools/test_scan_staged.py", "literal": '"evidence/manual/INDEX.md", "README.md"])', "names": "evidence/manual/INDEX.md",
     "reason": "fixture path a test asserts"},
    {"path": "tools/test_scan_staged.py", "literal": '"evidence/a/b.json", "reference/notes.md", "scratch-ver/CODEX0-a.txt",', "names": "scratch-ver/CODEX0-a.txt",
     "reason": "fixture path a test asserts (found by the T15 rule check)"},
    {"path": "tools/test_redact.py", "literal": 'assert not redact.is_private(Path("docs/ENVIRONMENT.md"))', "names": "docs/ENVIRONMENT.md",
     "reason": "fixture path a test asserts (a docs/ path that redact.is_private must not treat as private)"},
]

# The private evidence files the index describes (read from git objects at the commit).
EVIDENCE_FILES = ["evidence/*.jsonl", "evidence/*.json"]

# Allowed exceptions of the final scan, one entry per hit: exact repo-relative path, scan kind and line, plus
# the reason. An entry matches only on all three; one that matches nothing is reported as a stale exception.
ALLOWED_EXCEPTIONS = [
    {"path": "pilot/fixtures/order_confirmation.txt", "kind": "street_address", "line": 15,
     "reason": "synthetic address marked DEMO"},
    {"path": "pilot/fixtures/shipping_tracking.txt", "kind": "street_address", "line": 16,
     "reason": "synthetic address marked DEMO"},
    {"path": "pilot/fixtures/variants/contradictory/shipping_tracking.txt", "kind": "street_address", "line": 14,
     "reason": "synthetic address marked DEMO"},
    {"path": "tests/local/test_field_extraction.py", "kind": "street_address", "line": 46,
     "reason": "synthetic address"},
    {"path": "tools/sandbox_orders.py", "kind": "street_address", "line": 34,
     "reason": "invented demo address of the sandbox sale (the earlier entry moved with the line)"},
    {"path": "tools/sandbox_card.py", "kind": "street_address", "line": 48,
     "reason": "invented demo billing address of the sandbox card order"},
    {"path": "tools/sandbox_card.py", "kind": "street_address", "line": 51,
     "reason": "false match: a comment, a decimal reference number followed by words, read as a house number"},
    {"path": "docs/JUDGE-RUN.md", "kind": "street_address", "line": 182,
     "reason": "false match: a plain count followed by words in a sentence, read as a house number"},
    {"path": "tools/judge_setup.py", "kind": "credential_assignment", "line": 88,
     "reason": "false match: a function default that names a function, no value"},
    {"path": "tools/test_judge_setup.py", "kind": "credential_assignment", "line": 55,
     "reason": "synthetic secret string of a test fixture, not a credential"},
    {"path": "tests/local/test_seller_shortcuts_document.py", "kind": "credential_assignment", "line": 86,
     "reason": "synthetic token string of a test, not a credential"},
    {"path": "tests/local/test_seller_shortcuts_document.py", "kind": "credential_assignment", "line": 110,
     "reason": "synthetic token string of a test, not a credential"},
    {"path": "tools/auto_run_d101_b8.py", "kind": "merchant_id", "line": 38,
     "reason": "stand-in literal of the offline PayPal stand-in (STANDIN prefix); appears in no evidence file"},
    {"path": "tools/test_sandbox_card.py", "kind": "merchant_id", "line": 45,
     "reason": "placeholder merchant literal of a test"},
    {"path": "tools/test_sandbox_card.py", "kind": "merchant_id", "line": 74,
     "reason": "placeholder merchant literal of a test"},
    {"path": "tests/features/judge_pool/test_judge_pool.py", "kind": "paypal_live_host", "line": 152,
     "reason": "a refusal test names the live host to prove the app rejects it"},
    {"path": "tests/features/ship_moment_capture/test_register_offer.py", "kind": "paypal_live_host", "line": 78,
     "reason": "a refusal test names the live host to prove the app rejects it"},
    {"path": "tools/test_sandbox_dispute_state.py", "kind": "paypal_live_host", "line": 115,
     "reason": "a refusal test names the live host to prove the app rejects it"},
    {"path": "tools/test_sandbox_dispute_state.py", "kind": "paypal_live_host", "line": 145,
     "reason": "an assertion that the live host never appears in the output"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "email", "line": 25,
     "reason": "synthetic contact on the reserved .invalid domain"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "email_address", "line": 25,
     "reason": "synthetic contact on the reserved .invalid domain"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "street_address", "line": 25,
     "reason": "invented example street of the sample sale"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "email", "line": 26,
     "reason": "synthetic contact on the reserved .invalid domain"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "email_address", "line": 26,
     "reason": "synthetic contact on the reserved .invalid domain"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "email", "line": 27,
     "reason": "synthetic contact on the reserved .invalid domain"},
    {"path": "copilot/features/sale_readiness/service.py", "kind": "email_address", "line": 27,
     "reason": "synthetic contact on the reserved .invalid domain"},
    {"path": "tests/features/sale_readiness/test_fulfilment_integration.py", "kind": "street_address", "line": 51,
     "reason": "invented address of a synthetic test order"},
    {"path": "tests/features/sale_readiness/test_fulfilment_integration.py", "kind": "street_address", "line": 87,
     "reason": "invented address of a synthetic test order"},
]

# Allowed exceptions that are path globs (synthetic-data paths and test fixtures): (path pattern, kind, reason).
SCAN_EXCEPTIONS = [
    ("tests/local/test_context.py", "paypal_live_host", "the test proves the app rejects the live host"),
    ("tests/local/test_send.py", "paypal_live_host", "the test proves the app rejects the live host"),
    ("copilot/fixtures/**", "street_address", "synthetic data"),
    ("pilot/eval/**", "street_address", "synthetic data"),
    ("pilot/test_sanitize.py", "street_address", "synthetic data"),
    ("demo/documents/**", "street_address", "synthetic data"),
    ("copilot/features/carrier_directory/directory.json", "local_path_posix_home", "a public carrier tracking URL (the path segment is a language/region slug, not a directory)"),
    ("copilot/features/lost_package_paths/paths.json", "local_path_posix_home", "a public carrier tracking URL (the path segment is a language/region slug, not a directory)"),
    ("tests/features/buyer_note/test_pre_dispute.py", "paypal_live_host", "the test proves the page never names the live host"),
    ("tests/features/buyer_page/test_buyer_page.py", "email", "synthetic buyer address on the reserved .invalid domain"),
    ("tests/features/buyer_page/test_buyer_page.py", "email_address", "synthetic buyer address on the reserved .invalid domain"),
    ("tests/features/package_insert/test_package_insert.py", "email", "synthetic seller contact on the reserved .test domain"),
    ("tests/features/package_insert/test_package_insert.py", "email_address", "synthetic seller contact on the reserved .test domain"),
    ("tests/features/ship_moment_capture/test_ship_moment_capture.py", "credential_assignment", "a literal synthetic CSRF token in a test, not a credential"),
    ("tests/features/accounting_line/test_ola4_visual.py", "email", "synthetic seller contact on the reserved .test domain"),
    ("tests/features/accounting_line/test_ola4_visual.py", "email_address", "synthetic seller contact on the reserved .test domain"),
    ("tests/features/package_insert/test_ola4_p6_flow.py", "email", "synthetic seller contact on the reserved .test domain"),
    ("tests/features/package_insert/test_ola4_p6_flow.py", "email_address", "synthetic seller contact on the reserved .test domain"),
    ("tests/features/buyer_note/test_delivery_incident_assistant.py", "street_address", "invented address of a synthetic test order"),
    ("tests/features/buyer_note/test_ola4_incident_route.py", "street_address", "invented address of a synthetic test order"),
    ("tests/features/buyer_note/test_shipment_package_integration.py", "street_address", "invented address of a synthetic test order"),
    ("tests/local/test_ux_rulebook_v2_continuation.py", "credential_assignment", "a literal synthetic CSRF token in a test, not a credential"),
    ("tools/test_redact.py", "street_address", "fictional and invented addresses that test the detector; a path rule, so a moved line cannot break it"),
    ("tools/test_scan_staged.py", "auth_header", "test fixtures for the staged-file scanner"),
    ("tools/test_scan_staged.py", "credential_assignment", "test fixtures for the staged-file scanner"),
]

# Identifier keys with a value, in JSON, Python/YAML and query strings, key names case-insensitive and
# also camelCase. Only the stated shapes are detected: a 13-character uppercase alphanumeric value after merchant_id,
# payer_id or account_id; 6 or more digits after account_number; an e-mail outside the allowed domains after
# email_address. Bare tokens without a key are NOT detected by shape (the private-terms file lists real ones).
# A value is also acceptable inside a documented synthetic range (path pattern, whole-value regex, reason).
PLACEHOLDER_VALUES = {"", "[redacted]", "[email]"}
SYNTHETIC_VALUE_ALLOWANCES = [
    ("copilot/fixtures/**", r"DEMO[A-Za-z0-9_-]*", "the synthetic fixtures' documented DEMO id range"),
    ("pilot/fixtures/synthetic/**", r"DEMO[A-Z0-9_-]*", "the synthetic fixtures' documented DEMO id range"),
    ("pilot/test_sanitize.py", r"DEMO[A-Z0-9_-]*", "synthetic ids with the DEMO prefix"),
]
# Allowed e-mail domains: example.com, example.org, example.net and their subdomains; a generated sandbox login local part blocks.
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
SANDBOX_LOCAL_PART = re.compile(r"(?i)^sb-[A-Za-z0-9]{6,}")

_KEY_SEP = r"""(?:\\*["'])?\s*[:=]\s*(?:\\*["'])?"""      # a closing quote may be backslash-escaped (JSON in a string)
_ID_KEYED = re.compile(r"(?<![A-Za-z0-9])(?i:merchant[_-]?id|payer[_-]?id|account[_-]?id)" + _KEY_SEP
                       + r"([A-Z0-9]{13})(?![A-Za-z0-9])")
_NUMBER_KEYED = re.compile(r"(?<![A-Za-z0-9])(?i:account[_-]?number)" + _KEY_SEP + r"([0-9]{6,})")
_EMAIL_KEYED = re.compile(r"(?<![A-Za-z0-9])(?i:email[_-]?address)" + _KEY_SEP + r"""([^\s"'\\,;)}\]]+)""")
# Assembled from pieces: written whole, the commit's own e-mail pattern flags this very line when the tool is exported.
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)" + "@" + r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# The credential shape that redact.py's own rule misses when the key carries a prefix (PAYPAL_CLIENT_SECRET):
# reported under redact's kind name `credential_assignment` so the spec's exceptions apply. Values that read as
# placeholders are not credentials.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""(?i)PAYPAL_CLIENT_(?:ID|SECRET)\s*[:=]\s*["']?([A-Za-z0-9_-]{20,})""")
_PLACEHOLDER_PREFIX = re.compile(r"(?i)^(?:your|fake|xxx|example|changeme|placeholder|dummy|test)")

# The extra scan patterns. Assembled from pieces so that THIS file (which is itself exported and scanned)
# does not contain the literal strings it looks for.
_EXTRA_PATTERNS = [
    ("local_path_windows", re.compile(r"[A-Za-z]:\\" + "Us" + r"ers\\")),
    ("local_path_posix_users", re.compile("/Us" + "ers/")),
    ("local_path_posix_home", re.compile("/ho" + "me/")),
]
PRIVATE_TERM_KIND = "private_term"
UNSCANNABLE_KIND = "unscannable"
IDENTIFIER_KIND = "identifier_value"
EMAIL_KIND = "email_address"
SCAN_ONLY_KINDS = ["credential_assignment", IDENTIFIER_KIND, EMAIL_KIND]


def _glob_to_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


_REGEX_CACHE: dict[str, re.Pattern] = {}


def match(pattern: str, path: str) -> bool:
    if pattern not in _REGEX_CACHE:
        _REGEX_CACHE[pattern] = _glob_to_regex(pattern)
    return _REGEX_CACHE[pattern].match(path) is not None


def classify(path: str) -> tuple[str, str]:
    """('copied' | 'sanitized' | 'excluded', rule). EXCLUDE always wins; nothing matched = default-deny."""
    for pattern, exceptions in effective_exclude():
        if match(pattern, path) and not any(match(e, path) for e in exceptions):
            return "excluded", f"exclude:{pattern}"
    if path.startswith("docs/"):                               # T17: the docs/ gate, R9 is the only way in
        if any(match(g, path) for g in PUBLIC_DOCS):
            return ("sanitized", "include:sanitized") if any(match(p, path) for p in SANITIZED) else ("copied", "include")
        return "excluded", "docs:not-public"
    if any(match(p, path) for p in SANITIZED):
        return "sanitized", "include:sanitized"
    if any(match(p, path) for p in INCLUDE):
        return "copied", "include"
    return "excluded", "default-deny"


def rules_version() -> str:
    """sha256 of the rule set exactly as encoded in this tool (any rule change changes it)."""
    encoded = json.dumps({"include": INCLUDE, "sanitized": SANITIZED, "exclude": effective_exclude(), "published_namespaces": sorted(PUBLISHED_NAMESPACES),
                          "public_docs": PUBLIC_DOCS, "evidence_files": EVIDENCE_FILES,
                          "allowed_exceptions": ALLOWED_EXCEPTIONS, "t15_literals": effective_t15(),
                          "scan_exceptions": SCAN_EXCEPTIONS,
                          "synthetic_values": [[a, b, c] for a, b, c in SYNTHETIC_VALUE_ALLOWANCES],
                          "seed_demo_address_context": SEED_DEMO_ADDRESS_CONTEXT,
                          "email_domains": list(ALLOWED_EMAIL_DOMAINS),
                          "extra_patterns": [name for name, _ in _EXTRA_PATTERNS] + SCAN_ONLY_KINDS,
                          "binary_scan": ["pdf_text_info_xmp", "png_text_exif"],
                          "metadata_fixture_exceptions": False, "template_values": "placeholder_or_feature_boolean"},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def count_lines(data: bytes) -> int:
    """Lines as `wc -l` plus a final unterminated line: b'' -> 0, b'a\\nb' -> 2, b'a\\nb\\n' -> 2."""
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


# ------------------------------------------------------------------------------------------------ git objects
def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True)
    if proc.returncode != 0:
        raise ExportError(f"git {args[0]} failed")
    return proc.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    """The 40-hex SHA of a COMMIT. A tree, a blob, a tag of a non-commit or an unknown name is an error."""
    try:
        out = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode("ascii").strip()
    except (ExportError, UnicodeDecodeError):
        raise ExportError("the ref is not a commit of this repository") from None
    if not HEX40.match(out):
        raise ExportError("the ref did not resolve to a 40-hex commit")
    return out


def list_tree(repo: Path, sha: str) -> list[tuple[str, str, str]]:
    """[(path, mode, blob oid)] for every entry of the commit's tree. Submodules and symlinks are returned with their
    mode so the caller can exclude them; they are never followed."""
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha)
    entries = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        mode, _type, oid = meta.decode("ascii").split()
        entries.append((path.decode("utf-8"), mode, oid))
    return entries


def read_blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    unique = list(dict.fromkeys(oids))
    if not unique:
        return {}
    data = _git(repo, "cat-file", "--batch", stdin=("\n".join(unique) + "\n").encode("ascii"))
    out, pos = {}, 0
    for oid in unique:
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ExportError("unexpected object in the tree")
        size = int(header[2])
        out[oid] = data[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return out


def load_commit_module(repo: Path, sha: str, path: str, name: str, register_as: str | None = None):
    """Loads a Python module from the BYTES AT THE COMMIT (never from the working tree). `register_as` is the name
    other modules of the commit import it by; the entry in sys.modules is only there while the loading runs."""
    try:
        source = _git(repo, "show", f"{sha}:{path}")
    except ExportError:
        raise ExportError(f"{path} is not in the exported commit") from None
    module = types.ModuleType(name)
    module.__file__ = "/__export_commit__/" + path          # only used lexically by the module
    try:
        exec(compile(source, f"<{sha[:8]}:{path}>", "exec"), module.__dict__)
    except Exception as exc:                                  # noqa: BLE001 - reported without detail
        raise ExportError(f"{path} at the exported commit can not be imported ({type(exc).__name__})") from None
    return module


def load_commit_modules(repo: Path, sha: str, present: set[str]):
    """(client, redact) from the commit's own bytes. `tools/redact.py` imports `personal_data_patterns`, which would
    resolve from the WORKING TREE through sys.path; the commit's blob is put in sys.modules under that name first, and
    whatever was there before is put back afterwards, so nothing of the commit leaks into this process."""
    client = load_commit_module(repo, sha, "pilot/client.py", "_export_commit_client")
    shared = "personal_data_patterns"
    previous, saved_path = sys.modules.get(shared), list(sys.path)
    try:
        if "tools/personal_data_patterns.py" in present:
            sys.modules[shared] = load_commit_module(repo, sha, "tools/personal_data_patterns.py", shared)
        else:
            sys.modules.pop(shared, None)                     # the commit has none: a working-tree copy must not stand in
        redact = load_commit_module(repo, sha, "tools/redact.py", "_export_commit_redact")
    finally:
        sys.path[:] = saved_path                              # redact.py inserts its own folder at import time
        if previous is None:
            sys.modules.pop(shared, None)
        else:
            sys.modules[shared] = previous
    return client, redact


# ------------------------------------------------------------------------------------------------ derivatives
def sanitize_jsonl(data: bytes, sanitize, path: str) -> tuple[bytes, int]:
    """(Unused path, kept on purpose.) One output line per input line. A line that is not a JSON value fails the export
    (file and line only)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportError(f"{path} is not valid UTF-8") from None
    out_lines = []
    for number, segment in enumerate(text.split("\n"), 1):
        eol = "\r" if segment.endswith("\r") else ""
        body = segment[:-1] if eol else segment
        if not body.strip():
            out_lines.append(segment)                          # blank lines stay blank (line numbers stay valid)
            continue
        try:
            value = json.loads(body)
        except ValueError:
            raise ExportError(f"{path}: line {number} is not valid JSON") from None
        out_lines.append(json.dumps(sanitize(value), ensure_ascii=False) + eol)
    return "\n".join(out_lines).encode("utf-8"), len(text.splitlines())


def sanitize_json(data: bytes, sanitize, path: str) -> bytes:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ExportError(f"{path} is not valid JSON") from None
    return (json.dumps(sanitize(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------- evidence index
def _valid_repo_path(file: object) -> bool:
    return (isinstance(file, str) and bool(file) and not file.startswith("/") and ".." not in file.split("/")
            and "\\" not in file and not re.match(r"^[A-Za-z]:", file))


def load_claims(raw: bytes | None) -> list[dict]:
    """Validates docs/evidence-claims.json (schema CLAIMS_SCHEMA). The file is REQUIRED: None means it is
    not in the commit and the export fails. An empty claims list is valid (the caller warns)."""
    if raw is None:
        raise ExportError(f"{CLAIMS_PATH} is required and is not in the exported commit")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ExportError(f"{CLAIMS_PATH} is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != CLAIMS_SCHEMA or not isinstance(doc.get("claims"), list):
        raise ExportError(f"{CLAIMS_PATH} does not follow the schema {CLAIMS_SCHEMA}")
    seen, claims = set(), []
    for position, claim in enumerate(doc["claims"], 1):
        where = f"{CLAIMS_PATH}, claim {position}"
        if not isinstance(claim, dict):
            raise ExportError(f"{where} is not an object")
        cid, file, lines = claim.get("id"), claim.get("file"), claim.get("lines")
        if not (isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", cid)) or cid in seen:
            raise ExportError(f"{where}: id is missing, malformed or repeated")
        seen.add(cid)
        if not _valid_repo_path(file):
            raise ExportError(f"{where} ({cid}): file must be a repo-relative path")
        if lines is not None and not (isinstance(lines, list) and len(lines) == 2
                                      and all(isinstance(n, int) and not isinstance(n, bool) for n in lines)):
            raise ExportError(f"{where} ({cid}): lines must be [first, last] or null (the whole file)")
        for key in ("recorded_utc", "proves"):
            if not (isinstance(claim.get(key), str) and claim[key].strip()):
                raise ExportError(f"{where} ({cid}): {key} is missing")
        used = claim.get("used_by")
        if not (isinstance(used, list) and all(isinstance(u, str) for u in used)):
            raise ExportError(f"{where} ({cid}): used_by must be a list of text")
        claims.append({"id": cid, "file": file, "lines": lines, "recorded_utc": claim["recorded_utc"],
                       "proves": claim["proves"], "used_by": used})
    return claims


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _private_file_label(path: str) -> str:
    """Display a neutral basename; retain the repo path only for source lookup/hashing."""
    return _cell(f"{Path(path).name} (private file, hashed)")


def _published_claim_uses(uses: list[str]) -> str:
    """Public file references only; keep original claim metadata privately, never emit an unpublished annotation."""
    labels = []
    for use in uses:
        path = use.split(maxsplit=1)[0] if use.strip() else ""
        if not path:
            continue
        label = path if _valid_repo_path(path) and classify(path)[0] in ("copied", "sanitized") else "Private material (not published)"
        if label not in labels:
            labels.append(label)
    return _cell("; ".join(labels))


def build_evidence_index(sha: str, evidence: dict[str, bytes], claims: list[dict], external: bool) -> bytes:
    """EVIDENCE-INDEX.md: hashes, counts and our own words, never content. `evidence` maps each private file the index
    describes (every evidence/** file plus every other file a claim cites) to the exact bytes to hash: git blob bytes
    at the commit, or the stored bytes of an external directory."""
    rows = []
    for claim in claims:
        data = evidence.get(claim["file"])
        if data is None:
            raise ExportError(f"claim {claim['id']} cites {claim['file']}, which is not a private file of this source")
        if claim["lines"] is None:
            rows.append((claim, "all", sha256(data)))                # the whole file: its own sha256
            continue
        first, last = claim["lines"]
        total = count_lines(data)
        if not (1 <= first <= last <= total):
            raise ExportError(f"claim {claim['id']} cites lines {first}-{last} of {claim['file']}, "
                              f"which has {total} lines")
        cited = b"\n".join(data.split(b"\n")[first - 1:last])
        rows.append((claim, str(first) if first == last else f"{first}-{last}", sha256(cited)))
    source_rule = ("the evidence files were read from a directory outside the repository and are hashed exactly as "
                   "stored" if external else "every hash and count is computed over the git blob bytes at the source "
                   "commit (LF line endings), never over a working-tree copy")
    verify = ("Recompute the same values on the file you were shown: its sha256, its line count and the sha256 of the "
              "cited lines." if external else "Recompute them from the private repository with "
              f"`git show {sha}:<file> | sha256sum` (or convert CRLF to LF first if you have a Windows checkout).")
    out = [
        "# Evidence index", "",
        f"Source commit: `{sha}`", "",
        "The files listed here are private records of calls made against the PayPal sandbox. They are not published, "
        "because the PayPal Developer Agreement (section 11) treats information received through API calls as "
        "confidential. The recorded model outputs used by the held-out replay are synthetic-case text only and ship in "
        "pilot/eval/draft_cases/recorded/. The author keeps the private files unchanged. This index "
        "publishes only hashes, counts and our own words, so that a reviewer shown a file can check it against this "
        "list.", "",
        f"How to verify: {verify} Hashing rule: {source_rule}. `cited_lines_sha256` is the sha256 of the exact bytes of "
        "the cited lines, separated by LF, with no trailing LF and no BOM; when a claim cites the whole file "
        "(`all`) it is the file's own sha256.", "",
        "## Files", "", "| file | file_sha256 | lines | bytes |", "| --- | --- | --- | --- |"]
    for name in sorted(evidence):
        out.append(f"| {_private_file_label(name)} | {sha256(evidence[name])} | {count_lines(evidence[name])} | {len(evidence[name])} |")
    if not evidence:
        out.append("| (no evidence files) | | 0 | 0 |")
    out += ["", "## Claims", "",
            "| id | file | lines | cited_lines_sha256 | recorded_utc | proves | used_by |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for claim, span, cited_sha in rows:
        out.append(f"| {_cell(claim['id'])} | {_private_file_label(claim['file'])} | {span} | {cited_sha} | "
                   f"{_cell(claim['recorded_utc'])} | {_cell(claim['proves'])} | {_published_claim_uses(claim['used_by'])} |")
    if not rows:
        out.append("| (no claims) | | | | | | |")
    text = "\n".join(out) + "\n"
    # No private content may reach the index: a claim's wording must not reproduce a line of a described file.
    for name, data in evidence.items():
        for number, raw_line in enumerate(data.split(b"\n"), 1):
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if len(line) >= 24 and line in text:
                raise ExportError(f"the index would copy line {number} of {name}")
    return text.encode("utf-8")


# ----------------------------------------------------------------------------------------------- private terms
def read_private_terms(argument: str | None) -> list[str]:
    """UTF-8, one literal term per line; blank lines and lines starting with # are ignored; matching is
    case-insensitive; a term needs MIN_TERM_LENGTH characters. Fails closed: no file, an unreadable file, a file with
    no term or a too short term is an error. Terms are returned lower-cased and are never printed."""
    location = argument or os.environ.get(PRIVATE_TERMS_ENV)
    if not location:
        raise ExportError("no private-terms file: pass --private-terms or set " + PRIVATE_TERMS_ENV)
    path = Path(location)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ExportError("the private-terms file is missing or unreadable") from None
    terms = [line.strip().lower() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not terms:
        raise ExportError("the private-terms file holds no term")
    if any(len(term) < MIN_TERM_LENGTH for term in terms):
        raise ExportError(f"the private-terms file has a term shorter than {MIN_TERM_LENGTH} characters")
    return terms


# ------------------------------------------------------------------------------------------------------- scan
def _email_blocked(local: str, domain: str) -> bool:
    """Allowed only at example.com/.org/.net or a subdomain, and never a generated sandbox login."""
    if SANDBOX_LOCAL_PART.match(local):
        return True
    domain = domain.lower()
    return not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def _allowed_email_hit(kind: str, line: str) -> bool:
    """The commit's own `email` and `email_address` rules are broader than the decision (they know only a
    fixed set of safe domains, and flag any value after the key). A hit of those two kinds is set aside when every
    address on the line is at an allowed domain (and, for the keyed form, every value is a placeholder or such an
    address); this tool's own e-mail rules above still judge the line."""
    if kind not in ("email", "email_address"):
        return False
    addresses = list(_EMAIL_RE.finditer(line))
    if any(_email_blocked(m.group(1), m.group(2)) for m in addresses):
        return False
    if kind == "email":
        return bool(addresses)
    values = [m.group(1) for m in _EMAIL_KEYED.finditer(line)]
    return bool(values) and all(v.lower() in PLACEHOLDER_VALUES or _EMAIL_RE.fullmatch(v) for v in values)


def _identifier_hits(line: str, path: str) -> bool:
    for found in _ID_KEYED.finditer(line):
        value = found.group(1)
        if value.lower() in PLACEHOLDER_VALUES:
            continue
        if any(match(p, path) and re.fullmatch(rx, value) for p, rx, _why in SYNTHETIC_VALUE_ALLOWANCES):
            continue
        return True
    if _NUMBER_KEYED.search(line):
        return True
    for found in _EMAIL_KEYED.finditer(line):
        value = found.group(1)
        if value.lower() in PLACEHOLDER_VALUES:
            continue
        pair = _EMAIL_RE.fullmatch(value)
        if pair is not None and _email_blocked(pair.group(1), pair.group(2)):     # only an e-mail is a value here
            return True
    return False


def _line_hits(line: str, path: str) -> list[str]:
    kinds = [kind for kind, pattern in _EXTRA_PATTERNS if pattern.search(line)]
    if _identifier_hits(line, path):
        kinds.append(IDENTIFIER_KIND)
    if any(_email_blocked(m.group(1), m.group(2)) for m in _EMAIL_RE.finditer(line)):
        kinds.append(EMAIL_KIND)
    if any(not _PLACEHOLDER_PREFIX.match(m.group(1)) for m in _CREDENTIAL_ASSIGNMENT.finditer(line)):
        kinds.append("credential_assignment")
    return kinds


def _template_placeholder(value: str) -> bool:
    if _PLACEHOLDER_PREFIX.match(value):
        return True
    pair = _EMAIL_RE.fullmatch(value)
    return bool(pair and not _email_blocked(pair[1], pair[2]))


def binary_scan_parts(path: str, data: bytes) -> tuple[str, str]:
    """Extract PDF text/Info/XMP or PNG text/EXIF. Parser/unsupported cases raise: caller blocks."""
    import io
    import zlib
    import xml.etree.ElementTree as ET
    if Path(path).suffix.lower() == '.pdf':
        import pypdfium2 as pdfium
        # This structural extractor does not tokenize escaped PDF names. Do not
        # certify metadata clean when a parser can interpret a name we miss.
        if re.search(rb'/[^\s/<>\[\](){}%]*#[0-9A-Fa-f]{2}', data):
            raise ValueError('Unsupported escaped PDF name')
        # No silent metadata omission for encrypted/compressed object dictionaries.
        if re.search(rb'/Encrypt\b|/ObjStm\b', data):
            raise ValueError('Unsupported PDF metadata container')
        if re.search(rb'/Info\b(?!\s+\d+\s+\d+\s+R\b)', data):
            raise ValueError('Unsupported PDF Info reference')
        objects = {(int(m[1]), int(m[2])): m[3] for m in
                   re.finditer(rb'(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj\b', data, re.S)}
        info_keys = set()
        for ref in re.findall(rb'/Info\s+(\d+)\s+(\d+)\s+R\b', data):
            info = objects.get(tuple(map(int, ref)))
            if info is None or info.count(b'<<') != 1 or b'[' in info:
                raise ValueError('Unsupported PDF Info dictionary')
            for key in re.findall(rb'/([A-Za-z0-9_#.-]+)', info):
                key = re.sub(rb'#([0-9A-Fa-f]{2})', lambda m: bytes([int(m[1], 16)]), key)
                info_keys.add(key.decode('utf-8'))
        with pdfium.PdfDocument(data) as document:
            content = []
            for page in document:
                try:
                    text_page = page.get_textpage()
                    try:
                        content.append(text_page.get_text_range())
                    finally:
                        text_page.close()
                finally:
                    page.close()
            info_values = document.get_metadata_dict()
            info_values.update({key: document.get_metadata_value(key) for key in info_keys})
            metadata = [key + ': ' + value for key, value in info_values.items()]
        references = re.findall(rb'/Metadata\s+(\d+)\s+(\d+)\s+R\b', data)
        for ref in references:
            body = objects.get(tuple(map(int, ref)))
            if body is None:
                raise ValueError('Unresolved PDF metadata')
            stream = re.search(rb'\bstream(?:\r\n|\n|\r)', body)
            if stream is None:
                raise ValueError('PDF metadata has no stream')
            header = body[:stream.start()]
            length = re.search(rb'/Length\s+(\d+)(?:\s+(\d+)\s+R)?\b', header)
            if length is None:
                raise ValueError('PDF metadata length missing')
            size = int(length[1])
            if length[2]:
                size = int(objects[(size, int(length[2]))].strip())
            raw = body[stream.end():stream.end() + size]
            if len(raw) != size:
                raise ValueError('PDF metadata stream truncated')
            if b'/Filter' in header:
                if not re.search(rb'/Filter\s*(?:/FlateDecode|\[\s*/FlateDecode\s*\])', header):
                    raise ValueError('Unsupported PDF metadata filter')
                raw = zlib.decompress(raw)
            element = ET.fromstring(raw)
            metadata.append('XMP: ' + ET.tostring(element, encoding='unicode'))
        return '\n'.join(content), '\n'.join(metadata)
    if Path(path).suffix.lower() == '.png':
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG':
                raise ValueError('PNG content mismatch')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()  # populates late text chunks as well
            metadata = [key + ': ' + value for key, value in image.text.items()]
            exif = image.getexif()
            def add_exif(values, label):
                for key, value in values.items():
                    if key in (34665, 34853, 40965):
                        add_exif(exif.get_ifd(key), label + '.' + str(key))
                    else:
                        if isinstance(value, (bytes, bytearray)) or (
                                key in (40091, 40092, 40093, 40094, 40095) and isinstance(value, tuple)):
                            raw_value = bytes(value)
                            value = raw_value.decode('utf-8', errors='replace')
                            if len(raw_value) % 2 == 0 and b'\x00' in raw_value:
                                value += '\n' + raw_value.decode('utf-16-le', errors='replace').rstrip('\x00')
                        metadata.append(label + '.' + str(key) + ': ' + str(value))
            add_exif(exif, 'EXIF')
            return '', '\n'.join(metadata)
    raise ValueError('Unsupported binary type')


def _seed_demo_street_lines(path: str, text: str, pattern) -> set[int]:
    """Only the approved literal at its module binding, never other data on that line.

    Parsing is inert (no execution); any ambiguity or altered value/context fails closed.
    Other privacy kinds and private terms are never waived by this street-only context.
    """
    rule = SEED_DEMO_ADDRESS_CONTEXT
    if path != rule["path"] or rule["marker"] not in text:
        return set()
    try:
        nodes = [n for n in ast.parse(text).body if isinstance(n, ast.Assign)
                 and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id == "SHIPPING_ADDRESS"]
        if len(nodes) != 1 or not isinstance(nodes[0].value, ast.Dict):
            return set()
        values = [v for k, v in zip(nodes[0].value.keys, nodes[0].value.values)
                  if isinstance(k, ast.Constant) and k.value == "address_line_1"]
        if len(values) != 1 or not isinstance(values[0], ast.Constant) or not isinstance(values[0].value, str):
            return set()
        node = values[0]
        if node.lineno != node.end_lineno or hashlib.sha256(node.value.encode()).hexdigest() != rule["sha256"]:
            return set()
        line = text.splitlines()[node.lineno - 1]
        start = len(line.encode()[:node.col_offset].decode())
        end = len(line.encode()[:node.end_col_offset].decode())
        hits = list(pattern.finditer(line))
        return {node.lineno} if hits and all(start <= m.start() and m.end() <= end for m in hits) else set()
    except (SyntaxError, UnicodeError, AttributeError, IndexError):
        return set()


def scan_tree(files: dict[str, bytes], redact, terms: list[str]) -> dict:
    """The final scan over the finished tree. Returns blocking hits, allowed hits, counts. Values are never kept;
    a private-term hit carries only the term's number in the file."""
    blocking, allowed = [], []
    exact_hits = [0] * len(ALLOWED_EXCEPTIONS)
    patterns = list(redact.PATTERNS) + list(redact.DETECT_ONLY_PATTERNS)
    skipped_binary = 0
    scanned_binary = 0

    def term_number(text: str) -> int:
        return next((n for n, term in enumerate(terms, 1) if term in text), 0)

    for path in sorted(files):
        number_in_path = term_number(path.lower())
        if number_in_path:
            blocking.append((path, 0, f"{PRIVATE_TERM_KIND} #{number_in_path}"))
        suffix = Path(path).suffix.lower()
        if suffix in {'.pdf', '.png'}:
            try:
                content, metadata = binary_scan_parts(path, files[path])
            except Exception:
                blocking.append((path, 0, UNSCANNABLE_KIND))
                continue
            scanned_binary += 1
            parts = [(content, True), (metadata, False)]
        elif suffix in redact.BINARY_EXT:
            skipped_binary += 1
            continue
        else:
            try:
                text = files[path].decode("utf-8")
            except UnicodeDecodeError:
                blocking.append((path, 0, UNSCANNABLE_KIND))
                continue
            parts = [(text, True)]
        for text, fixture_exceptions in parts:
            all_lines = text.splitlines()
            seed_streets = _seed_demo_street_lines(path, text, dict(redact.PATTERNS)["street_address"])
            hits = [(n, k) for n, k in redact.scan_text(text) if not _allowed_email_hit(k, all_lines[n - 1])]
            for number, line in enumerate(all_lines, 1):
                if path == '.env.example' and line.strip() and not line.lstrip().startswith('#'):
                    assignment = re.fullmatch(r'([A-Z][A-Z0-9_]*)\s*=\s*(.*)', line.strip())
                    if (not assignment or
                        (assignment[1].startswith('COPILOT_FEATURE_') and assignment[2] not in {'true', 'false'}) or
                        (not assignment[1].startswith('COPILOT_FEATURE_') and assignment[2]
                         and not _template_placeholder(assignment[2]))):
                        hits.append((number, 'template_assignment'))
                hits.extend((number, kind) for kind in _line_hits(line, path))
                term = term_number(line.lower())
                if term:
                    hits.append((number, f"{PRIVATE_TERM_KIND} #{term}"))
            for number, kind in sorted(set(hits)):
                if fixture_exceptions and kind == "street_address" and number in seed_streets:
                    allowed.append((path, number, kind, ("seed-demo-address-context", kind,
                                    "approved DEMO literal at SHIPPING_ADDRESS.address_line_1; exact fingerprint and AST span")))
                    continue
                index = next((i for i, e in enumerate(ALLOWED_EXCEPTIONS)
                              if e["path"] == path and e["kind"] == kind and e["line"] == number), None)
                if fixture_exceptions and index is not None:
                    exact_hits[index] += 1
                    allowed.append((path, number, kind, (path, kind, ALLOWED_EXCEPTIONS[index]["reason"])))
                    continue
                exception = next((e for e in SCAN_EXCEPTIONS if e[1] == kind and match(e[0], path)), None)
                if fixture_exceptions and exception:
                    allowed.append((path, number, kind, exception))
                else:
                    blocking.append((path, number, kind))
    names = {k for k, _ in patterns} | {k for k, _ in _EXTRA_PATTERNS} | set(SCAN_ONLY_KINDS) | {PRIVATE_TERM_KIND, 'template_assignment'}
    return {"blocking": blocking, "allowed": allowed, "skipped_binary": skipped_binary,
            "scanned_binary": scanned_binary, "exact_hits": exact_hits, "pattern_names": sorted(names)}


# T15 scope: the exporter and its test name their own rule paths by nature, so they are skipped.
T15_SKIP = ("tools/export_public.py", "tools/test_export_public.py")


# T15 by rule: path-shaped strings under the private roots, checked against the rules whether or not
# the named file exists at the exported commit (a published file may name a script added in a later commit):
# `internal/...`, `research/...`, `docs/...` (a docs/ file that is not on the public list) and `scratch-*` names count when classify() excludes them, and any path on the private drive `D:\` counts.
# `evidence/...` names are NOT matched by shape: the app writes its own evidence/*.jsonl at run time and names them in
# code (68 functional hits in the first run); they stay covered by the existing check when the file is in the commit.
_PRIVATE_PATH_SHAPE = re.compile(r"(?<![\w/.\\-])(internal/[\w.-]*[\w-](?:/[\w.-]*[\w-])*"
                                 r"|scratch-[\w-]+(?:/[\w.-]*[\w-])*|D:\\[\w.\\-]*[\w-])(?![\w/-])")


def _private_by_rule(name: str) -> bool:
    if name.startswith("D:\\"):
        return True
    return classify(name.replace("\\", "/"))[0] == "excluded"


def old_docs_paths(repo: Path, sha: str) -> list[str]:
    """The OLD docs/ paths that internal/PATH-MAP.md lists (the briefs and logs moved to internal/), read from the
    exported commit; only those the rules exclude. Empty when the commit has no such map."""
    try:
        text = _git(repo, "show", f"{sha}:internal/PATH-MAP.md").decode("utf-8")
    except Exception:        # no map in this commit: nothing to add
        return []
    found = {m.group(1) for m in re.finditer(r"^\|\s*`(docs/[^`*]+)`\s*\|", text, re.M)}
    return sorted(p for p in found if classify(p)[0] == "excluded")


def excluded_path_warnings(outputs: dict[str, bytes], excluded_paths: list[str],
                           exempt_hits: list[int] | None = None) -> list[str]:
    """T15 (a warning, like T14): a plain-text path that names a file this export EXCLUDED, in a published text file
    (T11 only sees Markdown links). Reported as path:line and the named path, never the line itself. The index is not
    checked: it lists private files by design. A hit that matches an entry of T15_FUNCTIONAL_LITERALS on path, literal and
    named file is skipped; when `exempt_hits` is given it receives the index of each entry used (see stale_t15).
    Besides the excluded files of the commit, a path-shaped string under internal/, scratch-* or the private drive D:\
    is reported when the rules exclude it, even if no such file exists (_PRIVATE_PATH_SHAPE)."""
    names = sorted({p for p in excluded_paths if p}, key=len, reverse=True)
    pattern = (re.compile(r"(?<![\w/.-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w/-])")
               if names else None)
    warnings = set()

    def hit(path: str, number: int, name: str, literal: str) -> None:
        entry = next((i for i, e in enumerate(effective_t15())
                      if (e["path"], e["literal"], e["names"]) == (path, literal.strip(), name)), None)
        if entry is not None:
            if exempt_hits is not None:
                exempt_hits.append(entry)
            return
        warnings.add(f"{path}:{number} names excluded file {name} (T15)")

    for path in sorted(outputs):
        if path == INDEX_NAME or path in T15_SKIP:
            continue
        try:
            text = outputs[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            spans = []
            for found in (pattern.finditer(line) if pattern else ()):
                spans.append(found.span(1))
                hit(path, number, found.group(1), line)
            for found in _PRIVATE_PATH_SHAPE.finditer(line):
                start, end = found.span(1)
                if any(start < e and s < end for s, e in spans):      # already reported as an excluded file
                    continue
                if _private_by_rule(found.group(1)):
                    hit(path, number, found.group(1), line)
    return sorted(warnings)


def stale_t15(exempt_hits: list[int]) -> list[str]:
    return [f"stale T15 exemption: {e['path']} {e['names']} matched nothing"
            for i, e in enumerate(effective_t15()) if i not in set(exempt_hits)]


def private_commit_id_warnings(repo: Path, outputs: dict[str, bytes]) -> list[str]:
    """T14 (a warning, never a block): hex tokens of 7-40 characters in an exported .md file (other than the index)
    that resolve to a commit of the private repository. The public repository starts a new history, so such a
    citation points at nothing a judge can open."""
    found: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(outputs):
        if not path.endswith(".md") or path == INDEX_NAME:
            continue
        try:
            text = outputs[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for token in re.findall(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])", line):
                found.setdefault(token, []).append((path, number))
    if not found:
        return []
    names = list(found)
    answer = _git(repo, "cat-file", "--batch-check",
                  stdin=("\n".join(f"{t}^{{commit}}" for t in names) + "\n").encode("ascii")).decode("utf-8").splitlines()
    warnings = []
    for token, reply in zip(names, answer):
        parts = reply.split()
        if len(parts) == 3 and parts[1] == "commit":
            warnings.extend(f"{path}:{number} looks like a private commit id (T14)" for path, number in found[token])
    return sorted(warnings)


# ------------------------------------------------------------------------------------------ T18: hygiene guard
# T18 (a warning, like T14 and T15): work-item, build-round, finding, review and run ids, role and process words, the
# owner's first name, private commit ids and decision ids that do not resolve in the published docs/DECISIONS.md, found
# in the COMMENTS, DOCSTRINGS and PROSE of a published text file. (T16 and T17 are blocking gates, so this one is T18.)
# What is read, by file kind (see _t18_kind): .py comment tokens, docstrings and `--` comments inside a string that holds
# CREATE TABLE; .html, .svg and .json the {# #} and <!-- --> comments; .css and .js the comments; .yaml, .toml, .ini,
# .example, .gitignore, .gitattributes and tools/pre-commit the # comments; .sha256 the comment lines only (never a
# hash line); .md and .txt whole. Data fixtures are not read (T18_DATA_DIRS). A string literal that code or a test uses
# is never read, so it can not be reworded by mistake. A warning names path:line and the family, never the line itself.
T18_SUFFIXES = (".py", ".html", ".svg", ".css", ".js", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".ini",
                ".example", ".sha256")
T18_HASH_COMMENT_NAMES = (".gitignore", ".gitattributes", "pytest.ini", "pre-commit")
T18_DATA_DIRS = ("pilot/fixtures/", "copilot/fixtures/", "pilot/eval/draft_cases/", "pilot/eval/cases/")

T18_FAMILIES = [
    # build-round, work-item and finding ids (the families seen in earlier rounds and the ones later reviews found)
    ("release-label", r"\bRC\d+(?:\.\d+)*(?:-[\w-]+)?\b"),
    ("round-id", r"\b(?:G-SH|SHF|SH|F-SS|J-R5|I-SS-R4|DI|MCS|RACE2|TI)-\d+|\bR-(?:R2C|RACE2|TI|R-C|R)-?[A-Z]?\d+[a-z]?\b"),
    ("round-id", r"\b(?:R[12]|D2|D2-3b?)[- ](?:[A-F]\d+[a-z]?(?:\([ab]\))?|fix\b)|\b[AB]-fix\b|\bA5c\b|\bD2[- ](?:block|\d[a-z]?)\b|\b\d[a-z] F\d+\b"),
    ("round-id", r"\bP[012]-\d+[a-z]?\b|\bU-P1-\d+|\bMU-\d+|\bSCOPE-U-\d+|\bLH\d+(?:-P\d)?\b|\bEST2-\d|\bUX2\b"),
    ("round-id", r"\b(?:OPS16c[a-z]+|FIX-[A-Z]+\d*|INT-FIX\d*|FIX\d+|AUD\d+|SEC\d+|UIC\d+|RULES-US\d+|COPY-[A-Z0-9]+|SHELL-ROLLOUT|VER-[A-Z0-9][A-Z0-9-]*)\b"),
    ("round-id", r"\bDR[0-9](?:-[A-Z]\d|[A-Z]\b)?|\bPart [RU]\b|\bAdenda\b|\bENCARGO\b|\bCRIT-|\bSR-\d|\bCODEX\d|\bSAN\d|\bHOSTED\d|\bEXP\d-n\d"),
    ("round-id", r"(?<![\w.#-])(?:" + r"[A-FJKLMO]\d{1,2}[a-z]?" + r")\s*:|(?<![\w.#-])L\d\d\b"),
    ("round-id", r"(?i)\bpoints? \d[a-z]?\b"),
    ("round-id", r"(?:^|[#(:]\s*|\"\"\")\s*(?:[A-F]\d{1,2}[a-z]?|[ABC]\d-?\d*)\s*$"),
    ("gate-name", r"\bGate D\d?(?:-[abc])?\b|\bD1(?:-[abc])?\b|\bWP-B\b|\bAstra\b|\bSol6\.1\b"),
    ("role-word", r"(?i)\bsupervisors?\b|\bverifiers?\b|\bOpus\b|\bProducto\b|\bCodex\b|\bMaestra\b|\btables? [23]\b|\bthe brief\b"
                  r"|\bbrief's\b|\bBRIEF\.md|\bshell brief\b|\bintegrators?\b|\bintegral review\b|\brounds? \d|\brefuerzo \d"
                  r"|\bstreams? [12]\b|\bstream briefs?\b|\bstreams? flags?\b|\bfix round\b|\bverification round\b"
                  r"|\bthis session\b|\binternal pilot log\b|\bprivate notes\b"
                  r"|\bsponsor-tech\b|\bproject working rules\b|\blane [A-Z0-9]\b"),
    ("owner-name", r"\bJon[aá]s\b"),
]
T18_PATTERNS = [(name, re.compile(rx)) for name, rx in T18_FAMILIES]
T18_CODE_ONLY = {"item-label", "release-label"}       # read in code files only: a document may number its own items and name its release
T18_DECISION_HEADING = re.compile(r"^#+\s*(D-\d{4}-\d\d-\d\d-\d\d)\b", re.M)
T18_DECISION_ID = re.compile(r"\bD-\d{4}-\d\d-\d\d-\d\d\b")
T18_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")        # the T14 shape; the cat-file lookup keeps the noise out
T18_LABEL = re.compile(r"(?<![\w.#@:])[A-Z]{1,6}\d{1,2}[a-z]?(?![\w@])")
T18_NOT_IDS = {"AB12": "a made-up sample tracking code in a comment of pilot/step4_evidence_ai.py"}
T18_SKIP: tuple[str, ...] = ()      # no file is skipped: the exporter and its test are read like every other file
# The exporter's and its test's own rule names (they appear in the warning text itself); anything else of that shape is read.
T18_OWN_RULES = re.compile(r"\b(?:T(?:[1-9]|1[0-8])|X[1-8]|R[1-9])\b")
T18_OWN_VOCAB = {path: T18_OWN_RULES for path in T15_SKIP}


def _item_labels(region: str) -> list[str]:
    """Letter-digit labels (a capital-letter prefix and a number), also when joined to another label or word by a slash
    or a hyphen, and a label before a sentence-final dot. A real path, a decimal or version and a file name are not."""
    found = []
    for m in T18_LABEL.finditer(region):
        token, start, end = m.group(0), m.start(), m.end()
        if token in T18_NOT_IDS:
            continue
        before = region[start - 1] if start else ""
        after = region[end:end + 2]
        if before == "/" and start >= 2 and not region[start - 2].isdigit():
            continue                                                  # a path segment
        if after[:1] == "." and (after[1:2].isalnum() or after[1:2] == "_"):
            continue                                                  # a file name, a domain or a version
        if after[:1] == "/" and not re.match(r"/[A-Z]{1,6}\d", region[end:end + 8]):
            continue                                                  # a path
        if before == "-":
            prev = region[start - 2] if start >= 2 else ""
            if not prev.isalpha():
                continue                                              # a command-line flag (-U0), a number range or a date part
            if prev.isupper() and (start < 3 or not region[start - 3].isalpha()):
                continue                                              # a character range such as A-Z0
        found.append(token)
    return found


# Allowlist: one entry per kept hit. `path` and `family` plus a way to find the line: `line` (an exact line of frozen
# bytes whose hash is their identity), `literal` (a substring of the region) or `starts` (the line begins with this
# phrase), and a `reason`. An entry whose file is not published is ignored; one that matched nothing is reported as
# stale, so the table can not rot silently.
T18_FROZEN_REASON = ("frozen bytes: the hash of this fixture is its identity (a test pins it), so the comment inside it "
                     "can not be reworded")
T18_FROZEN_LINES = {
    # path: {line: the families on that line}
    "tests/features/active_approval/fixtures/package_review-baseline-0.html": {35: ("round-id",), 112: ("role-word",), 171: ("round-id",)},
    "tests/features/active_approval/fixtures/package_review-historic-1.html": {44: ("round-id",), 125: ("role-word",), 186: ("round-id",)},
    "tests/features/active_approval/fixtures/package_review-historic-2.html": {83: ("round-id",), 161: ("role-word",), 294: ("round-id",)},
    "tests/features/active_approval/fixtures/package_review-frozen-3.html": {84: ("round-id",), 162: ("role-word",), 295: ("round-id",)},
    "tests/features/active_approval/fixtures/package_review-frozen-3-delta.json": {189: ("round-id",), 444: ("role-word",), 684: ("round-id",)},     # the 'old' fields
    "tests/features/deadlines/fixtures/disputes_list-frozen-1.html": {4: ("round-id",), 8: ("release-label", "round-id")},
}
T18_ALLOWLIST: list[dict] = [
    *({"path": path, "family": family, "line": line, "reason": T18_FROZEN_REASON}
      for path, lines in T18_FROZEN_LINES.items() for line, families in lines.items() for family in families),
    {"path": "docs/AI-USAGE.md", "family": "role-word", "starts": "- **Opus** sessions acted as supervisor/reviewer: wrote frozen briefs",
     "reason": "the AI-use disclosure names the AI and human roles on purpose"},
    {"path": "docs/AI-USAGE.md", "family": "role-word", "starts": "This project was built with two AI coding agents, **Claude Code**",
     "reason": "the AI-use disclosure names the AI tools on purpose"},
    {"path": "docs/AI-USAGE.md", "family": "role-word", "starts": "- **Codex** sessions planned and supervised two parallel development lanes",
     "reason": "the AI-use disclosure names the AI and human roles on purpose"},
    {"path": "docs/AI-USAGE.md", "family": "owner-name", "starts": "- **Jonás (the owner)** decided scope",
     "reason": "the AI-use disclosure names the human who decided scope on purpose"},
    {"path": "docs/AI-USAGE.md", "family": "role-word", "starts": "it was checked by tests, by the secret scanner",
     "reason": "the AI-use disclosure names the review role on purpose"},
    {"path": "docs/CONFORMANCE.md", "family": "round-id", "literal": "row L3:",
     "reason": "a row label of the document's own source table (the table lists L1 to L5)"},
]


def effective_t18() -> list[dict]:
    """The allowlist entries whose file this export publishes (like effective_t15)."""
    return [e for e in T18_ALLOWLIST if classify(e["path"])[0] != "excluded"]


def _regions(text: str, shapes: list[tuple[str, int]]) -> list[tuple[int, str]]:
    """(line number, piece) for each line of every match of a comment shape."""
    found = []
    for rx, flags in shapes:
        for m in re.finditer(rx, text, flags):
            start = text.count("\n", 0, m.start()) + 1
            for offset, piece in enumerate(m.group(0).split("\n")):
                found.append((start + offset, piece))
    return found


def _py_prose(text: str) -> list[tuple[int, str]] | None:
    """Comments, docstrings and the `--` comments of a CREATE TABLE string; None when the file does not parse."""
    import io
    import tokenize
    found = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                found.append((tok.start[0], tok.string))
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if (body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    for n in range(body[0].lineno, body[0].end_lineno + 1):
                        found.append((n, lines[n - 1]))
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and "CREATE TABLE" in node.value.upper()):
                for n in range(node.lineno, node.end_lineno + 1):       # SQL comments inside the schema string
                    at = lines[n - 1].find("--")
                    if at >= 0:
                        found.append((n, lines[n - 1][at:]))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return None
    return found


def _t18_kind(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    suffix = name[name.rfind("."):].lower() if "." in name else ""
    if name in T18_HASH_COMMENT_NAMES:
        return "hash"
    if suffix == ".sha256":
        return "hash-lines"
    if suffix in (".html", ".svg", ".json"):
        return "markup"
    if suffix in (".css", ".js"):
        return suffix[1:]
    if suffix in (".yaml", ".yml", ".toml", ".ini", ".example"):
        return "hash"
    if suffix in (".py", ".md", ".txt"):
        return suffix[1:]
    return None


def hygiene_prose(path: str, text: str) -> list[tuple[int, str]] | None:
    kind = _t18_kind(path)
    if kind == "py":
        return _py_prose(text)
    if kind in ("md", "txt"):
        return list(enumerate(text.splitlines(), 1))
    if kind == "markup":
        return _regions(text, [(r"<!--.*?-->", re.S), (r"\{#.*?#\}", re.S)])
    if kind in ("css", "js"):
        found = _regions(text, [(r"/\*.*?\*/", re.S)])
        if kind == "js":
            found += _regions(text, [(r"(?:^|(?<=\s))//[^\n]*", re.M)])
        return found
    if kind == "hash-lines":
        return [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith("#")]
    if kind == "hash":
        return [(n, line[line.index("#"):]) for n, line in enumerate(text.splitlines(), 1)
                if re.search(r"(?:^|\s)#", line)]
    return []


def hygiene_warnings(repo: Path | None, outputs: dict[str, bytes], used: list[int] | None = None) -> list[str]:
    """T18: see the block comment above. `repo` resolves hex tokens against the private history (None skips that
    check); `used` receives the index (in effective_t18) of each allowlist entry that matched, for stale_t18."""
    resolvable = set(T18_DECISION_HEADING.findall(outputs.get("docs/DECISIONS.md", b"").decode("utf-8", "replace")))
    hits: dict[tuple[str, int, str], str] = {}
    unreadable: list[str] = []
    hex_tokens: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(outputs):
        if (path == INDEX_NAME or path in T18_SKIP or _t18_kind(path) is None
                or any(path.startswith(d) for d in T18_DATA_DIRS)):
            continue
        try:
            text = outputs[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        prose = hygiene_prose(path, text)
        if prose is None:
            unreadable.append(f"{path} does not parse, so T18 did not read it (T18)")
            continue
        for number, region in prose:
            if path in T18_OWN_VOCAB:
                region = T18_OWN_VOCAB[path].sub(" ", region)
            for family, pattern in T18_PATTERNS:
                if family in T18_CODE_ONLY and path.lower().endswith((".md", ".txt")):
                    continue
                if pattern.search(region):
                    hits.setdefault((path, number, family), region)
            if not path.lower().endswith((".md", ".txt")) and _item_labels(region):
                hits.setdefault((path, number, "item-label"), region)
            for token in T18_DECISION_ID.findall(region):
                if token not in resolvable:
                    hits.setdefault((path, number, "decision-id"), region)
            if repo is not None and not path.endswith(".md"):       # T14 already reads the .md files
                for token in T18_HEX.findall(region):
                    hex_tokens.setdefault(token, []).append((path, number))
    if repo is not None and hex_tokens:
        names = list(hex_tokens)
        answer = _git(repo, "cat-file", "--batch-check",
                      stdin=("\n".join(f"{t}^{{commit}}" for t in names) + "\n").encode("ascii")).decode("utf-8").splitlines()
        for token, reply in zip(names, answer):
            parts = reply.split()
            if len(parts) == 3 and parts[1] == "commit":
                for path, number in hex_tokens[token]:
                    hits.setdefault((path, number, "commit-id"), token)
    by_line: dict[tuple[str, int], dict[str, str]] = {}
    for (path, number, family), region in hits.items():
        by_line.setdefault((path, number), {})[family] = region
    allow = effective_t18()
    warnings = list(unreadable)
    for (path, number), families in sorted(by_line.items()):
        if len(families) > 1:
            families.pop("item-label", None)          # the bare letter-digit label is the weakest reading of a line
        left = []
        for family, region in sorted(families.items()):
            entry = next((i for i, e in enumerate(allow)
                          if e["path"] == path and e["family"] in (family, "*")
                          and (e.get("line") == number
                               or (e.get("literal") and e["literal"] in region)
                               or (e.get("starts") and region.strip().startswith(e["starts"])))), None)
            if entry is None:
                left.append(family)
            elif used is not None:
                used.append(entry)
        if left:
            warnings.append(f"{path}:{number} {'/'.join(left)} in a comment, docstring or prose (T18)")
    return sorted(warnings)


def stale_t18(used: list[int]) -> list[str]:
    return [f"stale T18 allowlist entry: {e['path']} {e.get('line') or e.get('literal') or e.get('starts')} matched nothing"
            for i, e in enumerate(effective_t18()) if i not in set(used)]


def report_path_for(dest: Path) -> Path:
    """`<DEST>.REDACT-SCAN.txt` in the parent folder of the export, never inside the tree."""
    return dest.parent / (dest.name + REPORT_SUFFIX)


def stale_exceptions(scan: dict) -> list[str]:
    return [f"stale exception: {e['path']}:{e['line']} {e['kind']} matched nothing"
            for e, hits in zip(ALLOWED_EXCEPTIONS, scan["exact_hits"]) if not hits]


def render_report(commit: str, scan: dict, warnings: list[str], t15: dict | None = None) -> str:
    lines = [f"export scan of commit {commit}"]
    for path, number, kind in scan["blocking"]:
        lines.append(f"BLOCK {path}:{number} {kind}")
    for path, number, kind, exception in scan["allowed"]:
        lines.append(f"allowed {path}:{number} {kind} ({exception[2]})")
    for warning in warnings:
        lines.append(f"warning: {warning}")
    if t15 is not None:
        lines.append(f"t15_exempt={t15['exempt']} stale={t15['stale']}")
    lines.append(f"blocking={len(scan['blocking'])} allowed={len(scan['allowed'])} "
                 f"binary_files_not_scanned={scan['skipped_binary']} binary_files_scanned={scan['scanned_binary']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------------- export
def read_external_evidence(directory: Path) -> dict[str, bytes]:
    try:
        names = sorted(p.name for p in directory.iterdir()
                       if p.is_file() and p.suffix.lower() in (".jsonl", ".json"))
        return {f"evidence/{name}": (directory / name).read_bytes() for name in names}
    except OSError:
        raise ExportError("the evidence directory is missing or unreadable") from None


# A flag with no feature package of its own: it stays in the template when its owned test package is published.
FLAG_ONLY_NAMESPACES = ("mock_adjudicator",)
# Settings read by core code, not by a feature package: the public template keeps one when the file that reads it is published.
CORE_FLAGS = {"rearm_note_ignored": "copilot/core/disputes.py",
              "seller_shortcuts": "copilot/features/active_approval/routes.py"}


# A flag whose code lives in another feature package (a section or a box of that package, switched by its own flag): the public
# template keeps it with that package, and the anchors test lets the flag's create_app() block import that package.
FLAG_PARENTS = {"fulfilment_register": "fulfilment", "resolution_writes": "resolution",
           "outcome_appeal": "outcome", "webhooks_verify": "webhooks",
           # O4 flags that switch surfaces of a published namespace on; they travel with that namespace
           "attention_model": "local_autopilot", "local_preparation": "local_autopilot", "gap_resolution": "local_autopilot",
           "selected_preparation": "explainable_queue", "incident_proposals": "buyer_note", "closure_summary": "accounting_line",
           # D-101 P3: the dated path is a section of the account page, switched by its own flag
           "account_cliff_path": "account_cliff",
           # D-101 r1: the seeded-case proposal lives in the demo_documents package
           "demo_documents_seeded": "demo_documents",
           # D-101 r2: the inbox watch box lives with the queue it watches
           "inbox_watch": "explainable_queue",
           # D-101 B5: the prepared status message lives in the resolution package
           "resolution_status_message": "resolution"}


def published_env_template(data: bytes, published_paths: set[str]) -> bytes:
    """Keep flag assignments only when their parent feature is actually exported."""
    result = []
    for line in data.splitlines(keepends=True):
        flag = re.match(rb"\s*(?:export\s+)?COPILOT_FEATURE_([A-Z0-9_]+)\s*=", line)
        if flag:
            name = flag.group(1).decode("ascii").lower()
            if name in CORE_FLAGS:
                if CORE_FLAGS[name] in published_paths:
                    result.append(line)
                continue
            namespace = FLAG_PARENTS.get(name, name)
            prefix = f"copilot/features/{namespace}/"
            if namespace in FLAG_ONLY_NAMESPACES:
                prefix = f"tests/features/{namespace}/"
            if not any(path.startswith(prefix) and path.endswith(".py") for path in published_paths):
                continue
        result.append(line)
    return b"".join(result)


def build_outputs(repo: Path, sha: str, evidence_dir: Path | None
                  ) -> tuple[dict[str, bytes], list[dict], list[dict], list[str], object, dict]:
    """Selects and builds every output file in memory. Returns (outputs, manifest files, excluded, warnings, redact,
    t15) where t15 = {"exempt": N, "stale": M} counts the T15 exemptions used and the ones that matched nothing."""
    entries = list_tree(repo, sha)
    needed = {"pilot/client.py", "tools/redact.py", "tools/export_public.py"}
    present = {p for p, _m, _o in entries}
    missing = sorted(needed - present)
    if missing:
        raise ExportError("the exported commit lacks: " + ", ".join(missing))
    client, redact = load_commit_modules(repo, sha, present)
    if not hasattr(client, "sanitize") or not hasattr(redact, "scan_text"):
        raise ExportError("sanitize() or scan_text() is missing at the exported commit")

    decisions, wanted = [], []
    evidence_oids: dict[str, str] = {}
    tracked: dict[str, str] = {}                                  # regular files: path -> blob oid
    claims_oid = None
    for path, mode, oid in entries:
        if path == CLAIMS_PATH and mode not in ("120000", "160000"):
            claims_oid = oid
        if mode in ("100644", "100755"):
            tracked[path] = oid
        if mode in ("100644", "100755") and any(match(p, path) for p in EVIDENCE_FILES):
            evidence_oids[path] = oid
        if mode == "120000":
            decisions.append((path, "excluded", "symlink", oid))
        elif mode == "160000":
            decisions.append((path, "excluded", "submodule", oid))
        else:
            action, rule = classify(path)
            decisions.append((path, action, rule, oid))
            if action != "excluded":
                wanted.append(oid)
    blobs = read_blobs(repo, wanted + ([claims_oid] if claims_oid else []) + list(evidence_oids.values()))

    outputs: dict[str, bytes] = {}
    files, excluded, warnings = [], [], []
    published_paths = {path for path, action, _rule, _oid in decisions if action != "excluded"}
    for path, action, rule, oid in sorted(decisions):
        if action == "excluded":
            excluded.append({"path": path, "rule": rule})
            continue
        data = blobs[oid]
        if path == ".env.example":
            derived = published_env_template(data, published_paths)
            outputs[path] = derived
            entry = {"path": path, "action": "copied" if derived == data else "sanitized",
                     "sha256": sha256(derived)}
            if derived != data:
                entry["source_sha256"] = sha256(data)
            files.append(entry)
        elif action == "copied":
            outputs[path] = data
            files.append({"path": path, "action": "copied", "sha256": sha256(data)})
        elif path.endswith(".jsonl"):
            derived, lines = sanitize_jsonl(data, client.sanitize, path)
            outputs[path] = derived
            files.append({"path": path, "action": "sanitized", "sha256": sha256(derived),
                          "source_sha256": sha256(data), "lines": lines})
        else:
            derived = sanitize_json(data, client.sanitize, path)
            outputs[path] = derived
            files.append({"path": path, "action": "sanitized", "sha256": sha256(derived),
                          "source_sha256": sha256(data)})

    evidence = (read_external_evidence(evidence_dir) if evidence_dir is not None
                else {path: blobs[oid] for path, oid in evidence_oids.items()})
    claims = load_claims(blobs[claims_oid] if claims_oid else None)
    # A claim may cite any tracked file that the rules EXCLUDE (a published file is never described as private)
    extra = {}
    for claim in claims:
        name = claim["file"]
        if name in evidence or name in extra:
            continue
        if (name not in tracked and name.startswith("evidence/") and evidence_dir is None
                and not any(n.startswith("evidence/") for n in evidence)):
            # The evidence files are not in the commit (they live in a private folder), and none was given. An
            # index built from nothing would hide that every claim lost its proof, so this stops the export (exit 2).
            raise ExportError(f"claim {claim['id']} cites {name}, but this commit holds no evidence files: pass "
                              "--evidence-dir <the folder holding the frozen evidence files>")
        if name not in tracked or classify(name)[0] != "excluded":
            raise ExportError(f"claim {claim['id']} cites {name}, which is not an evidence file or an excluded "
                              "tracked file of this source")
        extra[name] = tracked[name]
    if extra:
        by_oid = read_blobs(repo, list(extra.values()))           # de-duplicated by oid: map back by oid
        evidence = {**evidence, **{n: by_oid[oid] for n, oid in extra.items()}}
    if not any(name.startswith("evidence/") for name in evidence):
        warnings.append("no evidence files were found (evidence/*.jsonl, evidence/*.json): the index lists none")
    if not claims:
        warnings.append(f"{CLAIMS_PATH} has no claims: the index lists none")
    index = build_evidence_index(sha, evidence, claims, evidence_dir is not None)
    outputs[INDEX_NAME] = index
    files.append({"path": INDEX_NAME, "action": "generated", "sha256": sha256(index)})
    files.sort(key=lambda f: f["path"])
    forbidden = sorted(p for p in outputs if p.startswith("internal/"))
    if forbidden:                                                  # T16: blocking, nothing is written
        raise ExportError(f"T16: {len(forbidden)} file(s) under internal/ would be copied, first {forbidden[0]}")
    off_list = sorted(p for p in outputs if p.startswith("docs/") and not any(match(g, p) for g in PUBLIC_DOCS))
    if off_list:                                                   # T17: blocking, nothing is written
        raise ExportError(f"T17: {len(off_list)} file(s) under docs/ are not on the public-docs list, first {off_list[0]}")
    warnings.extend(private_commit_id_warnings(repo, outputs))
    exempt_hits: list[int] = []
    warnings.extend(excluded_path_warnings(outputs, [e["path"] for e in excluded] + old_docs_paths(repo, sha), exempt_hits))
    stale = stale_t15(exempt_hits)
    warnings.extend(stale)
    used_t18: list[int] = []
    warnings.extend(hygiene_warnings(repo, outputs, used_t18))
    warnings.extend(stale_t18(used_t18))
    t15 = {"exempt": len(exempt_hits), "stale": len(stale)}
    return outputs, files, excluded, warnings, redact, t15


def excluded_summary(excluded: list[dict]) -> dict:
    """What the public manifest says about the excluded files: no path, only counts and a hash of the full list."""
    by_rule: dict[str, int] = {}
    for entry in excluded:
        by_rule[entry["rule"]] = by_rule.get(entry["rule"], 0) + 1
    lines = sorted(entry["path"] + "\t" + entry["rule"] + "\n" for entry in excluded)
    return {"count": len(excluded), "by_rule": dict(sorted(by_rule.items())),
            "sha256": sha256("".join(lines).encode("utf-8"))}


def excluded_path_for(dest: Path) -> Path:
    """`<DEST>.EXCLUDED.json` in the parent folder of the export, never inside the tree."""
    return dest.parent / (dest.name + EXCLUDED_SUFFIX)


def render_excluded(sha: str, excluded: list[dict]) -> bytes:
    summary = excluded_summary(excluded)
    doc = {"source_commit": sha, "count": summary["count"], "sha256": summary["sha256"], "excluded": excluded}
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_manifest(sha: str, tool_sha: str, files: list[dict], excluded: list[dict], scan: dict,
                   external: bool) -> bytes:
    exceptions = []
    for entry, hits in zip(ALLOWED_EXCEPTIONS, scan["exact_hits"]):
        exceptions.append({"path": entry["path"], "kind": entry["kind"], "line": entry["line"],
                           "reason": entry["reason"], "hits": hits})
    for pattern, kind, why in SCAN_EXCEPTIONS:
        hits = sum(1 for a in scan["allowed"] if a[3][0] == pattern and a[3][1] == kind)
        exceptions.append({"path": pattern, "kind": kind, "line": None, "reason": why, "hits": hits})
    exceptions.append({"path": SEED_DEMO_ADDRESS_CONTEXT["path"], "kind": "street_address", "line": None,
                       "context": SEED_DEMO_ADDRESS_CONTEXT["binding"],
                       "value_sha256": SEED_DEMO_ADDRESS_CONTEXT["sha256"],
                       "reason": "approved DEMO literal, inert AST binding and exact match span",
                       "hits": sum(1 for a in scan["allowed"] if a[3][0] == "seed-demo-address-context")})
    manifest = {
        "schema": SCHEMA, "source_commit": sha, "rules_version": rules_version(), "tool_sha256": tool_sha,
        "files": files, "excluded": excluded_summary(excluded),
        "scan": {"patterns": scan["pattern_names"], "hits": len(scan["blocking"]),
                 "allowed_exceptions": len(scan["allowed"]), "exceptions": exceptions},
    }
    if external:
        manifest["evidence_dir"] = "external"                  # never the real path
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def count_files(repo: Path, commit: str) -> dict:
    """Counts from the git tree alone (nothing is built, nothing is written)."""
    sha = resolve_commit(repo, commit)
    tally = {"published": 0, "sanitized": 0, "excluded": 0, "unmatched": 0}
    for path, mode, _oid in list_tree(repo, sha):
        if mode in ("120000", "160000"):
            tally["excluded"] += 1
            continue
        action, rule = classify(path)
        if action == "excluded":
            tally["excluded"] += 1
            tally["unmatched"] += rule == "default-deny"
        else:
            tally["published"] += 1
            tally["sanitized"] += action == "sanitized"
    tally["published"] += 1                                    # the generated EVIDENCE-INDEX.md
    return tally


def export(repo: Path, dest: Path, commit: str, private_terms: str | None,
           evidence_dir: Path | None = None) -> tuple[int, dict]:
    """Returns (exit code, summary). Raises ExportError for exit 2 (nothing written in that case)."""
    repo, dest = repo.resolve(), dest.resolve()
    sha = resolve_commit(repo, commit)
    if dest.exists():
        raise ExportError("the destination already exists (refusing to merge into or overwrite it)")
    terms = read_private_terms(private_terms)
    outputs, files, excluded, warnings, redact, t15 = build_outputs(repo, sha, evidence_dir)
    tool_sha = sha256(_git(repo, "show", f"{sha}:tools/export_public.py"))

    dest.mkdir(parents=True)
    for path, data in outputs.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # the finished tree is scanned from what was written to disk
    on_disk = {p.relative_to(dest).as_posix(): p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()}
    scan = scan_tree(on_disk, redact, terms)
    summary = {"commit": sha, "copied": sum(1 for f in files if f["action"] == "copied"),
               "sanitized": sum(1 for f in files if f["action"] == "sanitized"),
               "generated": sum(1 for f in files if f["action"] == "generated"), "excluded": len(excluded),
               "unmatched": sum(1 for e in excluded if e["rule"] == "default-deny"),
               "blocking": len(scan["blocking"]), "allowed": len(scan["allowed"]), "warnings": len(warnings)}
    report_path = report_path_for(dest)
    warnings.extend(stale_exceptions(scan))
    summary["warnings"] = len(warnings)
    if scan["blocking"]:
        report_path.write_text(render_report(sha, scan, warnings, t15), encoding="utf-8")
        return 1, summary
    manifest = build_manifest(sha, tool_sha, files, excluded, scan, evidence_dir is not None)
    extra = scan_tree({"MANIFEST.json": manifest}, redact, terms)           # the manifest is scanned too
    if extra["blocking"]:
        scan["blocking"].extend(extra["blocking"])
        report_path.write_text(render_report(sha, scan, warnings, t15), encoding="utf-8")
        summary["blocking"] = len(scan["blocking"])
        return 1, summary
    (dest / "MANIFEST.json").write_bytes(manifest)
    excluded_path_for(dest).write_bytes(render_excluded(sha, excluded))
    report_path.write_text(render_report(sha, scan, warnings, t15), encoding="utf-8")
    return 0, summary


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", type=Path, nargs="?", help="destination directory - must not already exist")
    ap.add_argument("--commit", required=True, help="commit-ish to export; resolved to a full 40-hex SHA")
    ap.add_argument("--private-terms", default=None,
                    help="file OUTSIDE the repository with one private term per line (or set %s)" % PRIVATE_TERMS_ENV)
    ap.add_argument("--evidence-dir", type=Path, default=None,
                    help="read the private evidence files from this directory instead of the commit (recorded as "
                         "'external' in the manifest)")
    ap.add_argument("--counts-only", action="store_true",
                    help="print the counts from the git tree; builds and writes nothing")
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                    help="repository to export from (default: this script's own repository)")
    a = ap.parse_args(argv)
    if a.counts_only:
        try:
            tally = count_files(a.repo.resolve(), a.commit)
        except ExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(" ".join(f"{k}={v}" for k, v in tally.items()))
        return 0
    if a.dest is None:
        print("error: DEST is required unless --counts-only is given", file=sys.stderr)
        return 2
    try:
        code, summary = export(a.repo, a.dest, a.commit, a.private_terms, a.evidence_dir)
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("commit={commit} copied={copied} sanitized={sanitized} generated={generated} excluded={excluded} "
          "unmatched={unmatched} blocking={blocking} allowed={allowed} warnings={warnings}".format(**summary))
    if code:
        print("export BLOCKED by the final scan: see <DEST>" + REPORT_SUFFIX + " next to the export directory",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
