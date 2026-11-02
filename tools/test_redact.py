"""Tests for tools/redact.py. Fixtures are synthetic and assembled at runtime so that this file itself holds no
secret-shaped literal (the staged-file scanner would otherwise block it)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact  # noqa: E402

FAKE = "x" * 30
TOKEN = "A21" + "AA" + FAKE
CLIENT_LIKE = "A" + "b1" * 38  # 77 chars
HEADER_BEARER = "Bearer" + " " + FAKE
HEADER_BASIC = "Basic" + " " + "QUJD" * 6
EMAIL = "someone" + "@" + "private-domain.io"
# Hosts are assembled from pieces (same convention as the internal guard hook): this file holds no literal live host.
LIVE_HOSTS = ["api-m" + ".paypal.com", "api" + ".paypal.com"]
NOT_LIVE_HOSTS = ["api-m.sandbox" + ".paypal.com", "api.sandbox" + ".paypal.com", "www" + ".paypal.com",
                  "developer" + ".paypal.com", "www.sandbox" + ".paypal.com"]


def kinds(text):
    return {k for _, k in redact.scan_text(text)}


def test_detects_oauth_token():
    assert "oauth_access_token" in kinds("token is " + TOKEN)


def test_detects_basic_and_bearer_headers():
    assert "auth_header" in kinds("Authorization: " + HEADER_BEARER)
    assert "auth_header" in kinds("Authorization: " + HEADER_BASIC)


def test_detects_credential_assignment_and_client_like():
    assert "credential_assignment" in kinds("client_" + "secret" + "=" + FAKE)
    assert "credential_assignment" in kinds('"client_' + 'id": "' + FAKE + '"')
    assert "client_id_like" in kinds("id " + CLIENT_LIKE)


def test_detects_email_but_not_safe_domains():
    assert "email" in kinds("contact " + EMAIL)
    assert "email" not in kinds("contact buyer" + "@" + "example.com")
    assert "email" not in kinds("Co-Authored-By: Claude <noreply" + "@" + "anthropic.com>")


def test_basic_as_an_ordinary_word_is_not_an_auth_header():
    """Real false positive found scanning this repo: "Basic Requirements" (a policy section name in
    copilot/rules/us_seller_protection_2026-01-26.json) matched the old shape-only pattern - "Requirements"
    alone is 12+ word characters. A real Basic/Bearer value always has at least one digit; this plain
    English phrase does not."""
    assert "auth_header" not in kinds("See section 'Basic Requirements' of the policy.")
    assert "auth_header" in kinds("Authorization: " + HEADER_BASIC)   # still catches the real shape


def test_a_generated_token_assignment_is_not_a_credential():
    """Real false positives found scanning this repo: Python code assigning a FRESH, generated value
    (never a hardcoded secret) still looks like "token = <16+ chars>" to the old pattern."""
    assert "credential_assignment" not in kinds("token = secrets.token_urlsafe(32)")
    assert "credential_assignment" not in kinds("token = page.cookies.get(CSRF_COOKIE_NAME)")
    # still catches a real hardcoded one (no function call involved):
    assert "credential_assignment" in kinds("token = " + FAKE)


def test_a_paypal_checkout_url_token_param_is_not_a_credential():
    """Real false positive found scanning this repo: evidence/sandbox_orders.jsonl's real approval URLs
    (https://www.sandbox.paypal.com/checkoutnow?token=<order id>) reuse the order id as that URL's own
    "token" query parameter - confirmed by reading the real line, never a secret."""
    assert "credential_assignment" not in kinds("\"https://www.sandbox.paypal.com/checkoutnow?token=" +
                                                "36J60973C92010242\"")
    # a bare key=value with no "?"/"&" right before it is still caught:
    assert "credential_assignment" in kinds("token=" + FAKE)


def test_detects_street_addresses():
    assert "street_address" in kinds("Ship to 742 Evergreen Terrace Street, Springfield")
    assert "street_address" in kinds("Calle Ejemplo 123, Ciudad de Mexico")
    assert "street_address" not in kinds("Version 3 of the plan is ready")


def test_clean_text_has_no_hits():
    assert redact.scan_text("Plain prose.\nMoney is integer cents: 1250.\n") == []


def test_hits_report_line_numbers_only():
    hits = redact.scan_text("ok\nok\n" + TOKEN + "\n")
    assert hits == [(3, "oauth_access_token")]


def test_redact_text_redacts_and_preserves_lines():
    text = "a\n" + TOKEN + "\nAuthorization: " + HEADER_BEARER + "\nmail " + EMAIL + "\n"
    out = redact.redact_text(text)
    for secret in (TOKEN, FAKE, EMAIL):
        assert secret not in out
    assert out.count("\n") == text.count("\n")
    assert "[REDACTED]" in out


def test_redact_text_redacts_multiline_json_values():
    text = '{"access_token": {\n "v": "' + FAKE + '"\n}, "ok": 1}'
    out = redact.redact_text(text)
    assert FAKE not in out and '"ok": 1' in out
    assert out.count("\n") == text.count("\n")


def test_is_private_paths():
    assert redact.is_private(Path(".env"))
    assert redact.is_private(Path(".git/config"))
    assert redact.is_private(Path("evidence/private/x.txt"))
    assert not redact.is_private(Path("docs/ENVIRONMENT.md"))


def test_cli_exit_codes_and_never_prints_values(capsys):
    with tempfile.TemporaryDirectory(prefix="proofie-redact-cli-") as scratch:
        tmp_path = Path(scratch)
        (tmp_path / "clean.md").write_text("nothing here\n", encoding="utf-8")
        assert redact.main([str(tmp_path)]) == 0
        capsys.readouterr()
        (tmp_path / "dirty.txt").write_text("x\n" + TOKEN + "\n", encoding="utf-8")
        assert redact.main([str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "dirty.txt:2 oauth_access_token" in out.replace("\\", "/")
        assert TOKEN not in out and FAKE not in out
        assert "hits=1" in out


def test_cli_skips_private_and_binary(tmp_path, capsys):
    (tmp_path / ".env").write_text(TOKEN + "\n", encoding="utf-8")
    (tmp_path / "x.png").write_bytes(b"\x89PNG" + TOKEN.encode())
    assert redact.main([str(tmp_path)]) == 0
    assert "skipped=2" in capsys.readouterr().out


def test_detects_both_live_hosts_with_and_without_scheme_in_mixed_case():
    for host in LIVE_HOSTS:
        mixed = "".join(c.upper() if i % 2 else c for i, c in enumerate(host))
        for variant in (host, host.upper(), mixed, "https://" + host, "HTTPS://" + mixed + "/v1/oauth2/token"):
            assert redact.scan_text("ok\ncall " + variant + " now\n") == [(2, "paypal_live_host")], variant


def test_does_not_flag_sandbox_www_or_developer_hosts():
    for host in NOT_LIVE_HOSTS:
        for variant in (host, "https://" + host + "/v2/checkout/orders"):
            assert "paypal_live_host" not in kinds("call " + variant), variant


def test_live_host_needs_a_real_host_boundary():
    assert "paypal_live_host" not in kinds("see " + LIVE_HOSTS[1] + "x or x" + LIVE_HOSTS[1])
    assert "paypal_live_host" in kinds("(" + LIVE_HOSTS[0] + "), then " + LIVE_HOSTS[1] + ".")


def test_redact_text_leaves_the_live_host_unchanged():
    """Detection only: redacting the host would hide a live reference in sanitized evidence."""
    text = "\n".join("POST https://" + h + "/v1/payments" for h in LIVE_HOSTS) + "\n"
    assert redact.redact_text(text) == text
    assert all(kind != "paypal_live_host" for kind, _ in redact.PATTERNS)


def test_live_host_does_not_change_existing_hits():
    assert redact.scan_text("host " + LIVE_HOSTS[0] + "\n" + TOKEN + "\n") == [
        (1, "paypal_live_host"), (2, "oauth_access_token")]


def test_cli_reports_live_host_by_location_only_and_never_prints_it(capsys):
    with tempfile.TemporaryDirectory(prefix="proofie-redact-live-") as scratch:
        tmp_path = Path(scratch)
        (tmp_path / "notes.md").write_text("fine\ncall https://" + LIVE_HOSTS[0] + "/x\n", encoding="utf-8")
        assert redact.main([str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "notes.md:2 paypal_live_host" in out.replace("\\", "/")
        assert "paypal.com" not in out.lower()
        assert "hits=1" in out


# ---- a JSON key `account_number` carrying a value is detected (scan rule; redact_text does not rewrite it) --
ACCOUNT_KEY = "account" + "_number"
ACCOUNT_DIGITS = "1234" + "5678" + "90123"


def test_detects_an_account_number_value_without_returning_it():
    line = '{"op": "x", "body": {"' + ACCOUNT_KEY + '": "' + ACCOUNT_DIGITS + '", "currency": "USD"}}'
    hits = redact.scan_text(line)
    assert (1, "account_number") in hits
    assert all(ACCOUNT_DIGITS not in str(h) for h in hits)                     # values are never returned


def test_detects_a_numeric_account_number_and_the_hyphenated_key_and_any_case():
    assert "account_number" in kinds('"' + ACCOUNT_KEY + '": ' + ACCOUNT_DIGITS)
    assert "account_number" in kinds('"account-' + 'number": "' + ACCOUNT_DIGITS + '"')
    assert "account_number" in kinds('"ACCOUNT_' + 'NUMBER" : "' + ACCOUNT_DIGITS + '"')


def test_does_not_flag_the_sanitisers_placeholder_an_empty_value_or_another_key():
    assert "account_number" not in kinds('"' + ACCOUNT_KEY + '": "[redacted]"')
    assert "account_number" not in kinds('"' + ACCOUNT_KEY + '": "[REDACTED]"')
    assert "account_number" not in kinds('"' + ACCOUNT_KEY + '": ""')
    assert "account_number" not in kinds('"' + ACCOUNT_KEY + '": null')
    assert "account_number" not in kinds('"account_' + 'id": "DEMOACCT00001"')
    assert "account_number" not in kinds("the " + ACCOUNT_KEY + " field is redacted by the sanitiser")   # prose, not JSON


def test_redact_text_leaves_the_detect_only_rule_alone():
    text = '"' + ACCOUNT_KEY + '": "' + ACCOUNT_DIGITS + '"'
    assert redact.redact_text(text) == text


# ---- identifier VALUES in JSON, Python/YAML and query-string forms --------------------------
# Every key and value is assembled from pieces, so this file holds no literal the rules would flag (the export scans it).
ID_KEYS = ["merchant" + "_id", "payer" + "_id", "account" + "_id", "account" + "_number", "email" + "_address"]
ID_VALUE = "AB12" + "CD34" + "EF56"
FORMS = [
    '{{"{k}": "{v}"}}',                      # JSON
    '{{"{k}":"{v}"}}',                       # JSON, no space
    '{{\\"{k}\\": \\"{v}\\"}}',              # JSON inside a JSON string (how evidence lines carry a body)
    '{k} = "{v}"',                           # Python
    "{k} = '{v}'",
    "{k}: {v}",                              # YAML, bare
    "{k}: '{v}'",
    "see /x?{k}={v}&other=1",                # query string
]


def test_every_identifier_key_with_a_value_is_detected_in_every_form():
    for key in ID_KEYS:
        for form in FORMS:
            line = form.format(k=key, v=ID_VALUE)
            assert (1, key) in redact.scan_text(line), (key, form)


def test_identifier_hits_never_return_the_value():
    for key in ID_KEYS:
        hits = redact.scan_text('{"' + key + '": "' + ID_VALUE + '"}')
        assert hits and all(ID_VALUE not in str(h) for h in hits)


def test_placeholder_empty_null_short_or_longer_key_are_not_identifier_hits():
    for key in ID_KEYS:
        for text in ('{"%s": "[redacted]"}' % key, '{"%s": "[REDACTED]"}' % key, '{"%s": ""}' % key,
                     '{"%s": null}' % key, "%s = None" % key, "%s: true" % key, "%s=abc" % key,
                     '{\\"%s\\": \\"[redacted]\\"}' % key, "the %s field is described here" % key,
                     '{"%s_confirmed": "%s"}' % (key, ID_VALUE)):
            assert key not in kinds(text), (key, text)


def test_bracketed_placeholders_demo_values_and_code_names_are_not_identifier_hits():
    """Found when the export reused these rules: `[email]`-style markers, the project's DEMO-prefixed synthetic ids
    and a variable such as `a_b_c` on the right of the key are not real identifiers."""
    for key in ID_KEYS:
        assert key not in kinds('{"%s": "[email]"}' % key)
        assert key not in kinds('{"%s": "[some marker]"}' % key)
        assert key not in kinds('{"%s": "DEMO%s"}' % (key, "MERCHANT0001"))
        assert key not in kinds("%s = norm_value_from_code" % key)
        assert key in kinds('{"%s": "[%s]"}' % (key, ID_VALUE))          # a bracket around a real value is not a marker


def test_only_an_uppercase_demo_prefix_exempts_a_value():
    """The patterns are case-insensitive, but the synthetic-value prefix is not: any other casing is a hit."""
    for key in ID_KEYS:
        for value in ("demo" + "abc12345", "Demo" + "graphic12", "dEMO" + "ABC12345"):
            assert key in kinds('{"%s": "%s"}' % (key, value)), (key, value)
            assert key in kinds("%s = %s" % (key, value)), (key, value)
        for value in ("DEMO" + "ABC12345", "DEMO" + "-ACC-0001", "DEMO" + "MERCHANT0001"):
            assert key not in kinds('{"%s": "%s"}' % (key, value)), (key, value)
            assert key not in kinds("%s = %s" % (key, value)), (key, value)
            assert key not in kinds('{\\"%s\\": \\"%s\\"}' % (key, value)), (key, value)


def test_a_function_call_on_the_right_is_code_not_a_value():
    """Found when the export copied its own source: `<key> = re.compile(...)` is not an identifier value. A bare
    token that is NOT followed by a call is still a hit."""
    for key in ID_KEYS:
        assert key not in kinds("_%s = re.compile(r'x')" % key.upper())
        assert key not in kinds("%s = lookup.get(row)" % key)
        assert key not in kinds("%s: build_value(1)" % key)
        assert key in kinds("%s = %s" % (key, ID_VALUE))
        assert key in kinds("%s = %s + other" % (key, ID_VALUE))


def test_a_key_that_only_ends_with_the_name_still_counts():
    """payer_account_id and similar keys carry the same data, so the match may sit after an underscore."""
    assert "account_id" in kinds('{"payer_' + 'account_id": "' + ID_VALUE + '"}')


def test_identifier_patterns_are_shared_not_copied():
    import personal_data_patterns as shared
    detect = dict(redact.DETECT_ONLY_PATTERNS)
    for kind, pattern in shared.VALUE_PATTERNS.items():
        assert detect[kind] is pattern


def test_sandbox_email_domains_are_safe_and_other_domains_are_not():
    for domain in ("personal.example" + ".com", "business.example" + ".com"):
        assert "email" not in kinds("buyer" + "@" + domain)
    assert "email" in kinds(EMAIL)


def test_cli_reports_an_identifier_value_by_location_only(capsys):
    with tempfile.TemporaryDirectory(prefix="proofie-redact-id-") as scratch:
        tmp_path = Path(scratch)
        (tmp_path / "x.json").write_text('{"ok": 1,\n "' + ID_KEYS[0] + '": "' + ID_VALUE + '"}\n', encoding="utf-8")
        assert redact.main([str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "x.json:2 merchant_id" in out.replace("\\", "/")
        assert ID_VALUE not in out
