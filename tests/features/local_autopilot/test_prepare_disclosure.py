from types import SimpleNamespace
from html.parser import HTMLParser
from jinja2 import Environment, FileSystemLoader


class Structure(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack=[]; self.items=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs); self.items.append((tag, attrs, tuple(self.stack)))
        if tag not in {"input", "br", "img", "meta", "link", "hr"}: self.stack.append((tag,attrs))
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0] == tag:
                del self.stack[i:]; break


def test_prepare_disclosures_preserve_actions_and_hackathon(monkeypatch):
    env=Environment(loader=FileSystemLoader("copilot/web/templates"), autoescape=True)
    env.filters["when"] = lambda x: x
    flags={"local_autopilot", "resolution", "negotiation_drafts"}
    env.globals["feature_enabled"] = lambda flag: flag in flags
    base={"view":"prepare", "norm":{"dispute_id":"PP-R-DEMO-0000001","currency":"USD"},
      "status_view":{"label":"Waiting"}, "due":{"iso":"2099-01-01"}, "csrf_token":"token-value",
      "request":SimpleNamespace(query_params={}), "local_autopilot_actions":["offer","message"],
      "local_autopilot_evidence_count":1, "local_autopilot_currency_supported":True,
      "local_autopilot_default_amount":"15.00", "local_autopilot_error":False, "local_autopilot_amount_error":False,
      "local_autopilot_history":[{"action":"offer","amount_text":"15.00 USD","decision":"simulated_action","policy_version":"v1", "policy_sha256":"p", "state_sha256":"s", "flags":[], "result":"SIMULATED — NOT SENT", "logical_id":"abc"}],
      "assistance_context":{"preparation":"ready for human review","package_id":7,"recommendation":"message", "reason":"Source recorded", "facts":[{"id":"doc_1"}],"dispute_amount_cents":1500,"currency":"USD"}}
    render=lambda context: env.get_template("features/local_autopilot/_assistance.html").render(**context)
    html=render(base); dom=Structure(); dom.feed(html)
    assert 'Review the prepared package' in html and 'Suggested next step: review a message' in html
    assert 'not approval or sending' in html and 'authenticity is not verified' in html
    details=[a for tag,a,_ in dom.items if tag=='details']
    assert len(details)>=2 and 'open' not in details[0] and 'open' not in details[1]
    assert 'href="/disputes/PP-R-DEMO-0000001/packages/7"' in html
    assert 'name="csrf_token" value="token-value"' in html and 'name="return_to" value="prepare"' in html
    assert 'for="autopilot-amount"' in html and 'local_autopilot_id=abc' in html
    reopened=render({**base,"local_autopilot_amount_error":True})
    assert '<details open><summary>Practice an action' in reopened and 'role="alert"' in reopened
    direct=env.get_template("case/_local_autopilot.html").render(**{**base,"view":"hackathon"})
    assert 'name="return_to"' not in direct and '<summary>Practice an action' not in direct
    flags.clear()
    assert 'id="local-autopilot"' not in render(base)


def test_prepare_amount_error_redirect_is_same_case_and_allowlisted(monkeypatch, tmp_path):
    import json
    from pathlib import Path
    from copilot import config
    from copilot.features.local_autopilot import routes
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "case.sqlite")
    monkeypatch.setattr(routes, "verify_csrf", lambda *_: True)
    raw=json.loads(Path("pilot/fixtures/synthetic/dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
    class Fake:
        def call(self, method, path, operation):
            assert method == "GET" and operation == "show_dispute"
            return raw
    url=routes.simulate_case(SimpleNamespace(), "PP-R-DEMO-0000001", csrf_token="token", profile="standard", action="offer", amount="bad", return_to="prepare", client=Fake())
    assert url.status_code == 303 and url.headers["location"] == "/disputes/PP-R-DEMO-0000001/workspace/prepare?local_autopilot_error=amount#local-autopilot"
    other=routes.simulate_case(SimpleNamespace(), "PP-R-DEMO-0000001", csrf_token="token", profile="standard", action="offer", amount="bad", return_to="//elsewhere", client=Fake())
    assert other.headers["location"].startswith("/disputes/PP-R-DEMO-0000001/workspace/hackathon?")
    monkeypatch.setattr(routes.service, "simulate", lambda data, path: {"decision":"simulated_action"})
    monkeypatch.setattr(routes, "_evidence", lambda _did: [])
    ok=routes.simulate_case(SimpleNamespace(), "PP-R-DEMO-0000001", csrf_token="token", profile="standard", action="offer", amount="15.00", return_to="prepare", client=Fake())
    assert ok.status_code == 303 and ok.headers["location"] == "/disputes/PP-R-DEMO-0000001/workspace/prepare?local_autopilot_done=1#local-autopilot"
