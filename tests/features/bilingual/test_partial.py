"""features/bilingual/_statements.html, the partial lane PK includes under the statements on the package page. Rendered here
with the page's own Jinja environment and the context names the package page provides."""
import re
from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace

from _lang import spanish_outside_lang

from copilot import config
from copilot.features.bilingual import service
from copilot.web.routes_package import templates   # the package page's own environment

from .conftest import DISPUTE_ID, EN_1, ES_1, claim_ids, make_package

PARTIAL = "features/bilingual/_statements.html"


def _render(bilingual, package_id, sent_version=False, form_errors=None, form_values=None, query=None):
    request = SimpleNamespace(query_params=query or {})
    return templates.env.get_template(PARTIAL).render(
        bilingual=bilingual, dispute_id=DISPUTE_ID, package={"id": package_id}, csrf_token="tok",
        sent_version=sent_version, form_errors=form_errors or {}, form_values=form_values or {}, request=request)


class _Lang(HTMLParser):
    """(tag, lang of the nearest ancestor that has one) for every textarea, and the text of every lang=es element."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.textareas, self.es_text = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("input", "br"):
            return
        self.stack.append((tag, a.get("lang")))
        if tag == "textarea":
            self.textareas.append((a.get("id"), a.get("lang")))

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        if data.strip() and any((lang or "").startswith("es") for _, lang in self.stack):
            self.es_text.append(data.strip())


def _parse(html):
    p = _Lang()
    p.feed(html)
    return p


def test_nothing_is_rendered_when_the_feature_is_off():
    assert _render(None, 1).strip() == ""


def test_missing_rows_offer_a_spanish_field_inside_lang_es(mx_on):
    package_id = make_package()
    html = _render(service.view(config.db_path(), package_id), package_id)
    first, second = claim_ids(package_id)
    parsed = _parse(html)
    assert parsed.textareas == [(f"field-es-claim-{first}", "es"), (f"field-es-claim-{second}", "es")]
    assert f'action="/disputes/{DISPUTE_ID}/packages/{package_id}/claims/{first}/spanish"' in html
    assert html.count("No Spanish version yet") == 2
    assert EN_1 in unescape(html)
    assert 'id="statements-es"' in html
    assert "PayPal receives the English statements only" in html
    assert spanish_outside_lang(html) == []


def test_seller_spanish_is_shown_inside_lang_es(mx_on):
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    html = _render(service.view(db, package_id), package_id)
    parsed = _parse(html)
    assert ES_1 in parsed.es_text
    assert "Written by you" in html
    assert html.count("No Spanish version yet") == 1
    assert spanish_outside_lang(html) == []


def test_a_stale_row_says_the_english_changed_and_prefills_the_old_spanish(mx_on):
    from copilot.db import repo
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    repo.mark_claim_edited(db, first, EN_1 + " Signed.")
    html = _render(service.view(db, package_id), package_id)
    assert "The English statement changed after this Spanish version was written" in html
    textarea = re.search(rf'<textarea id="field-es-claim-{first}"[^>]*>(.*?)</textarea>', html, re.S)
    assert textarea and textarea.group(1) == ES_1


def test_a_sent_version_shows_the_spanish_without_a_form(mx_on):
    db = config.db_path()
    package_id = make_package()
    service.save_seller_spanish(db, DISPUTE_ID, package_id, claim_ids(package_id)[0], ES_1)
    html = _render(service.view(db, package_id), package_id, sent_version=True)
    assert "<form" not in html and "<textarea" not in html
    assert ES_1 in _parse(html).es_text
    assert "No Spanish version yet" in html


def test_a_field_error_is_drawn_and_the_typed_text_kept(mx_on):
    package_id = make_package()
    first = claim_ids(package_id)[0]
    key = f"es-claim-{first}"
    html = _render(service.view(config.db_path(), package_id), package_id,
                   form_errors={key: service.EMPTY_MESSAGE}, form_values={key: "  "})
    assert f'id="error-{key}"' in html and service.EMPTY_MESSAGE in html
    assert re.search(rf'<textarea id="field-{key}"[^>]*aria-invalid="true"[^>]*aria-describedby="error-{key}"', html)


def test_the_saved_notice_is_shown_on_its_row_only(mx_on):
    db = config.db_path()
    package_id = make_package()
    first, second = claim_ids(package_id)
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    html = _render(service.view(db, package_id), package_id, query={"es_saved": str(first)})
    assert html.count('data-state="es-saved"') == 1
    assert re.search(rf'id="es-claim-{first}".*?data-state="es-saved"', html, re.S)
    html = _render(service.view(db, package_id), package_id, query={"es_saved": "<script>"})
    assert 'data-state="es-saved"' not in html
