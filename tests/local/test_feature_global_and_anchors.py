"""The two-stream contract: the `feature_enabled` Jinja global is defined in both template
environments (so a flag-guarded include line never raises with every flag off); with every feature flag off (unset, or set
to any value config.feature_enabled reads as off) the inbox, a dispute page and the package review page are byte-equal;
turning one feature flag on alone still renders the three pages, and changes no byte of a page whose templates never name
that flag (the names are read from the templates and copilot/app.py, not listed here); and inside create_app() the two
two anchor comments split the feature blocks into regions: the six original blocks first, the first group
between the anchors, the second group after the second anchor, each feature import inside its own flag's block.
Offline: a fake client replays the synthetic sandbox-shaped bodies of pilot/fixtures/synthetic; each rendering gets its own
fresh database, a fixed CSRF token and a fixed "checked at" label, so two renderings of the same state are byte-equal."""
from _hosted import authenticated_client, reset_csrf
import json
import inspect
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.web import routes_disputes, routes_package
from copilot.web.routes_disputes import get_client
from copilot.web.security import CSRF_COOKIE_NAME
from tools import export_public as ep   # FLAG_PARENTS: the one map of flags whose code lives in another feature package

REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1 = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
ENVS = {"routes_disputes": routes_disputes.templates.env, "routes_package": routes_package.templates.env}
APP_PY = ROOT / "copilot" / "app.py"
TEMPLATES = ROOT / "copilot" / "web" / "templates"
# The feature flags of the two streams.
STREAM_FLAGS = ("CLAIMS", "ACTIVE_APPROVAL", "TRIAGE", "LEDGER", "PACKAGE_PRINT", "DELIVERY_FACTS", "DEMO_DOCUMENTS", "CONFORMANCE", "EVENTS", "DEADLINES", "CARRIER_REQUIRED", "JUDGE_AS_BUYER", "BUYER_NOTE", "SHIP_MOMENT_CAPTURE", "CARRIER_DIRECTORY", "SALE_LESSON", "CALL_SCRIPT", "READ_ALOUD_REVIEW", "SECOND_OPINION", "ACCOUNTING_LINE", "OFFLINE_MARKET_MODE", "DECLARED_LIMITS", "PRESENTATION_MODE", "TWO_CLOCKS", "DO_NOTHING_PATH", "PACKAGE_INSERT", "SHIP_NOTICE", "FIRST_DISPUTE_GUIDE", "TRADE_PROFILES", "LESSONS_EXCHANGE", "SEASON_PREP", "LOST_PACKAGE_PATHS", "THIRD_PARTY_STATEMENT", "TRANSIT_DELIVERED_GUIDE", "CASE_HISTORY", "STATUS_LINK", "ACCEPT_CLAIM")
ANCHOR_FIRST = "    # --- feature blocks, first group: below this line ---"
ANCHOR_SECOND = "    # --- feature blocks, second group: below this line ---"
# The feature blocks of create_app(), by region: the six that existed before the streams (in this order), then each
# stream's own names.
ORIGINAL_BLOCKS = ("account_cliff", "fulfilment", "resolution", "outcome", "webhooks", "draft")
STREAM1_BLOCKS = ("claims", "active_approval", "triage", "ledger", "package_print", "delivery_facts")
STREAM2_BLOCKS = ("conformance", "events", "deadlines", "carrier_required", "replay", "bilingual", "demo_documents",
                  # local tools, appended after the streams (declared change, D-68)
                  "evidence_dossier", "explainable_queue", "negotiation_drafts", "sale_readiness", "appeal_workspace",
                  "mcp_local_contracts", "evaluation_workbench", "judge_walkthrough", "local_autopilot", "local_demo_hub",
                  # Nivel2 authorized additions; keep their existing region ownership visible to this guard.
                  "judge_as_buyer", "buyer_page", "buyer_note", "ship_moment_capture", "carrier_directory", "sale_lesson", "call_script", "read_aloud_review", "second_opinion", "accounting_line", "offline_market_mode", "declared_limits", "presentation_mode", "two_clocks", "do_nothing_path", "package_insert", "ship_notice", "first_dispute_guide", "trade_profiles", "lessons_exchange", "season_prep", "lost_package_paths", "third_party_statement", "transit_delivered_guide", "case_history", "judge_pool", "inbox_watch", "demo_documents_seeded", "status_link", "accept_claim")
# Values config.feature_enabled reads as off: only the word "true" (any case, outer spaces ignored) turns a flag on.
OFF_VALUES = ("false", "0", "", " ", "1", "yes", "on", "False")


class Fake:
    def call(self, method, path, op, **kw):
        if op == "list_disputes":
            return LIST_PAGE1
        if op == "show_dispute":
            return WAITING
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _render_responses(tmp_path, tag) -> dict:
    """The three pages' responses, rendered once against a fresh database named by tag; each must be a 200."""
    config.set_db_path_override(tmp_path / f"{tag}.db")
    config.set_uploads_dir_override(tmp_path / f"{tag}-uploads")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    c = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(c, secrets.token_urlsafe(32))
    db = config.db_path()
    repo.insert_document(db, DID, "label.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    pid = repo.insert_package(db, DID, source="human_edit", validation_ok=False, review_flags=[])
    pages = {"inbox": c.get("/"), "dispute": c.get(f"/disputes/{DID}"), "prepare": c.get(f"/disputes/{DID}/workspace/prepare"),
             "package": c.get(f"/disputes/{DID}/packages/{pid}")}
    for name, r in pages.items():
        assert r.status_code == 200, (tag, name, r.status_code)
    return pages


def _render_three(tmp_path, tag) -> dict:
    """The three pages' bytes, rendered once against a fresh database named by tag."""
    return {name: r.content for name, r in _render_responses(tmp_path, tag).items()}


def _fixed(monkeypatch):
    from datetime import datetime, timezone

    fixed_instant = datetime(2026, 10, 23, 2, 13, 16, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_instant.replace(tzinfo=None) if tz is None else fixed_instant.astimezone(tz)

    monkeypatch.setattr(routes_disputes, "datetime", FixedDateTime)
    import importlib   # by name, so that the export closure check sees no static import of a feature package from this published test
    try:
        queue_service = importlib.import_module("copilot.features.explainable_queue.service")
    except ImportError:   # a tree without that lane
        queue_service = None
    if queue_service is not None:   # its deadline state reads the wall clock too; without this the inbox bytes flip at the hour mark of the sample deadline
        monkeypatch.setattr(queue_service, "datetime", FixedDateTime)
    monkeypatch.setattr(routes_disputes, "new_csrf_token", lambda: "FIXED-TOKEN")
    monkeypatch.setattr(routes_package, "new_csrf_token", lambda: "FIXED-TOKEN")
    monkeypatch.setattr(routes_disputes, "_checked_at_label", lambda: "FIXED-TIME")
    # The new timeline displays the persisted document/package times as well.
    monkeypatch.setattr(repo, "_now", lambda: "2026-10-07T12:00:00+0000")
    for flag in STREAM_FLAGS:
        monkeypatch.delenv("COPILOT_FEATURE_" + flag, raising=False)


def _flag_key(name: str) -> str:
    # The same normalisation config.feature_enabled applies to its argument.
    return name.strip().upper().replace("-", "_")


_NAMED = re.compile(r"""feature_enabled\(\s*["']([^"']+)["']\s*\)""")
_TEMPLATE_REF = re.compile(r"""\{%-?\s*(?:extends|include|import|from)\s+["']([^"']+)["']""")


def _flags_named_in(text: str) -> set:
    return {_flag_key(name) for name in _NAMED.findall(text)}


def _assert_declared_excluded_template(name: str):
    """An absent optional include is legitimate only under the exact export inventory."""
    _feature_availability(ROOT)  # schema, paths, duplicates, complete runtime inventories, physical absence
    manifest = ROOT / "MANIFEST.json"
    assert manifest.is_file() and not manifest.is_symlink(), "Absent template requires export inventory"
    inventory = json.loads(manifest.read_text(encoding="utf-8"))
    relative = (TEMPLATES / name).relative_to(ROOT).as_posix()
    parts = PurePosixPath(relative).parts
    assert len(parts) >= 6 and parts[:4] == ("copilot", "web", "templates", "features"), "Missing core template"
    namespace = parts[4]
    assert namespace in set(ORIGINAL_BLOCKS) | {flag.lower() for flag in STREAM_FLAGS}, "Unknown template namespace"
    prefix = "copilot/web/templates/features/" + namespace + "/"
    assert not any(entry["path"].startswith(prefix) for entry in inventory["files"]), "Partial template namespace"
    matches = [entry for entry in inventory["excluded"] if entry["path"] == relative]
    assert len(matches) == 1, "Undeclared template exclusion"
    assert matches[0]["rule"] in {"exclude:copilot/web/templates/features/**",
                                  "exclude:copilot/web/templates/features/" + namespace + "/**"}, "Wrong template exclusion rule"
    assert not (TEMPLATES / "features" / namespace).exists(), "Excluded template namespace present"


def _template_closure(name: str) -> set:
    """The template and every template it extends, includes or imports, transitively (string-literal names)."""
    seen, todo = set(), [name]
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        target = TEMPLATES / current
        if not target.is_file():
            _assert_declared_excluded_template(current)
            continue
        assert not target.is_symlink(), "Linked template source"
        seen.add(current)
        todo.extend(_TEMPLATE_REF.findall(target.read_text(encoding="utf-8")))
    return seen


def _flags_named_by_page_templates(root_template: str) -> set:
    names=set().union(*(_flags_named_in((TEMPLATES / t).read_text(encoding="utf-8"))
                       for t in _template_closure(root_template)))
    if root_template in ("dispute_detail.html", "case_task.html"):
        # next-task URLs and the case navigation are prepared here, not by a template flag call.
        from copilot.web import case_workspace
        names |= _flags_named_in(inspect.getsource(routes_disputes._case_tasks))
        names |= _flags_named_in(inspect.getsource(case_workspace))
    return names


def _flags_named_anywhere() -> set:
    names = _flags_named_in(APP_PY.read_text(encoding="utf-8"))
    for path in TEMPLATES.rglob("*.html"):
        names |= _flags_named_in(path.read_text(encoding="utf-8"))
    return names



# Frozen map of the required namespaces, declared literally; never inferred from imports or exporter values.
_PORTABLE_REQUIRES = {
    "accept_claim": frozenset({"resolution"}), "account_cliff": frozenset(), "active_approval": frozenset({"claims", "carrier_required"}),
    "carrier_required": frozenset(), "claims": frozenset(),
    "conformance": frozenset({"account_cliff", "fulfilment", "outcome", "resolution", "webhooks"}),
    "deadlines": frozenset({"events"}), "delivery_facts": frozenset({"draft"}),
    "demo_documents": frozenset(),
    "draft": frozenset({"resolution"}), "events": frozenset(), "fulfilment": frozenset(),
    "ledger": frozenset(), "outcome": frozenset({"fulfilment", "resolution"}),
    "package_print": frozenset(), "resolution": frozenset({"account_cliff"}),
    "triage": frozenset({"claims", "resolution", "clauses"}), "webhooks": frozenset(),
    "bilingual": frozenset(), "clauses": frozenset(), "replay": frozenset(), "timeline": frozenset(),
    # The ten lane namespaces, published on 2026-10-24; their edges are the ones the lazy-import tests allow.
    "appeal_workspace": frozenset(), "evaluation_workbench": frozenset(), "evidence_dossier": frozenset(),
    "explainable_queue": frozenset({"webhooks"}), "judge_walkthrough": frozenset(),
    "local_autopilot": frozenset({"resolution"}), "local_demo_hub": frozenset({"local_autopilot", "mcp_local_contracts"}),
    "mcp_local_contracts": frozenset(), "negotiation_drafts": frozenset({"local_autopilot", "resolution"}),
    "sale_readiness": frozenset({"fulfilment"}),
    "judge_as_buyer": frozenset(), "buyer_note": frozenset({"trade_profiles"}),
    "ship_moment_capture": frozenset(), "carrier_directory": frozenset(), "sale_lesson": frozenset({"outcome"}),
    "call_script": frozenset(), "read_aloud_review": frozenset(), "second_opinion": frozenset(),
    "accounting_line": frozenset({"account_cliff", "ledger", "outcome"}),
    "offline_market_mode": frozenset(), "declared_limits": frozenset(), "presentation_mode": frozenset({"evaluation_workbench"}),
    "two_clocks": frozenset({"sale_readiness"}),
    "do_nothing_path": frozenset({"resolution", "account_cliff"}),
    "package_insert": frozenset({"carrier_directory", "fulfilment", "trade_profiles", "lessons_exchange"}),
    "ship_notice": frozenset({"buyer_note", "carrier_directory", "fulfilment"}),
    "first_dispute_guide": frozenset(),
    "trade_profiles": frozenset({"package_insert", "sale_readiness"}),
    "lessons_exchange": frozenset({"sale_lesson", "trade_profiles", "package_insert"}),
    "season_prep": frozenset({"carrier_directory", "fulfilment"}),
    "lost_package_paths": frozenset({"carrier_directory", "fulfilment"}),
    "third_party_statement": frozenset(), "transit_delivered_guide": frozenset(), "case_history": frozenset(), "status_link": frozenset(),
    # Shipped in the kit and read here only once a MANIFEST.json exists; their lazy imports are the edges.
    "buyer_page": frozenset({"resolution", "negotiation_drafts"}), "case_reentry": frozenset(), "judge_pool": frozenset({"accept_claim"}),
}


def _assert_import_inventory(flag, included, probe, activation_allowed=True):
    target = "copilot.features." + flag.lower()
    def target_modules(names):
        return {name for name in names if name == target or name.startswith(target + ".")}
    assert not target_modules(probe["off"]), "Feature imported before its flag was enabled"
    if included and not activation_allowed:
        assert probe["failure"] is None, "Hosted-only feature failed during local no-op activation"
        assert not target_modules(probe["on"]), "Hosted-only feature imported in local mode"
        assert probe["restored_routes"] == probe["off_routes"], "Local flag changed the route table"
    elif included:
        assert probe["failure"] is None, "Included feature activation failed"
        assert target + ".routes" in target_modules(probe["on"]), "Included feature was not imported lazily"
    else:
        assert not target_modules(probe["on"]), "Excluded feature imported on activation attempt"
        assert probe["failure"] in {"copilot.features", target, target + ".routes"}, "Unrelated import failure"
        assert probe["restored_routes"] == probe["off_routes"], "Failed activation changed baseline routes"


def _isolated_import_inventory(flag, included, activation_allowed=True):
    # Fresh process, no sys.modules deletion; no request, client, network or model call.
    script = """import json, os, sys
from copilot.app import create_app
flag = sys.argv[1]
off_app = create_app()
route = lambda app: [(getattr(r, 'path', None), sorted(getattr(r, 'methods', None) or [])) for r in app.routes]
probe = {'off': sorted(sys.modules), 'off_routes': route(off_app), 'failure': None}
os.environ['COPILOT_FEATURE_' + flag] = 'true'
try:
    create_app()
except ModuleNotFoundError as error:
    probe['failure'] = error.name
probe['on'] = sorted(sys.modules)
os.environ.pop('COPILOT_FEATURE_' + flag)
probe['restored_routes'] = route(create_app())
print(json.dumps(probe))
"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COPILOT_FEATURE_", "PAYPAL_", "OLLAMA_"))}
    env.update(PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", script, flag], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=True)
    _assert_import_inventory(flag, included, json.loads(result.stdout), activation_allowed)


def _feature_availability(root=ROOT) -> set:
    """Missing code is intentional only when the export inventory declares its exclusion."""
    manifest = root / "MANIFEST.json"
    assert not manifest.is_symlink(), "Linked export manifest"
    if manifest.is_file():
        inventory = json.loads(manifest.read_text(encoding="utf-8"))
        assert inventory.get("schema") == "proofie-export/1", "Invalid export manifest schema"
        included, excluded, summary = {}, {}, None
        sources = (("files", included), ("excluded", excluded))
        if isinstance(inventory.get("excluded"), dict):
            # Public form: no excluded path is named, only {count, by_rule, sha256}.
            summary = inventory["excluded"]
            by_rule = summary.get("by_rule")
            assert isinstance(summary.get("count"), int) and not isinstance(summary.get("count"), bool), "Invalid manifest excluded summary"
            assert isinstance(by_rule, dict) and all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                                                     for k, v in by_rule.items()), "Invalid manifest excluded summary"
            assert isinstance(summary.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", summary["sha256"]), "Invalid manifest excluded summary"
            sources = (("files", included),)
        for key, target in sources:
            assert isinstance(inventory.get(key), list), f"Missing manifest {key} inventory"
            for entry in inventory[key]:
                path = entry["path"]
                parts = PurePosixPath(path).parts
                assert (parts and path == PurePosixPath(path).as_posix() and not PurePosixPath(path).is_absolute() and ".." not in parts
                        and "\\" not in path and ":" not in path), "Unsafe manifest path"
                assert path not in included and path not in excluded, "Duplicate manifest path"
                target[path] = entry
        available = set()
        for flag in STREAM_FLAGS:
            namespace = ("copilot", "features", flag.lower())
            entrypoint = PurePosixPath(*namespace, "routes.py").as_posix()
            if summary is not None:
                shipped = {p for p in included if PurePosixPath(p).parts[:3] == namespace}
                if shipped:
                    assert entrypoint in included, f"Partial exported namespace: {flag}"
                    on_disk = {path.relative_to(root).as_posix() for path in root.joinpath(*namespace).rglob("*")
                               if not path.is_dir() and "__pycache__" not in path.parts}
                    assert on_disk <= shipped, f"Partial exported namespace: {flag}"
                    assert not any(rule.startswith("exclude:copilot/features/" + flag.lower() + "/")
                                   or rule == "exclude:copilot/features/**" for rule in summary["by_rule"]), \
                        f"Partial exported namespace: {flag}"
                    for path in shipped:
                        source = root / path
                        assert source.is_file() and not source.is_symlink(), f"Missing exported source: {path}"
                    available.add(flag)
                else:
                    legitimate_rules = ("exclude:copilot/features/**", "exclude:copilot/features/" + flag.lower() + "/**")
                    assert any(summary["by_rule"].get(rule, 0) > 0 for rule in legitimate_rules), f"Undeclared namespace: {flag}"
                    assert not root.joinpath(*namespace).exists(), f"Excluded namespace present: {flag}"
                continue
            assert entrypoint in included or entrypoint in excluded, f"Undeclared namespace: {flag}"
            members = {p for p in included.keys() | excluded.keys()
                       if PurePosixPath(p).parts[:3] == namespace}
            shipped = members & included.keys()
            if shipped:
                assert members == shipped, f"Partial exported namespace: {flag}"
                for path in shipped:
                    source = root / path
                    assert source.is_file() and not source.is_symlink(), f"Missing exported source: {path}"
                available.add(flag)
            else:
                legitimate_rules = {"exclude:copilot/features/**",
                                    "exclude:copilot/features/" + flag.lower() + "/**"}
                assert all(excluded[p]["rule"] in legitimate_rules for p in members), flag
                assert not root.joinpath(*namespace).exists(), f"Excluded namespace present: {flag}"
        shipped_names = {PurePosixPath(path).parts[2] for path in included
                         if PurePosixPath(path).parts[:2] == ("copilot", "features")
                         and len(PurePosixPath(path).parts) >= 4}
        assert shipped_names <= _PORTABLE_REQUIRES.keys(), "Unknown included namespace"
        for name in shipped_names:
            assert _PORTABLE_REQUIRES[name] <= shipped_names, "Incomplete namespace dependency closure"
            prefix = ("copilot", "features", name)
            members = {path for path in included.keys() | excluded.keys()
                       if PurePosixPath(path).parts[:3] == prefix}
            assert members <= included.keys(), "Partial dependency namespace"
            for path in members:
                assert root.joinpath(path).is_file() and not root.joinpath(path).is_symlink(), "Missing dependency source"
        return available
    assert (root / ".git").exists(), "Feature inventory requires Git or a valid export manifest"
    paths = subprocess.run(["git", "ls-files", "-z", "--", "copilot/features"], cwd=root,
                           capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    for flag in STREAM_FLAGS:
        entrypoint = PurePosixPath("copilot", "features", flag.lower(), "routes.py").as_posix()
        assert entrypoint in paths, f"Missing tracked namespace: {flag}"
    for path in filter(None, paths):
        source = root / path
        assert source.is_file() and not source.is_symlink(), f"Missing tracked source: {path}"
    return set(STREAM_FLAGS)


def test_demo_documents_is_in_stream2_and_portable_namespace_contract():
    assert "demo_documents" in STREAM2_BLOCKS
    assert _PORTABLE_REQUIRES["demo_documents"] == frozenset()


@pytest.mark.parametrize("damage", [None, "undeclared", "present", "wrong-rule", "missing-source"])
def test_feature_inventory_distinguishes_declared_exclusion_from_corruption(tmp_path, damage):
    excluded = [{"path": PurePosixPath("copilot", "features", flag.lower(), "routes.py").as_posix(),
                 "rule": "exclude:copilot/features/**"} for flag in STREAM_FLAGS]
    inventory = {"schema": "proofie-export/1", "files": [], "excluded": excluded}
    if damage == "undeclared":
        excluded.pop()
    elif damage == "present":
        tmp_path.joinpath("copilot", "features", "claims").mkdir(parents=True)
    elif damage == "wrong-rule":
        excluded[0]["rule"] = "default-deny"
    elif damage == "missing-source":
        inventory["files"] = [excluded.pop(0)]
    (tmp_path / "MANIFEST.json").write_text(json.dumps(inventory), encoding="utf-8")
    if damage is None:
        assert _feature_availability(tmp_path) == set()
    else:
        with pytest.raises(AssertionError):
            _feature_availability(tmp_path)


def test_the_global_is_defined_in_both_environments_and_is_the_config_reader():
    for name, env in ENVS.items():
        assert env.globals.get("feature_enabled") is config.feature_enabled, name


def test_a_guarded_include_form_renders_nothing_with_the_flag_off_and_never_raises(monkeypatch):
    monkeypatch.delenv("COPILOT_FEATURE_ACTIVE_APPROVAL", raising=False)
    for name, env in ENVS.items():
        tpl = env.from_string('a\n{%- if feature_enabled("active_approval") %}X{% endif %}\nb')
        assert tpl.render() == "a\nb", name
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "true")
    for name, env in ENVS.items():
        tpl = env.from_string('a\n{%- if feature_enabled("active_approval") %}X{% endif %}\nb')
        assert tpl.render() == "aX\nb", name


def test_two_renderings_of_the_same_state_are_byte_equal(tmp_path, monkeypatch):
    # Guards the comparisons below: if the pages were not deterministic, "equal" would prove nothing.
    _fixed(monkeypatch)
    assert _render_three(tmp_path, "first") == _render_three(tmp_path, "second")


def test_due_label_tracks_route_clock_across_48_hour_boundary(tmp_path, monkeypatch, web_mode):
    from datetime import datetime, timezone

    _fixed(monkeypatch)
    list_items = [item for item in LIST_PAGE1["items"] if item.get("dispute_id") == DID]
    assert len(list_items) == 1
    assert list_items[0]["seller_response_due_date"] == "2026-10-25T02:13:17.000Z"
    assert WAITING["seller_response_due_date"] == "2026-10-25T02:13:17.163Z"

    before = datetime(2026, 10, 23, 2, 13, 16, tzinfo=timezone.utc)
    after = datetime(2026, 10, 23, 2, 13, 18, tzinfo=timezone.utc)

    def render_at(tag, instant):
        class ScenarioDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

        monkeypatch.setattr(routes_disputes, "datetime", ScenarioDateTime)
        return _render_three(tmp_path, tag)

    first = render_at("before-due-boundary", before)
    second = render_at("after-due-boundary", after)
    for page in ("inbox", "dispute"):
        assert b"2 days left" in first[page]
        assert b"Due in 47 hours" in second[page]
        assert first[page] != second[page]
    assert first["package"] == second["package"]


def test_the_off_values_used_below_are_read_as_off_and_only_the_word_true_turns_a_flag_on(monkeypatch):
    for value in OFF_VALUES:
        monkeypatch.setenv("COPILOT_FEATURE_CLAIMS", value)
        assert config.feature_enabled("claims") is False, repr(value)
    for value in ("true", "TRUE", " True "):
        monkeypatch.setenv("COPILOT_FEATURE_CLAIMS", value)
        assert config.feature_enabled("claims") is True, repr(value)


@pytest.mark.parametrize("value", OFF_VALUES, ids=[f"off{i}" for i in range(len(OFF_VALUES))])
def test_every_stream_flag_set_to_an_off_value_changes_no_byte_of_the_three_pages(tmp_path, monkeypatch, value):
    _fixed(monkeypatch)
    unset = _render_three(tmp_path, "unset")
    for flag in STREAM_FLAGS:
        monkeypatch.setenv("COPILOT_FEATURE_" + flag, value)
    explicit_off = _render_three(tmp_path, "explicit-off")
    assert explicit_off == unset
    assert all(b"FIXED-TOKEN" in page for page in (unset["prepare"], unset["package"]))
    for name, env in ENVS.items():                  # the global is kept while the flags are off
        assert env.globals.get("feature_enabled") is config.feature_enabled, name


@pytest.mark.parametrize("flag", STREAM_FLAGS)
def test_one_stream_flag_on_alone_renders_the_pages_and_leaves_the_pages_that_never_name_it_unchanged(
        tmp_path, monkeypatch, flag, request):
    _fixed(monkeypatch)
    available = _feature_availability()  # inventory and closure before any activation probe
    _isolated_import_inventory(flag, flag in available, flag != "DEMO_DOCUMENTS" or config.hosted_demo())
    off = _render_responses(tmp_path, "off")
    if flag not in available:
        request.node.user_properties.append(("feature_on_unavailable",
                                            f"{flag}: intentionally excluded by export manifest"))
        assert all(not config.feature_enabled(name) for name in STREAM_FLAGS)
        excluded = "copilot.features." + flag.lower()
        assert not any(name == excluded or name.startswith(excluded + ".") for name in sys.modules)
        routes = [(getattr(r, "path", None), getattr(r, "methods", None)) for r in create_app().routes]
        assert _render_three(tmp_path, "export-off-" + flag.lower()) == {name: r.content for name, r in off.items()}
        assert [(getattr(r, "path", None), getattr(r, "methods", None)) for r in create_app().routes] == routes
        assert _region_problems(APP_PY.read_text(encoding="utf-8")) == []
        return
    monkeypatch.setenv("COPILOT_FEATURE_" + flag, "true")   # this flag alone; the other feature flags stay unset
    on = _render_three(tmp_path, "on-" + flag.lower())        # status 200 for each page is asserted inside
    if flag not in _flags_named_anywhere():
        # Still inert: no template and no create_app() block reads it.
        assert on == {name: r.content for name, r in off.items()}
        return
    for name, r in off.items():
        root_template = r.template.name
        if flag not in _flags_named_by_page_templates(root_template):
            assert on[name] == r.content, (flag, name, root_template)


# --- the regions of create_app() ---------------------------------------------------------------------------------------
_BLOCK = re.compile(r"""^    if config\.feature_enabled\(\s*["']([^"']+)["']\s*\):\s*(#.*)?$""")
_FEATURE_IMPORT = re.compile(r"^\s*(?:from|import)\s+copilot\.features\b")
_FEATURE_IMPORT_NAME = re.compile(
    r"^\s*(?:from\s+copilot\.features\.(\w+)|import\s+copilot\.features\.(\w+)|from\s+copilot\.features\s+import\s+(\w+))")


def _region_problems(text: str) -> list:
    """Every way the create_app() feature blocks of this app.py source break the contract, as 'line N: ...' strings."""
    lines = text.splitlines()
    problems = []
    if lines.count(ANCHOR_FIRST) != 1 or lines.count(ANCHOR_SECOND) != 1:
        return [f"anchors: first {lines.count(ANCHOR_FIRST)} time(s), second {lines.count(ANCHOR_SECOND)} time(s), expected 1 each"]
    start = next((i for i, line in enumerate(lines) if line.startswith("def create_app(")), None)
    if start is None:
        return ["no create_app()"]
    root = next((i for i in range(start, len(lines)) if lines[i].strip().startswith('@app.get("/"')), None)
    if root is None:
        return ["no root route in create_app()"]
    a, s = lines.index(ANCHOR_FIRST), lines.index(ANCHOR_SECOND)
    if not start < a < s < root:
        return [f"anchors out of order: create_app line {start + 1}, first {a + 1}, second {s + 1}, root route {root + 1}"]
    regions = {**{n: "original" for n in ORIGINAL_BLOCKS}, **{n: "stream1" for n in STREAM1_BLOCKS},
               **{n: "stream2" for n in STREAM2_BLOCKS}}
    first = next((i for i in range(start, root) if _BLOCK.match(lines[i])), a)
    blocks, current, owner = [], None, {}    # owner: line index of a body line -> its block's flag name
    for i in range(min(first, a), root):
        line = lines[i]
        here = "original" if i < a else ("stream1" if i < s else "stream2")
        m = _BLOCK.match(line)
        if m:
            name = m.group(1)
            current = name
            blocks.append(name)
            want = regions.get(name)
            if want is None:
                problems.append(f"line {i + 1}: unknown feature block {name!r}")
            elif want != here:
                problems.append(f"line {i + 1}: block {name!r} belongs to the {want} region, found in the {here} region")
        elif i in (a, s):
            current = None                      # an anchor closes the region above it; nothing below it continues a block
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        elif line.startswith("        "):
            if current is None:
                problems.append(f"line {i + 1}: indented line that belongs to no feature block of its region")
            else:
                owner[i] = current
        else:
            problems.append(f"line {i + 1}: only feature blocks, blank lines and comments may sit here: {line.strip()!r}")
    for name in sorted({n for n in blocks if blocks.count(n) > 1}):
        problems.append(f"block {name!r} occurs {blocks.count(name)} times")
    originals = [n for n in blocks if n in ORIGINAL_BLOCKS]
    if originals != list(ORIGINAL_BLOCKS):
        problems.append(f"original blocks {originals} != {list(ORIGINAL_BLOCKS)}")
    for i, line in enumerate(lines):          # lazy imports: every feature import sits in its own flag's block
        if not _FEATURE_IMPORT.match(line):
            continue
        m = _FEATURE_IMPORT_NAME.match(line)
        namespace = next((g for g in m.groups() if g), None) if m else None
        if i not in owner:
            problems.append(f"line {i + 1}: feature import outside a feature block: {line.strip()!r}")
        elif namespace != ep.FLAG_PARENTS.get(owner[i], owner[i]):
            problems.append(f"line {i + 1}: import of {namespace!r} inside the {owner[i]!r} block")
    return problems


def test_the_two_stream_anchors_occur_once_each_inside_create_app_first_anchor_first_before_the_root_route():
    lines = APP_PY.read_text(encoding="utf-8").splitlines()
    assert lines.count(ANCHOR_FIRST) == 1 and lines.count(ANCHOR_SECOND) == 1
    start = next(i for i, line in enumerate(lines) if line.startswith("def create_app("))
    root_route = next(i for i in range(start, len(lines)) if lines[i].strip().startswith('@app.get("/"'))
    last_original = max(i for i, line in enumerate(lines)
                        if (m := _BLOCK.match(line)) and m.group(1) in ORIGINAL_BLOCKS)
    assert start < last_original < lines.index(ANCHOR_FIRST) < lines.index(ANCHOR_SECOND) < root_route


def test_every_feature_block_of_create_app_sits_in_its_region_once_with_its_imports_inside_it():
    assert _region_problems(APP_PY.read_text(encoding="utf-8")) == []


# Synthetic create_app() sources: the checker above must accept the layout the two groups use and name each break.
def _src(original_tail="", stream1="", stream2="", extra_top=""):
    blocks = "".join(f'    if config.feature_enabled("{n}"):\n'
                     f'        from copilot.features.{n}.routes import router as {n}_router\n'
                     f'        app.include_router({n}_router)\n' for n in ORIGINAL_BLOCKS)
    return (f"{extra_top}def create_app() -> FastAPI:\n    app = FastAPI()\n{blocks}{original_tail}"
            f"{ANCHOR_FIRST}\n{stream1}\n{ANCHOR_SECOND}\n{stream2}\n"
            '    @app.get("/", include_in_schema=False)\n    def root():\n        return None\n    return app\n')


def _blk(name, body=None):
    body = body if body is not None else f"        from copilot.features.{name}.routes import register\n        register(app)\n"
    return f'    if config.feature_enabled("{name}"):\n{body}'


@pytest.mark.parametrize("source, expected", [
    (_src(), None),
    (_src(stream1=_blk("claims") + _blk("active_approval"), stream2=_blk("events") + "    # a comment\n\n"), None),
    (_src(original_tail=_blk("claims")), "belongs to the stream1 region"),
    (_src(stream1=_blk("conformance")), "belongs to the stream2 region"),
    (_src(stream2=_blk("triage")), "belongs to the stream1 region"),
    (_src(stream1=_blk("mystery")), "unknown feature block 'mystery'"),
    (_src(stream1=_blk("claims") + _blk("claims")), "block 'claims' occurs 2 times"),
    (_src(stream1="    from copilot.features.claims.routes import router\n"), "only feature blocks"),
    (_src(stream1="    app.include_router(x)\n"), "only feature blocks"),
    (_src(stream1="        app.include_router(x)\n"), "belongs to no feature block"),
    (_src(stream1=_blk("claims", "        from copilot.features.ledger.store import x\n")), "import of 'ledger' inside"),
    (_src(stream2=_blk("inbox_watch", "        from copilot.features.explainable_queue.watch import router\n")), None),
    (_src(stream2=_blk("inbox_watch", "        from copilot.features.ledger.store import x\n")), "import of 'ledger' inside the 'inbox_watch' block"),
    (_src(extra_top="from copilot.features.claims import routes\n"), "feature import outside a feature block"),
    (_src().replace(ANCHOR_SECOND + "\n", "", 1).replace(ANCHOR_FIRST + "\n", ANCHOR_SECOND + "\n" + ANCHOR_FIRST + "\n"),
     "anchors out of order"),
    (_src() + ANCHOR_FIRST + "\n", "anchors: first 2 time(s)"),
], ids=["empty-anchors", "stream-blocks", "stream1-above-first-anchor", "stream2-in-stream1", "stream1-in-stream2",
        "unknown-name", "twice", "bare-import", "bare-statement", "orphan-body", "foreign-import", "subflag-own-parent",
        "subflag-foreign-import", "module-import",
        "anchors-swapped", "anchor-twice"])
def test_the_region_checker_accepts_the_contract_layout_and_names_each_break(source, expected):
    problems = _region_problems(source)
    if expected is None:
        assert problems == []
    else:
        assert any(expected in p for p in problems), problems


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]

from _hosted import set_test_csrf

@pytest.mark.parametrize("source,expected", [
    (_src(stream1=_blk("package_print")), None),
    (_src(stream2=_blk("package_print")), "belongs to the stream1 region"),
    (_src(extra_top="from copilot.features.package_print import routes\n"), "feature import outside a feature block"),
])
def test_package_print_region_canaries(source,expected):
    problems=_region_problems(source)
    if expected is None:
        assert problems==[]
    else:
        assert any(expected in p for p in problems),problems

@pytest.mark.parametrize("source,expected", [
    (_src(stream2=_blk("carrier_required")), None),
    (_src(stream1=_blk("carrier_required")), "belongs to the stream2 region"),
    (_src(extra_top="from copilot.features.carrier_required import service\n"), "feature import outside a feature block"),
])
def test_carrier_required_region_canaries(source,expected):
    problems=_region_problems(source)
    if expected is None:
        assert problems==[]
    else:
        assert any(expected in p for p in problems),problems

@pytest.mark.parametrize("source,expected", [
    (_src(stream1=_blk("delivery_facts")), None),
    (_src(stream2=_blk("delivery_facts")), "belongs to the stream1 region"),
    (_src(extra_top="from copilot.features.delivery_facts import service\n"), "feature import outside a feature block"),
])
def test_delivery_facts_region_canaries(source,expected):
    problems=_region_problems(source)
    if expected is None:
        assert problems==[]
    else:
        assert any(expected in item for item in problems),problems


@pytest.mark.parametrize("rule_damage", [None, "legacy", "neighbor-namespace", "default-deny", "unknown-rule", "partial-namespace"])
def test_exact_namespace_manifest_rules_do_not_accept_neighbor_or_default_deny(tmp_path, rule_damage):
    excluded = [{"path": PurePosixPath("copilot", "features", flag.lower(), "routes.py").as_posix(),
                 "rule": "exclude:copilot/features/" + flag.lower() + "/**"} for flag in STREAM_FLAGS]
    if rule_damage == "legacy":
        for entry in excluded:
            entry["rule"] = "exclude:copilot/features/**"
    elif rule_damage == "neighbor-namespace":
        excluded[0]["rule"] = "exclude:copilot/features/not_the_requested_namespace/**"
    elif rule_damage == "default-deny":
        excluded[0]["rule"] = "default-deny"
    elif rule_damage == "unknown-rule":
        excluded[0]["rule"] = "exclude:unknown/**"
    inventory = {"schema": "proofie-export/1", "files": [], "excluded": excluded}
    if rule_damage == "partial-namespace":
        entry = excluded.pop(0)
        inventory["files"] = [entry]
        excluded.append({"path": "copilot/features/claims/synthetic_partial_member.py", "rule": "exclude:copilot/features/claims/**"})
        file = tmp_path / entry["path"]
        file.parent.mkdir(parents=True)
        file.write_text("# synthetic route fixture\n", encoding="utf-8")
    (tmp_path / "MANIFEST.json").write_text(json.dumps(inventory), encoding="utf-8")
    if rule_damage in (None, "legacy"):
        assert _feature_availability(tmp_path) == set()
    else:
        with pytest.raises(AssertionError):
            _feature_availability(tmp_path)


@pytest.mark.parametrize("template_damage", [None, "legacy", "undeclared", "wrong-rule", "neighbor", "included-missing", "present-namespace", "missing-core"])
def test_template_closure_requires_exact_complete_exclusion(tmp_path, monkeypatch, template_damage):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    templates = tmp_path / "copilot/web/templates"
    templates.mkdir(parents=True)
    monkeypatch.setattr(sys.modules[__name__], "TEMPLATES", templates)
    (templates / "base.html").write_text('{% if feature_enabled("deadlines") %}{% include "features/deadlines/synthetic_fixture_include.html" %}{% endif %}', encoding="utf-8")
    excluded = [{"path": "copilot/features/" + flag.lower() + "/routes.py",
                 "rule": "exclude:copilot/features/" + flag.lower() + "/**"} for flag in STREAM_FLAGS]
    template = {"path": "copilot/web/templates/features/deadlines/synthetic_fixture_include.html",
                "rule": "exclude:copilot/web/templates/features/deadlines/**"}
    excluded.append(template)
    inventory = {"schema": "proofie-export/1", "files": [{"path": "copilot/web/templates/base.html"}], "excluded": excluded}
    if template_damage == "legacy":
        template["rule"] = "exclude:copilot/web/templates/features/**"
    elif template_damage == "undeclared":
        excluded.remove(template)
    elif template_damage == "wrong-rule":
        template["rule"] = "default-deny"
    elif template_damage == "neighbor":
        template["rule"] = "exclude:copilot/web/templates/features/events/**"
    elif template_damage == "included-missing":
        excluded.remove(template)
        inventory["files"].append(template)
    elif template_damage == "present-namespace":
        (templates / "features/deadlines").mkdir(parents=True)
    elif template_damage == "missing-core":
        (templates / "base.html").write_text('{% include "missing_core.html" %}', encoding="utf-8")
    (tmp_path / "MANIFEST.json").write_text(json.dumps(inventory), encoding="utf-8")
    if template_damage in (None, "legacy"):
        assert _template_closure("base.html") == {"base.html"}
        assert "DEADLINES" in _flags_named_by_page_templates("base.html")
    else:
        with pytest.raises(AssertionError):
            _template_closure("base.html")


@pytest.mark.parametrize("planted", [False, True])
def test_mixed_included_excluded_import_inventory_catches_planted_import(tmp_path, planted):
    # Claims is legitimately included; every other stream namespace remains excluded.
    entries = [{"path": "copilot/features/" + flag.lower() + "/routes.py",
                "rule": "exclude:copilot/features/" + flag.lower() + "/**"} for flag in STREAM_FLAGS]
    claim = next(entry for entry in entries if PurePosixPath(entry["path"]).parts[2] == "claims")
    entries.remove(claim)
    source = tmp_path / claim["path"]
    source.parent.mkdir(parents=True)
    source.write_text("# synthetic included route\n", encoding="utf-8")
    (tmp_path / "MANIFEST.json").write_text(json.dumps({"schema": "proofie-export/1", "files": [claim], "excluded": entries}), encoding="utf-8")
    assert _feature_availability(tmp_path) == {"CLAIMS"}
    included_modules = ["copilot.features.claims", "copilot.features.claims.routes"]
    probe = {"off": included_modules, "on": included_modules.copy(), "failure": "copilot.features.deadlines",
             "off_routes": ["/"], "restored_routes": ["/"]}
    if planted:
        probe["on"].append("copilot.features.deadlines.routes")
        with pytest.raises(AssertionError, match="Excluded feature imported"):
            _assert_import_inventory("DEADLINES", False, probe)
    else:
        _assert_import_inventory("DEADLINES", False, probe)


@pytest.mark.parametrize("complete", [False, True])
def test_manifest_delivery_facts_requires_literal_transitive_closure(tmp_path, complete):
    selected = {"delivery_facts", "draft", "resolution", "account_cliff"} if complete else {"delivery_facts"}
    files, excluded = [], []
    for name in _PORTABLE_REQUIRES:
        entry = {"path": "copilot/features/" + name + "/routes.py",
                 "rule": "exclude:copilot/features/" + name + "/**"}
        if name in selected:
            files.append(entry)
            path = tmp_path / entry["path"]
            path.parent.mkdir(parents=True)
            path.write_text("# synthetic included route\n", encoding="utf-8")
        else:
            excluded.append(entry)
    (tmp_path / "MANIFEST.json").write_text(json.dumps({"schema": "proofie-export/1", "files": files, "excluded": excluded}), encoding="utf-8")
    if complete:
        assert _feature_availability(tmp_path) == {"DELIVERY_FACTS"}
    else:
        with pytest.raises(AssertionError, match="Incomplete namespace dependency closure"):
            _feature_availability(tmp_path)


def test_dr1b_byte_comparison_freezes_persisted_timeline_clock(tmp_path, monkeypatch):
    from itertools import count
    ticks = count()
    # Make crossing a minute deterministic, instead of rerunning until the wall clock happens to agree.
    monkeypatch.setattr(repo, "_now", lambda: f"2026-10-07T12:{next(ticks):02d}:00+0000")
    _fixed(monkeypatch)
    first = _render_three(tmp_path, "before-minute")
    second = _render_three(tmp_path, "after-minute")
    changed = [name for name in first if first[name] != second[name]]
    assert changed == [], changed


def test_pages_that_extend_the_base_never_open_a_second_main_landmark():
    """The base template owns <main id="main">, the skip-link target; a page body inside it must not add another."""
    offenders = [path.relative_to(TEMPLATES).as_posix() for path in sorted(TEMPLATES.rglob("*.html"))
                 if path.name != "_base.html" and 'extends "_base.html"' in path.read_text(encoding="utf-8")
                 and re.search(r"<main\b", path.read_text(encoding="utf-8"))]
    assert offenders == [], offenders


def test_every_hand_written_button_in_a_template_names_its_kind():
    """The button macro in _components.html draws the controls; a hand-written <button> must still carry
    btn-primary or btn-secondary so it follows the shell's control contract."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        if path.name == "_components.html":
            continue
        for tag in re.findall(r"<button\b[^>]*>", path.read_text(encoding="utf-8")):
            if not re.search(r'class="[^"]*\bbtn-(?:primary|secondary|link)\b', tag):
                offenders.append((path.relative_to(TEMPLATES).as_posix(), tag))
    assert offenders == [], offenders


def test_error_paragraphs_marked_as_alerts_are_red_with_a_mark():
    """Pages draw some errors as <p role="alert">; the stylesheet must make those red with the same cross mark the
    field errors use, so an error is never prose alone."""
    css = (ROOT / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    rules = re.findall(r'p\[role="alert"\](::before)?\s*\{([^}]*)\}', css)
    plain = " ".join(body for before, body in rules if not before)
    marked = " ".join(body for before, body in rules if before)
    assert "var(--error-fg)" in plain and "border-left:4px solid var(--error-edge)" in plain
    assert "\\2715" in marked
