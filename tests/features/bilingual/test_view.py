"""bilingual.view(db_path, package_id): the interface lane PK's package page calls. Off by default, MX only; each English
statement with its Spanish version and where that came from (seller, ai, or missing)."""
import sqlite3

import pytest

from copilot import config
from copilot.db import repo
from copilot.features.bilingual import service, store

from .conftest import DISPUTE_ID, EN_1, EN_2, ES_1, ES_2, FLAG, claim_ids, make_package


def _tables(db):
    with sqlite3.connect(str(db)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_flag_off_returns_none_and_writes_nothing(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setattr(config, "RULES_COUNTRY", "MX")
    package_id = make_package()
    db = config.db_path()
    before = (db.stat().st_size, db.stat().st_mtime_ns)
    assert service.view(db, package_id) is None
    assert (db.stat().st_size, db.stat().st_mtime_ns) == before
    assert store.TABLE not in _tables(db)


@pytest.mark.parametrize("value", ["false", "", "1", "yes"])
def test_flag_needs_the_literal_true(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    monkeypatch.setattr(config, "RULES_COUNTRY", "MX")
    assert service.view(config.db_path(), make_package()) is None


@pytest.mark.parametrize("country", ["US", "", "us_xx"])
def test_us_or_other_rule_set_returns_none(monkeypatch, country):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", country)
    package_id = make_package()
    assert service.enabled() is False
    assert service.view(config.db_path(), package_id) is None
    assert store.TABLE not in _tables(config.db_path())


def test_country_is_read_case_insensitively(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", " mx ")
    assert service.enabled() is True


def test_mx_without_spanish_every_row_is_missing(mx_on):
    package_id = make_package()
    view = service.view(config.db_path(), package_id)
    ids = claim_ids(package_id)
    assert view["complete"] is False
    assert [(r["claim_id"], r["position"], r["en"], r["es"], r["es_state"], r["stale"]) for r in view["rows"]] == [
        (ids[0], 1, EN_1, "", "missing", False), (ids[1], 2, EN_2, "", "missing", False)]


def test_the_package_level_view_is_the_hook_interface(mx_on, monkeypatch):
    from copilot.features import bilingual
    db = config.db_path()
    package_id = make_package()
    assert bilingual.view(db, package_id) == service.view(db, package_id)
    monkeypatch.delenv(FLAG)
    assert bilingual.view(db, package_id) is None


def test_package_without_statements_is_not_complete(mx_on):
    view = service.view(config.db_path(), make_package(texts=()))
    assert view == {"rows": [], "complete": False}


def test_unknown_package_returns_none(mx_on):
    assert service.view(config.db_path(), 999) is None


def test_seller_spanish_is_shown_and_completes_the_view(mx_on):
    db = config.db_path()
    package_id = make_package()
    first, second = claim_ids(package_id)
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    view = service.view(db, package_id)
    assert [(r["es"], r["es_state"]) for r in view["rows"]] == [(ES_1, "seller"), ("", "missing")]
    assert view["complete"] is False
    service.save_seller_spanish(db, DISPUTE_ID, package_id, second, ES_2 + "\r\nSegunda línea.")
    view = service.view(db, package_id)
    assert [(r["es"], r["es_state"]) for r in view["rows"]] == [(ES_1, "seller"), (ES_2 + "\nSegunda línea.", "seller")]
    assert view["complete"] is True


def test_the_latest_spanish_wins_and_earlier_rows_are_kept(mx_on):
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, "Primera versión.")
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    assert service.view(db, package_id)["rows"][0]["es"] == ES_1
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {store.TABLE} WHERE claim_id = ?", (first,)).fetchone()[0] == 2


def test_saving_is_audited_without_the_text(mx_on):
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    rows = [a for a in repo.list_audit(db, DISPUTE_ID) if a["action"] == "save_spanish_statement"]
    assert len(rows) == 1 and rows[0]["actor"] == "vendor"
    assert ES_1 not in str(rows[0]) and "entregó" not in str(rows[0])


def test_english_edited_after_the_spanish_makes_the_row_stale(mx_on):
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    repo.mark_claim_edited(db, first, EN_1 + " The buyer signed for it.")
    row = service.view(db, package_id)["rows"][0]
    assert (row["es"], row["es_state"], row["stale"], row["previous_es"]) == ("", "missing", True, ES_1)


def test_whitespace_around_the_english_does_not_make_it_stale(mx_on):
    db = config.db_path()
    package_id = make_package()
    first = claim_ids(package_id)[0]
    service.save_seller_spanish(db, DISPUTE_ID, package_id, first, ES_1)
    repo.mark_claim_edited(db, first, "  " + EN_1 + "\n")
    assert service.view(db, package_id)["rows"][0]["es_state"] == "seller"


def test_a_new_version_of_the_same_dispute_keeps_the_spanish_of_the_same_english(mx_on):
    db = config.db_path()
    old = make_package()
    service.save_seller_spanish(db, DISPUTE_ID, old, claim_ids(old)[0], ES_1)
    new = make_package()
    assert [r["es_state"] for r in service.view(db, new)["rows"]] == ["seller", "missing"]
    other = make_package(dispute_id="PP-R-DEMO-0000002")
    assert [r["es_state"] for r in service.view(db, other)["rows"]] == ["missing", "missing"]


@pytest.mark.parametrize("text", ["", "   ", "\r\n\t"])
def test_empty_spanish_is_refused(mx_on, text):
    db = config.db_path()
    package_id = make_package()
    with pytest.raises(ValueError, match=service.EMPTY_MESSAGE):
        service.save_seller_spanish(db, DISPUTE_ID, package_id, claim_ids(package_id)[0], text)
    assert service.view(db, package_id)["rows"][0]["es_state"] == "missing"


def test_too_long_spanish_is_refused(mx_on):
    db = config.db_path()
    package_id = make_package()
    with pytest.raises(ValueError, match=service.TOO_LONG_MESSAGE):
        service.save_seller_spanish(db, DISPUTE_ID, package_id, claim_ids(package_id)[0], "a" * (service.MAX_LENGTH + 1))
    service.save_seller_spanish(db, DISPUTE_ID, package_id, claim_ids(package_id)[0], "a" * service.MAX_LENGTH)


def test_a_statement_of_another_package_is_refused(mx_on):
    db = config.db_path()
    package_id = make_package()
    other = make_package()
    with pytest.raises(LookupError):
        service.save_seller_spanish(db, DISPUTE_ID, package_id, claim_ids(other)[0], ES_1)


def test_saving_needs_the_feature_on(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    package_id = make_package()
    with pytest.raises(PermissionError):
        service.save_seller_spanish(config.db_path(), DISPUTE_ID, package_id, claim_ids(package_id)[0], ES_1)


def test_store_accepts_only_the_two_sources(mx_on):
    db = config.db_path()
    package_id = make_package()
    with pytest.raises(ValueError):
        store.save(db, package_id, claim_ids(package_id)[0], "0" * 64, ES_1, source="model")
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(str(db)) as conn:
            conn.executescript(store.DDL)
            conn.execute(f"INSERT INTO {store.TABLE} (created_at, package_id, claim_id, en_sha256, es, source) "
                         "VALUES ('x', 1, 1, 'x', 'x', 'model')")
