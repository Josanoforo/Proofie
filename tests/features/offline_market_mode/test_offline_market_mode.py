from copilot.features.offline_market_mode.service import browser_policy, OFFLINE_MESSAGE, may_submit
from pathlib import Path


def test_offline_policy_blocks_writes_without_automatic_retry():
    assert browser_policy() == {"blocks_approval_offline": True, "blocks_send_offline": True,
                                "automatic_retry": False, "automatic_send_on_reconnect": False}
    assert "never sends or retries" in OFFLINE_MESSAGE.lower()
    assert may_submit("approve", False) is False
    assert may_submit("send", False) is False
    assert may_submit("send", True) is True
    assert browser_policy()["blocks_approval_offline"] is True


def test_package_review_offline_guard_is_feature_gated_and_not_a_send_path_edit():
    root = Path(__file__).resolve().parents[3]
    template = (root / "copilot/web/templates/features/offline_market_mode/_entry.html").read_text(encoding="utf-8")
    assert 'form.approve-form, form[data-sends]' in template
    assert 'window.addEventListener("online", update)' in template
    assert "preventDefault" in template
    assert "fetch(" not in template
    assert "window.localStorage.setItem(storageKey" in template
    assert "crypto.subtle.digest(\"SHA-256\"" in template
    assert "prepared-local-only" in template
    assert "fresh PayPal read" in template and "new human action" in template
    assert "form.submit" not in template and "location.reload" not in template


def test_local_preparation_is_content_bound_and_revalidated_without_reconnect_action():
    root = Path(__file__).resolve().parents[3]
    template = (root / "copilot/web/templates/features/offline_market_mode/_entry.html").read_text(encoding="utf-8")
    assert 'document.querySelectorAll(".claims .claim-card")' in template
    assert "documents: Array.from(card.querySelectorAll(\".claim-source-detail\"))" in template
    assert 'document.querySelector(\'ul[data-state="will-send"]\')' in template
    assert 'document.querySelectorAll("textarea, input:not([type=hidden]):not([type=file]):not([type=submit]):not([type=button]), select")' in template
    assert 'input.files || []' in template and "file.lastModified" in template
    assert "current === saved.fingerprint" in template
    assert 'window.addEventListener("online", update)' in template
    update = template.split('const update = () => {', 1)[1].split('};', 1)[0]
    assert "digest(content())" in update
    assert "fetch(" not in update and "submit(" not in update
    assert 'data-dispute-id="{{ dispute_id }}"' in template
    assert 'data-package-version="{{ package.version if package else' in template


def test_editable_draft_and_photo_bytes_persist_locally_without_upload_or_approval():
    root = Path(__file__).resolve().parents[3]
    template = (root / "copilot/web/templates/features/offline_market_mode/_entry.html").read_text(encoding="utf-8")
    assert "proofie:offline-editable-draft:${panel.dataset.disputeId}:${panel.dataset.packageId}" in template
    assert 'state: "editable-draft-local-only"' in template
    assert "const draftValues = () => editableControls().map" in template
    assert "control.closest(\".approve-form\")" in template
    assert 'control.closest(\'form[data-sends]\')' in template
    assert "restoreDraft();" in template
    assert 'document.addEventListener("DOMContentLoaded", initialize, { once: true })' in template
    assert 'window.localStorage.setItem(draftKey' in template
    assert "captured photos are saved only in this browser" in template.lower()
    assert 'data-offline-photo="label"' in template and 'data-offline-photo="parcel"' in template
    assert 'capture="environment"' in template
    assert '/disputes/{{ dispute_id }}/workspace/prepare#documents' in template
    assert "Nothing here adds a file to the package" in template
    assert "indexedDB.open(photoDbName, 1)" in template
    assert "objectStore(photoStore).put" in template and "blob: file" in template
    assert "new File([row.blob]" in template and "new DataTransfer()" in template
    assert "restorePhotos().then(() => update())" in template
    assert "data-photo-downloads" in template and "Download ${kind} photo for manual upload" in template
    assert "URL.createObjectURL(file)" in template and "link.download = file.name" in template
    assert "choose it in the dispute's document upload" in template
    assert "not attached to the package, ledger, or provider" in template.lower()
    assert "nothing was uploaded" in template.lower()
    assert "5 MB each" in template and 'input[type="file"] { max-width: 100%' in template
    assert "fresh case read" in template and "new human approval choice" in template
    assert "fetch(" not in template and "form.submit" not in template and "location.reload" not in template
    update = template.split('const update = () => {', 1)[1].split('};', 1)[0]
    assert "indexedDB" not in update and "fetch(" not in update and ".submit(" not in update


def test_declared_limits_and_presentation_links_are_independent_flags():
    from jinja2 import Environment
    root = Path(__file__).resolve().parents[3]
    source = (root / "copilot/web/templates/features/offline_market_mode/_entry.html").read_text(encoding="utf-8")
    render = Environment().from_string(source).render(
        feature_enabled=lambda name: False, dispute_id="CASE", package=None)
    assert render.strip() == ""
    render = Environment().from_string(source).render(
        feature_enabled=lambda name: name == "declared_limits", dispute_id="CASE", package=None)
    assert "/declared-limits" in render
    assert "/presentation-mode" not in render
    render = Environment().from_string(source).render(
        feature_enabled=lambda name: name == "presentation_mode", dispute_id="CASE", package=None)
    assert "/presentation-mode?case_id=CASE" in render
    assert "/declared-limits" not in render
    package_review = (root / "copilot/web/templates/package_review.html").read_text(encoding="utf-8")
    assert 'feature_enabled("offline_market_mode") or feature_enabled("declared_limits") or feature_enabled("presentation_mode")' in package_review
    # Each link is guarded independently in the included partial and is outside the offline-only section/script.
    first_gate_end = source.index("{% endif %}")
    assert source.index('feature_enabled("declared_limits")') > first_gate_end
    assert source.index('feature_enabled("presentation_mode")') > first_gate_end
