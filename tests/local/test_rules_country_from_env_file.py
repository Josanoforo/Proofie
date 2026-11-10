"""v1.1.2: COPILOT_RULES_COUNTRY set only in the .env file takes effect (it was read before the file was loaded)."""
import inspect

from copilot import config
from copilot.core import checklist_input


def test_a_country_that_arrives_with_the_env_file_replaces_the_import_time_value(monkeypatch):
    monkeypatch.delenv("COPILOT_RULES_COUNTRY", raising=False)
    monkeypatch.setattr(config, "RULES_COUNTRY", "MX")
    monkeypatch.setattr(checklist_input, "RULES_COUNTRY", "MX")
    monkeypatch.setattr(checklist_input, "_RULES_CACHE", [])
    assert checklist_input.cached_rules().country == "MX"
    monkeypatch.setenv("COPILOT_RULES_COUNTRY", "US")          # what loading the .env file does when the file sets it
    config._refresh_rules_country()                              # the step load_env_file() runs right after the load
    assert config.RULES_COUNTRY == "US" and checklist_input.cached_rules().country == "US"


def test_without_the_variable_nothing_changes(monkeypatch):
    monkeypatch.delenv("COPILOT_RULES_COUNTRY", raising=False)
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    config._refresh_rules_country()
    assert config.RULES_COUNTRY == "US"


def test_load_env_file_runs_the_refresh_after_the_load():
    source = inspect.getsource(config).split("def load_env_file")[1].split("def _refresh_rules_country")[0]
    assert source.index("load_dotenv(") < source.index("_refresh_rules_country()")
