"""On the same-statements collision step, "Check again" must ask PayPal.

On the package page it is a POST to the package /check route (which reconciles the earlier attempt); on the dispute
page it is a GET of the dispute page (which reconciles on load). It is never a plain reload of the package page."""
from copilot.web import next_step
from copilot.web.status import Collision

PKG = "/disputes/PP-D-1/packages/2"
ATTEMPTED = Collision("attempted", 1, 1, ("b.pdf",))


def test_package_page_collision_check_again_posts_to_the_check_route():
    step = next_step._collision_step(ATTEMPTED, package_url=PKG, check_post=f"{PKG}/check")
    (check,) = step.secondary
    assert check.label == "Check again"
    assert check.post == f"{PKG}/check" and check.href is None
    assert check.kind == "secondary"
    assert step.action.label == "Change a statement" and step.action.href == f"{PKG}#statements"


def test_dispute_page_collision_check_again_goes_to_the_dispute_page():
    step = next_step._collision_step(ATTEMPTED, package_url=PKG, check_href="/disputes/PP-D-1")
    (check,) = step.secondary
    assert check.href == "/disputes/PP-D-1" and check.post is None


def test_no_caller_reloads_the_package_page_as_its_check():
    import inspect
    src = inspect.getsource(next_step)
    calls = [line.strip() for line in src.splitlines() if "_collision_step(collision" in line and "def " not in line]
    assert calls, "expected callers of _collision_step"
    for line in calls:
        assert "check_post=" in line or "check_href=" in line, line
