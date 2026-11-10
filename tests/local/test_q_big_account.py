"""Q (v1.1.2): the inbox and the other list readers never fail on a big account."""
import pytest

from copilot.core import disputes
from copilot.core.disputes import DisputeList, PayPalError, fetch_all_disputes, list_cut_line


class Pages:
    """A fake PayPal dispute list: `total` disputes served `size` per page through PayPal's own next link."""
    def __init__(self, total, fail_after=None):
        self.total, self.fail_after, self.calls = total, fail_after, []

    def call(self, method, path, op, **kw):
        self.calls.append(path)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise PayPalError(503, "SERVICE_UNAVAILABLE", None, {})
        size = int(path.split("page_size=")[1].split("&")[0])
        start = int(path.split("start=")[1]) if "start=" in path else 0
        items = [{"dispute_id": f"PP-R-AAA-{n:08d}"} for n in range(start, min(start + size, self.total))]
        links = ([{"rel": "next", "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes?page_size={size}&start={start + size}"}]
                 if start + size < self.total else [])
        return {"items": items, "links": links}


def test_the_list_asks_for_fifty_per_page_and_reads_it_all(monkeypatch):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    fake = Pages(321)
    items, pages = fetch_all_disputes(fake)
    assert len(items) == 321 and pages == 7 and fake.calls[0] == "/v1/customer/disputes?page_size=50"
    assert list_cut_line(items) is None


def test_past_the_page_cap_the_list_is_cut_with_one_line_never_an_error(monkeypatch):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    items, pages = fetch_all_disputes(Pages(1200))
    assert len(items) == 1000 and pages == 20 and items.cut
    assert list_cut_line(items) == disputes.LIST_CUT_LINE.format(n=1000)


def test_a_later_page_that_fails_keeps_what_was_read_and_the_first_page_still_raises(monkeypatch):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    items, pages = fetch_all_disputes(Pages(321, fail_after=2))
    assert len(items) == 100 and pages == 2 and items.cut
    with pytest.raises(PayPalError):
        fetch_all_disputes(Pages(321, fail_after=0))


@pytest.mark.pool_scope
def test_the_hosted_demo_reads_its_cases_by_id_and_never_lists_the_account(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_FEATURE_JUDGE_POOL", "true")
    monkeypatch.setenv("COPILOT_JUDGE_POOL", "PP-R-AAA-10000001,PP-R-AAA-10000002")

    class ById:
        calls = []

        def call(self, method, path, op, **kw):
            self.calls.append((method, path, op))
            did = path.rsplit("/", 1)[1]
            if did == "PP-R-AAA-10000002":
                raise PayPalError(404, "RESOURCE_NOT_FOUND", None, {})
            return {"dispute_id": did}

    fake = ById()
    items, _pages = fetch_all_disputes(fake)
    assert [i["dispute_id"] for i in items] == ["PP-R-AAA-10000001", "PP-R-HJE-10194967"]
    assert all(op == "show_dispute" and "?" not in path for _m, path, op in fake.calls) and len(fake.calls) == 3
