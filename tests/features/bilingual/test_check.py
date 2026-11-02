"""check.parity_problems(en, es): code, not the model, decides whether a Spanish version keeps the facts of its English
statement: the same digit runs, the same months, the same tracking-like tokens, nothing added, no link or e-mail."""
import pytest

from copilot.features.bilingual import check

from .conftest import EN_1, EN_2, ES_1, ES_2


@pytest.mark.parametrize("en,es", [
    (EN_1, ES_1),
    (EN_2, ES_2),
    ("Shipped on 3 October 2026 with FedEx.", "Enviado el 3 de octubre de 2026 con FedEx."),
    ("Delivered on Sep 30, 2026.", "Entregado el 30 sept 2026."),
    ("The amount was 1,250.00 MXN.", "El monto fue 1,250.00 MXN."),
    ("Tracking DEMO-FDX-0012345678 shows delivered.", "La guía DEMO FDX 0012345678 muestra entregado."),
])
def test_a_faithful_version_passes(en, es):
    assert check.parity_problems(en, es) == []


@pytest.mark.parametrize("en,es,word", [
    (EN_2, ES_2 + " Llegó el 9 oct 2026.", "number"),
    (EN_1, ES_1.replace("7 oct", "8 oct"), "number"),
    (EN_1, ES_1.replace(" el 7 oct 2026", ""), "number"),
    ("Shipped on 3 October 2026.", "Enviado el 3 de noviembre de 2026.", "month"),
    ("Shipped in October.", "Enviado.", "month"),
    (EN_1, ES_1.replace("1Z999AA10123456784", "1Z999AA10123456785"), "number"),
    (EN_2, ES_2 + " Guía ABCDEFGHJK1.", "tracking"),
    (EN_2, ES_2 + " Vea https://example.test", "link"),
    (EN_2, ES_2 + " Escriba a ventas@example.com", "link"),
])
def test_a_changed_fact_is_named(en, es, word):
    problems = check.parity_problems(en, es)
    assert problems and any(word in p for p in problems), problems


def test_empty_and_too_long_are_problems():
    assert check.parity_problems(EN_2, "  ")
    assert check.parity_problems(EN_2, "a" * (check.MAX_LENGTH + 1))


def test_every_problem_is_a_plain_sentence():
    problems = check.parity_problems(EN_1, "Hola 9 noviembre https://x.test")
    assert problems and all(p[0].isupper() and p.endswith(".") for p in problems)
