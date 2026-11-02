"""ONE live, read-only smoke test of Proofie's MCP server against the PayPal sandbox.

Run once, by hand:  .venv-ref\\Scripts\\python.exe demo\\mcp_smoke.py     (or the development .venv)
It ends by itself (the stdio server is closed when the client context exits); nothing is started or stopped by hand.

What it does
- asserts COPILOT_MCP_ALLOW_SEND is NOT set (in this process and in the environment it hands the server), so the
  optional send tool is not registered;
- starts `python -m copilot.mcp_server` over stdio with the MCP Python SDK client and asserts that the tool list is
  exactly the five read/draft tools: no approve tool and no send tool exists;
- calls `list_disputes`, then `get_dispute_file` on the dispute named by PROOFIE_SMOKE_DISPUTE_ID (the 40 USD dispute whose evidence was already
  sent from this app); no other tool is called;
- appends summary lines to evidence/mcp_smoke.jsonl through pilot/client.py's `sanitize` (no token, secret, e-mail or
  name), and the server's own PayPal calls land in the same file through SandboxClient (COPILOT_MCP_LOG_NAME), which
  sanitises every line too; afterwards it checks that every PayPal call of this run was a GET (the only other call is
  the standard OAuth client-credentials token request).

Exit code 0 only if every assertion held. A failure is logged and reported as it is; this script is not re-run to get
a green result.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))

from mcp import Client, StdioServerParameters  # noqa: E402

from client import evidence_dir, sanitize  # noqa: E402 - pilot/client.py, the project's sanitising client module

SMOKE_ENV = "PROOFIE_SMOKE_DISPUTE_ID"
TARGET = os.environ.get(SMOKE_ENV, "PP-R-DEMO-0000002")    # the real sandbox id is passed in, never committed
ALLOW_SEND_ENV = "COPILOT_MCP_ALLOW_SEND"
LOG_NAME = "mcp_smoke"
LOG = evidence_dir() / f"{LOG_NAME}.jsonl"
EXPECTED_TOOLS = {"list_disputes", "get_dispute_file", "what_is_missing", "prepare_draft", "get_send_status"}


def log(record: dict) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sanitize(record), ensure_ascii=False) + "\n")


def payload(result) -> dict:
    """The tool's JSON result from an MCP CallToolResult (structured content when present, else the text block)."""
    data = result.model_dump(by_alias=True)
    assert not data.get("isError"), data
    if isinstance(data.get("structuredContent"), dict):
        return data["structuredContent"]
    return json.loads(data["content"][0]["text"])


async def run() -> int:
    failures: list[str] = []

    def check(condition: bool, what: str) -> None:
        if not condition:
            failures.append(what)

    check(ALLOW_SEND_ENV not in os.environ, f"{ALLOW_SEND_ENV} is set in this process")
    child_env = {k: v for k, v in os.environ.items() if k != ALLOW_SEND_ENV}
    child_env["COPILOT_MCP_LOG_NAME"] = LOG_NAME
    check(ALLOW_SEND_ENV not in child_env, f"{ALLOW_SEND_ENV} would reach the server")
    log({"op": "mcp_smoke_start", "target": TARGET, "allow_send_env_present": ALLOW_SEND_ENV in os.environ,
         "python": Path(sys.executable).name})
    if failures:
        log({"op": "mcp_smoke_result", "ok": False, "failures": failures})
        print("FAIL:", failures)
        return 1

    offset = LOG.stat().st_size if LOG.exists() else 0          # everything after this point belongs to this run
    params = StdioServerParameters(command=sys.executable, args=["-m", "copilot.mcp_server"], env=child_env,
                                   cwd=str(ROOT))
    summary: dict = {}
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        check(tools == EXPECTED_TOOLS, f"tool list is {sorted(tools)}")
        check(not [t for t in tools if "approve" in t or "send_approved" in t], "an approve or send tool exists")
        log({"op": "mcp_smoke_tools", "tools": sorted(tools)})

        listing = payload(await client.call_tool("list_disputes", {}))
        rows = listing.get("disputes") or []
        ids = [r.get("dispute_id") for r in rows]
        check("error" not in listing, f"list_disputes returned an error: {listing.get('error')}")
        check(TARGET in ids, f"{TARGET} not in list_disputes ({len(ids)} disputes)")
        target_row = next((r for r in rows if r.get("dispute_id") == TARGET), {})
        summary["list"] = {"count": listing.get("count"), "target_present": TARGET in ids,
                           "status": target_row.get("status"), "amount": target_row.get("amount"),
                           "already_submitted_by_you": len(target_row.get("already_submitted_by_you") or [])}
        log({"op": "mcp_smoke_list_disputes", **summary["list"]})

        file = payload(await client.call_tool("get_dispute_file", {"dispute_id": TARGET}))
        check("error" not in file, f"get_dispute_file returned an error: {file.get('error')}")
        check((file.get("dispute") or {}).get("dispute_id") == TARGET, "get_dispute_file is not about the target")
        draft = file.get("draft") or {}
        summary["file"] = {
            "dispute": {k: (file.get("dispute") or {}).get(k) for k in ("dispute_id", "status", "reason", "amount")},
            "checks": [{"id": c.get("id"), "state": c.get("state"), "action": c.get("action")}
                       for c in file.get("checklist") or []],
            "context_limitations": len(file.get("context_limitations") or []),
            "draft_present": bool(draft), "draft_package_id": draft.get("package_id"),
            "draft_claims": draft.get("claim_count", 0),     # get_dispute_file omits claim text by default
            "draft_approval": draft.get("approval"), "draft_send_status": draft.get("send_status"),
            "review_url": file.get("review_url")}
        check(bool(summary["file"]["checks"]), "get_dispute_file returned no checklist")
        log({"op": "mcp_smoke_get_dispute_file", **summary["file"]})

    # every PayPal call this run's server made must be a read (GET); the one other call is the OAuth token request
    new_lines = LOG.read_bytes()[offset:].decode("utf-8").splitlines()
    calls = [json.loads(line) for line in new_lines if line.strip()]
    paypal = [c for c in calls if c.get("method") or c.get("op") == "oauth_token"]
    writes = [c for c in paypal if c.get("method") and c["method"].upper() != "GET"]
    check(not writes, f"a non-GET PayPal call was made: {[(w.get('method'), w.get('op')) for w in writes]}")
    check(any(c.get("method") == "GET" for c in paypal), "no PayPal GET was recorded for this run")
    summary["paypal_calls"] = [{"op": c.get("op"), "method": c.get("method") or "POST(oauth_token)", "status": c.get("status")}
                               for c in paypal]
    log({"op": "mcp_smoke_result", "ok": not failures, "failures": failures, "paypal_calls": summary["paypal_calls"]})
    print(json.dumps({"ok": not failures, "failures": failures, "list": summary.get("list"),
                      "paypal_calls": summary["paypal_calls"]}, indent=1, ensure_ascii=False))
    return 0 if not failures else 1


def main() -> int:
    try:
        return asyncio.run(asyncio.wait_for(run(), timeout=180))
    except Exception as exc:                                    # noqa: BLE001 - recorded, never retried
        log({"op": "mcp_smoke_result", "ok": False, "failures": [f"{type(exc).__name__}: {str(exc)[:300]}"]})
        print("FAIL:", type(exc).__name__, str(exc)[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
