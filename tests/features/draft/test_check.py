"""validate_draft: a valid pair and each refusal named by a problem text."""
import pytest

from copilot.features.draft import check
from draft_fakes import EN_OK, ES_OK, sheet
from draft_fakes import _no_httpx_calls  # noqa: F401  (autouse: no httpx call in these tests)


def _problems(en=EN_OK, es=ES_OK, s=None):
    v = check.validate_draft(s or sheet(), en, es)
    return v, " | ".join(v.problems)


def test_a_valid_pair_is_ok_with_no_problems():
    v, _ = _problems()
    assert v.ok is True and v.problems == ()


def test_a_foreign_number_is_refused():
    v, text = _problems(en=EN_OK + " Your reference is 98765.")
    assert not v.ok and "English: the number 98765 is not in the facts" in text


def test_a_foreign_date_is_refused():
    v, text = _problems(es=ES_OK.replace("2026-10-10", "2026-12-24"))
    assert not v.ok and "Spanish: the date 2026-12-24 is not in the facts" in text


def test_a_date_inside_a_literal_or_the_order_date_is_accepted():
    assert _problems(en=EN_OK.replace("2026-10-10", "2026-09-28"))[0].ok


def test_a_second_tracking_like_token_is_refused():
    v, text = _problems(en=EN_OK + " Also ABCDEF123456 is yours.")
    assert not v.ok and "the tracking-like token ABCDEF123456 is not in the facts" in text


def test_the_tracking_number_must_appear_verbatim_in_both_texts():
    v, text = _problems(en=EN_OK.replace("DEMO-FDX-000001", "the parcel"))
    assert not v.ok and "English: the tracking number is missing" in text
    v, text = _problems(es=ES_OK.replace("DEMO-FDX-000001", "DEMO-FDX-00001"))
    assert not v.ok and "Spanish: the tracking number is missing" in text


def test_without_a_tracking_number_any_tracking_like_token_is_refused():
    s = sheet(track=None)
    v, text = _problems(en="Your order shipment 1Z999AA10123456784 is on its way, the buyer can reply here and the seller will look into it right away.",
                        es="Su pedido va en camino, responda aquí y el vendedor lo revisará de inmediato.", s=s)
    assert not v.ok and "tracking-like token 1Z999AA10123456784" in text


@pytest.mark.parametrize("bad", ["See http://example.test now", "visit www.example.test", "write to me@example.com"])
def test_links_and_emails_are_refused(bad):
    v, text = _problems(en=EN_OK + " " + bad)
    assert not v.ok and "no links or emails" in text


@pytest.mark.parametrize("word", ["guarantee", "guaranteed", "GUARANTEED"])
def test_the_english_promise_words_are_refused(word):
    v, text = _problems(en=EN_OK + f" It is {word}.")
    assert not v.ok and "promise word" in text


def test_the_spanish_promise_stem_is_refused():
    v, text = _problems(es=ES_OK + " Se lo garantizo.")
    assert not v.ok and "promise word 'garantiz'" in text


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_text_is_refused(empty):
    v, text = _problems(en=empty)
    assert not v.ok and "English: the text is empty" in text
    v, text = _problems(es=empty)
    assert not v.ok and "Spanish: the text is empty" in text


def test_a_text_over_600_characters_is_refused_and_exactly_600_is_not():
    long_en = EN_OK + " order" * 100
    v, text = _problems(en=long_en)
    assert not v.ok and "over 600 characters" in text
    exact = (EN_OK + " " + "order " * 100)[:600]
    assert len(exact) == 600 and "over 600" not in _problems(en=exact)[1]


def test_the_wrong_language_is_refused_by_the_cheap_word_check():
    v, text = _problems(en="Hola, su pedido llegará pronto DEMO-FDX-000001.")
    assert not v.ok and "English: does not look like English" in text
    v, text = _problems(es="Hello, your order DEMO-FDX-000001 will arrive soon.")
    assert not v.ok and "Spanish: does not look like Spanish" in text


def test_every_refusal_is_a_named_problem_and_several_can_stack():
    v, _ = _problems(en="http://x.test 98765 guaranteed", es="")
    assert not v.ok and len(v.problems) >= 5 and all(p.startswith(("English:", "Spanish:")) for p in v.problems)


def test_the_validator_is_pure_and_never_calls_a_model():
    import pathlib
    source = pathlib.Path(check.__file__).read_text(encoding="utf-8")
    assert "httpx" not in source and "call_model" not in source and "ollama" not in source.lower()


# ---- more validator observations (a), (b), (c) ------------------------------------------------------------------------------
def test_a_two_digit_or_one_digit_foreign_number_is_refused_but_the_sheets_own_runs_are_allowed():
    v, text = _problems(en=EN_OK + " Please wait within 14 days.")
    assert not v.ok and "English: the number 14 is not in the facts" in text
    v, text = _problems(es=ES_OK + " Espere 3 días.")
    assert not v.ok and "Spanish: the number 3 is not in the facts" in text
    s = sheet()
    assert {"2026", "09", "28", "40", "00", "10"} <= set(s.numbers)           # 2026-09-28, 40.00 and 2026-10-10 still yield their runs
    assert _problems()[0].ok                                                  # EN_OK/ES_OK only use the sheet's own numbers


def test_the_sheets_own_short_numbers_are_allowed_in_the_text():
    v, _ = _problems(en=EN_OK.replace("for 40.00 USD", "for 40.00 USD (40 dollars)"))
    assert v.ok


@pytest.mark.parametrize("foreign", ["abcdef123456", "1z 999 aa1 0123 456 784", "1Z999AA10123456784", "zz-12345-yy99",
                                     "demo-fdx-999999"])
def test_a_foreign_tracking_string_is_refused_in_any_case_and_with_spaces_or_hyphens(foreign):
    v, text = _problems(en=EN_OK + f" Also {foreign}.")
    assert not v.ok and "English: the tracking-like token" in text


def test_the_sheets_tracking_number_in_another_case_or_spacing_is_not_a_foreign_token():
    for spaced in ("demo-fdx-000001", "DEMO-FDX-000001", "Demo-Fdx-000001"):
        v, text = _problems(en=EN_OK.replace("DEMO-FDX-000001", "DEMO-FDX-000001") + f" Reference {spaced}.")
        assert "tracking-like token" not in text


def test_words_without_digits_and_ordinary_dates_are_never_tracking_like():
    v, text = _problems(en=EN_OK + " Internationalization of the correspondence is unconstitutionally slow.")
    assert "tracking-like" not in text
    assert "tracking-like" not in _problems()[1]                              # "by 2026-10-10", "40.00 USD" and the like


@pytest.mark.parametrize("month", ["January", "february", "MARCH", "April", "June", "July", "August", "September", "October",
                                   "November", "December", "Jan", "Feb", "sept", "Oct", "Nov", "Dec"])
def test_english_month_names_are_refused_in_both_texts(month):
    for kwargs, label in (({"en": EN_OK + f" It left in {month}."}, "English"), ({"es": ES_OK + f" Salió en {month}."}, "Spanish")):
        v, text = _problems(**kwargs)
        assert not v.ok and f"{label}: contains a spelled-out date (use the facts' ISO date)" in text


@pytest.mark.parametrize("month", ["enero", "Febrero", "MARZO", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                                   "setiembre", "octubre", "noviembre", "diciembre", "ene", "abr", "dic", "sep"])
def test_spanish_month_names_are_refused_in_both_texts(month):
    for kwargs, label in (({"en": EN_OK + f" It left in {month}."}, "English"), ({"es": ES_OK + f" Salió en {month}."}, "Spanish")):
        v, text = _problems(**kwargs)
        assert not v.ok and f"{label}: contains a spelled-out date (use the facts' ISO date)" in text


@pytest.mark.parametrize("ordinary", ["You may reply here.", "It arrived two days ago.", "The sea is called el mar."])
def test_ambiguous_ordinary_words_are_not_dates_unless_a_digit_is_next_to_them(ordinary):
    assert "spelled-out date" not in _problems(en=EN_OK + " " + ordinary)[1]


@pytest.mark.parametrize("dated", ["It left on may 3.", "It left 3 may.", "Salió el 3 mar.", "Salió ago 5."])
def test_the_ambiguous_short_forms_next_to_a_digit_are_refused(dated):
    assert "spelled-out date" in _problems(en=EN_OK + " " + dated)[1]


# synthetic causal regressions; no original model output copied.
def _fixguard_sheet(field, value):
    from dataclasses import replace
    from copilot.features.draft.facts import DIGIT_RUN
    s = sheet()
    literals = tuple(v for v in s.literals if v != getattr(s, field)) + (value,)
    numbers = tuple(dict.fromkeys(n for v in literals for n in DIGIT_RUN.findall(v)))
    return replace(s, **{field: value, "literals": literals, "numbers": numbers})


@pytest.mark.parametrize("word", ["ReFuNd", "REEMBOLSAR", "money back", "DINERO"])
@pytest.mark.parametrize("language", ["en", "es"])
def test_fixguard_refund_substrings_are_refused_in_either_text(word, language):
    texts = {"en": EN_OK, "es": ES_OK}
    texts[language] += " " + word
    v, text = _problems(**texts)
    assert not v.ok
    assert "contains refund wording: the seller decides refunds from the page" in text


@pytest.mark.parametrize("language", ["en", "es"])
def test_fixguard_address_six_words_are_refused_even_below_forty_characters(language):
    value = "north south east west demo parcel"
    assert len(value) < 40 and len(value.split()) == 6
    texts = {"en": EN_OK, "es": ES_OK}
    texts[language] += " NORTH, south east west demo parcel."
    v, text = _problems(s=_fixguard_sheet("shipping_address_line", value), **texts)
    assert not v.ok and "repeats free text from a fact value" in text


@pytest.mark.parametrize("language", ["en", "es"])
def test_fixguard_long_free_literal_six_word_echo_is_refused(language):
    value = "DEMO DATA alpha bravo charlie delta echo foxtrot"
    assert len(value) > 40
    texts = {"en": EN_OK, "es": ES_OK}
    texts[language] += " alpha bravo charlie delta echo foxtrot"
    v, text = _problems(s=_fixguard_sheet("tracking_status", value), **texts)
    assert not v.ok and "repeats free text from a fact value" in text


def test_fixguard_five_words_and_nonconsecutive_words_are_not_an_echo():
    s = _fixguard_sheet("shipping_address_line", "north south east west demo parcel")
    for fragment in ("north south east west demo", "north south east new west demo parcel"):
        v, text = _problems(s=s, en=EN_OK + " " + fragment)
        assert v.ok and "repeats free text" not in text


@pytest.mark.parametrize("length,blocked", [(40, False), (41, True)])
def test_fixguard_other_free_literal_threshold_is_strictly_over_forty(length, blocked):
    value = "alpha bravo charlie delta echo foxtrot" + "x" * (length - len("alpha bravo charlie delta echo foxtrot"))
    assert len(value) == length and len(value.split()) == 6
    v, text = _problems(s=_fixguard_sheet("carrier", value), en=EN_OK + " " + value)
    assert ("repeats free text from a fact value" in text) is blocked
    assert v.ok is (not blocked)


def test_fixguard_topic_words_delivery_and_entrega_are_allowed():
    s = sheet(track=None)
    v = check.validate_draft(s, "Delivery update for your delivery.", "Informacion de entrega para su entrega.")
    assert v.ok and v.problems == ()
    assert "refund" not in check.EN_WORDS and "reembols" not in check.ES_WORDS


def test_fixguard_typed_ids_and_existing_numeric_date_tracking_rules_stay_separate():
    value = "DEMO DATA alpha bravo charlie delta echo foxtrot"
    v, text = _problems(s=_fixguard_sheet("dispute_id", value), en=EN_OK + " " + value)
    assert v.ok and "repeats free text" not in text
    assert "the number 98765 is not in the facts" in _problems(en=EN_OK + " 98765")[1]
    assert "tracking-like token" in _problems(en=EN_OK + " ZZ999999999999")[1]
    assert "spelled-out date" in _problems(en=EN_OK + " October")[1]
