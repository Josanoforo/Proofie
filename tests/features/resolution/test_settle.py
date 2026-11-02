"""wait_until_settled with an injected sleep (no test ever waits)."""
import httpx
import pytest

from copilot.features.resolution import settle
from resolution_fakes import DISPUTE_ID, FakePayPal, paypal_error


def test_settles_on_the_third_read_with_two_sleeps_of_ten_seconds():
    fake, sleeps = FakePayPal(settling_reads=2), []
    fake.pending, fake.left = "after_offer", 2            # as if an offer had just been POSTed
    result = settle.wait_until_settled(fake, DISPUTE_ID, sleep=sleeps.append)
    assert (result.settled, result.reads, result.limitations) == (True, 3, ())
    assert sleeps == [10, 10]
    assert result.norm["status"] == "WAITING_FOR_BUYER_RESPONSE"
    assert {c[2] for c in fake.calls} == {"resolution_poll"}


def test_a_dispute_that_is_not_settling_returns_after_one_read_and_no_sleep():
    sleeps = []
    result = settle.wait_until_settled(FakePayPal(), DISPUTE_ID, sleep=sleeps.append)
    assert (result.settled, result.reads, sleeps) == (True, 1, [])


def test_eight_settling_reads_return_not_settled_with_the_limitation():
    fake, sleeps = FakePayPal(settling_reads=99), []
    fake.pending, fake.left = "after_offer", 99
    result = settle.wait_until_settled(fake, DISPUTE_ID, sleep=sleeps.append)
    assert (result.settled, result.reads) == (False, 8)
    assert sleeps == [10] * 7
    assert [lim.message for lim in result.limitations] == [
        "PayPal is still updating this dispute after 80 seconds; refresh later"]


@pytest.mark.parametrize("error", [paypal_error(500, None), paypal_error(403, None), httpx.ReadTimeout("slow")])
def test_a_paypal_failure_is_a_limitation_not_an_exception(error):
    result = settle.wait_until_settled(FakePayPal(get_raises=error), DISPUTE_ID, sleep=lambda s: None)
    assert result.settled is False and result.norm is None and len(result.limitations) == 1
    assert "dbg-1" not in result.limitations[0].message


def test_a_programming_error_still_raises():
    with pytest.raises(KeyError):
        settle.wait_until_settled(FakePayPal(get_raises=KeyError("bug")), DISPUTE_ID, sleep=lambda s: None)


def test_the_poll_count_and_delay_are_parameters():
    fake, sleeps = FakePayPal(settling_reads=99), []
    fake.pending, fake.left = "after_offer", 99
    result = settle.wait_until_settled(fake, DISPUTE_ID, polls=3, delay_seconds=5, sleep=sleeps.append)
    assert (result.reads, sleeps) == (3, [5, 5])
    assert "after 15 seconds" in result.limitations[0].message
