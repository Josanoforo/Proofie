from pathlib import Path
from copy import deepcopy

from jinja2 import Environment, FileSystemLoader, select_autoescape

from copilot.features.case_reentry.projection import build


CASE = "PP-R-DEMO-0000001"
TEMPLATE = (Path(__file__).resolve().parents[3] / "copilot" / "web" / "templates" /
            "features" / "case_reentry" / "_checkpoint.html")


def _fixture():
    return {
        "case_id": CASE,
        "tasks": [{"key": "prepare", "title": "Prepare", "href": f"/disputes/{CASE}/packages/22#next-step",
                   "status": "in_progress", "label": "Package v2 needs review"}],
        "packages": [
            {"id": 11, "version": 1, "dispute_id": CASE, "created_at": "2026-10-20T10:00:00+00:00"},
            {"id": 22, "version": 2, "dispute_id": CASE, "created_at": "2026-10-20T11:00:00+00:00"},
        ],
        "audit_rows": [{"id": 1, "dispute_id": CASE, "ts": "2026-10-20T12:00:00+00:00", "actor": "vendor",
                         "action": "delivery_incident_response",
                         "detail": {"capture_id": "TXN-SYNTHETIC-1", "package_id": 11,
                                    "source": "seller_recorded", "response": "buyer_contacted",
                                    "private_extra": "must not be displayed"}}],
        "package_states": {11: {"approval_valid": False, "approval_stale": True},
                           22: {"approval_valid": True, "approval_stale": False}},
    }


def test_saved_work_task_links_and_approval_are_version_scoped():
    view = build(**_fixture())
    assert view["available"] is True
    assert view["latest_saved_work"] == {
        "label": "Seller-recorded delivery incident response",
        "ts": "2026-10-20T12:00:00+00:00", "source": "Local case audit", "package_id": 11,
        "href": f"/disputes/{CASE}/packages/11", "attribution": "Recorded by the seller",
    }
    assert view["next_task"]["href"] == f"/disputes/{CASE}/packages/22#next-step"
    assert [row["href"] for row in view["packages"]] == [f"/disputes/{CASE}/packages/11",
                                                           f"/disputes/{CASE}/packages/22"]
    assert "No current approval" in view["packages"][0]["approval_note"]
    assert "recorded as current" in view["packages"][1]["approval_note"]


def test_unknown_approval_and_absent_prior_review_remain_explicit():
    fixture = _fixture()
    fixture["package_states"] = {}
    view = build(**fixture)
    assert all("unavailable" in row["approval_note"] for row in view["packages"])
    assert any("cannot say what changed" in text for text in view["limitations"])
    assert any("last page you visited" in text for text in view["limitations"])


def test_foreign_rows_and_unlisted_package_references_are_ignored():
    fixture = _fixture()
    fixture["audit_rows"].extend([
        {"id": 2, "dispute_id": "PP-R-FOREIGN-0000001", "ts": "2026-10-20T13:00:00+00:00",
         "actor": "vendor", "action": "delivery_incident_response",
         "detail": {"capture_id": "FOREIGN", "package_id": 11, "source": "seller_recorded"}},
        {"id": 3, "dispute_id": CASE, "ts": "2026-10-20T14:00:00+00:00", "actor": "vendor",
         "action": "delivery_incident_response",
         "detail": {"capture_id": "TXN-SYNTHETIC-2", "package_id": 999, "source": "seller_recorded"}},
    ])
    fixture["packages"].append({"id": 33, "version": 3, "dispute_id": "PP-R-FOREIGN-0000001",
                                 "created_at": "2026-10-20T15:00:00+00:00"})
    view = build(**fixture)
    assert view["latest_saved_work"]["package_id"] == 11
    assert all(row["id"] != 33 for row in view["packages"])


def test_non_vendor_or_unattributed_delivery_response_is_not_presented_as_seller_recorded():
    fixture = _fixture()
    fixture["audit_rows"][0]["actor"] = "system"
    view = build(**fixture)
    assert view["latest_saved_work"]["label"] == "Package version v2 saved"


def test_malformed_context_and_task_links_fail_safe():
    assert build(case_id="../other", tasks=[], packages=[], audit_rows=[])["available"] is False
    fixture = _fixture()
    fixture["tasks"] = [{"title": "Unsafe", "status": "todo", "href": "https://outside.invalid/"}]
    view = build(**fixture)
    assert view["next_task"]["href"] is None
    assert view["history_href"] == f"/disputes/{CASE}/history"


def test_projection_does_not_mutate_human_saved_inputs():
    fixture = _fixture()
    before = deepcopy(fixture)
    build(**fixture)
    assert fixture == before


def test_unhashable_status_and_action_are_ignored_safely():
    fixture = _fixture()
    fixture["tasks"] = [{"title": "Malformed task", "status": [], "href": f"/disputes/{CASE}/history"}]
    fixture["audit_rows"] = [{"dispute_id": CASE, "ts": "2026-10-20T15:00:00+00:00",
                              "action": ["delivery_incident_response"], "actor": "vendor",
                              "detail": {"capture_id": "TXN-SYNTHETIC-1", "source": "seller_recorded"}}]
    view = build(**fixture)
    assert view["next_task"] is None
    assert view["latest_saved_work"]["label"] == "Package version v2 saved"


def test_fragment_uses_real_template_and_feature_flag_without_write_controls():
    env = Environment(loader=FileSystemLoader(str(TEMPLATE.parent)), autoescape=select_autoescape(["html"]))
    from copilot.web.routes_disputes import templates      # the app's own `when` filter; a bare environment has none
    env.filters["when"] = templates.env.filters["when"]
    template = env.get_template("_checkpoint.html")
    view = build(**_fixture())
    off = template.render(feature_enabled=lambda _name: False, case_reentry_view=view)
    assert "case-reentry-checkpoint" not in off
    on = template.render(feature_enabled=lambda name: name == "case_reentry", case_reentry_view=view)
    assert 'data-state="case-reentry-checkpoint"' in on
    assert "Most recent saved work" in on and "Recorded by the seller" in on
    assert f'href="/disputes/{CASE}/packages/11"' in on
    assert f'href="/disputes/{CASE}/packages/22#next-step"' in on
    assert "must not be displayed" not in on
    assert "<form" not in on and "<button" not in on and "method=\"post\"" not in on
