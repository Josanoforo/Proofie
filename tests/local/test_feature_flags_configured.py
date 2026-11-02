"""Every COPILOT_FEATURE_* flag the code reads is configured, and nothing configured is dead.

The code's flags are found by reading copilot/**/*.py (no import): the literal argument of feature_enabled(...), a
generator over a literal tuple of names (`all(feature_enabled(f) for f in ("a", "b"))`), and the module constants
FLAG / _FLAG. A flag read some other way is not seen here. The three files: `.env.example` (local), `render.free.yaml` and
`render.yaml` (hosted; the paid-plan one is private and skipped when absent). Each yaml value must be "true"."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _flags_read_by_the_code() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "copilot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("FLAG", "_FLAG") for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    found.add(node.value.value)
            if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                for gen in node.generators:
                    if isinstance(gen.target, ast.Name) and isinstance(gen.iter, (ast.Tuple, ast.List)):
                        used = any(isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "feature_enabled"
                                   and c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == gen.target.id
                                   for c in ast.walk(node))
                        if used:
                            found.update(e.value for e in gen.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "feature_enabled":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
    return {name.strip().upper().replace("-", "_") for name in found}


def _yaml(path):
    return dict(re.findall(r'^      - key: COPILOT_FEATURE_([A-Z0-9_]+)\r?\n        value: "(\w+)"\r?$',
                           path.read_text(encoding="utf-8"), re.M))


def test_the_code_reads_the_flags_this_test_expects_to_see():
    read = _flags_read_by_the_code()
    assert len(read) == 75 and {"STATUS_LINK", "JUDGE_POOL", "OUTCOME_APPEAL", "ATTENTION_MODEL", "DEMO_DOCUMENTS", "DEMO_DOCUMENTS_SEEDED", "INBOX_WATCH", "MCP_LOCAL_CONTRACTS", "RESOLUTION_STATUS_MESSAGE"} <= read


def test_every_flag_the_code_reads_is_in_env_example_and_in_both_yamls():
    read = _flags_read_by_the_code()
    env = dict(re.findall(r"^COPILOT_FEATURE_([A-Z0-9_]+)=(\w+)\s*$", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
    assert read - set(env) == set(), f"read by the code, missing from .env.example: {sorted(read - set(env))}"
    free = _yaml(ROOT / "render.free.yaml")
    assert read - set(free) == set(), f"read by the code, missing from render.free.yaml: {sorted(read - set(free))}"
    private = ROOT / "render.yaml"
    if private.is_file():
        assert read - set(_yaml(private)) == set(), f"read by the code, missing from render.yaml: {sorted(read - set(_yaml(private)))}"


def test_nothing_configured_is_dead_and_every_hosted_value_is_true():
    read = _flags_read_by_the_code()
    env = dict(re.findall(r"^COPILOT_FEATURE_([A-Z0-9_]+)=(\w+)\s*$", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
    free = _yaml(ROOT / "render.free.yaml")
    assert set(env) - read == set() and set(free) - read == set(), (sorted(set(env) - read), sorted(set(free) - read))
    assert len(env) == len(free) == 75 and set(free.values()) == {"true"}
    assert {k for k, v in env.items() if v != "true"} == {"DEMO_DOCUMENTS", "WEBHOOKS", "WEBHOOKS_VERIFY"}   # hosted-by-design, off locally
