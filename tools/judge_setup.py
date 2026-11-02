"""The judge's credentials step (D-101 R1-1): `python tools/judge_check.py --setup`.

Asks for the sandbox Client ID and Secret with hidden input and writes the settings file, so nobody edits it by hand.
The file is a copy of the shipped example with only the two credential lines replaced, so the feature lines match too.
It never prints, logs or echoes a value: the output carries a scope count and a yes/no for the dispute scopes, nothing else.
One token request, to the sandbox host only. Nothing is written to PayPal.
"""
from __future__ import annotations

import base64
import getpass
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

SANDBOX_HOST = "api-m.sandbox.paypal.com"
SANDBOX_TOKEN_URL = f"https://{SANDBOX_HOST}/v1/oauth2/token"
TARGET_NAME = ".env"
EXAMPLE_NAME = ".env.example"
BUYER_NAME = ".env.buyer"
KEYS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
MIN_LENGTH = 20
YES = ("s", "si", "sí", "y", "yes")


def git_ignores(root: Path, path: Path) -> bool:
    return subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(path)], capture_output=True).returncode == 0


def _pairs(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        found = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if found and not line.lstrip().startswith("#"):
            out[found.group(1)] = found.group(2).strip().strip('"').strip("'")
    return out


def buyer_values(root: Path) -> set:
    """The buyer app's id and secret, read only to refuse them here by mistake; never printed."""
    return {v for k, v in _pairs(root / BUYER_NAME).items() if k in KEYS and v}


def valid(value: str) -> bool:
    return bool(value) and not any(ch.isspace() for ch in value) and len(value) >= MIN_LENGTH


def render_settings(example_text: str, client_id: str, secret: str) -> str:
    """The example's text with only the two credential lines replaced."""
    lines, seen = [], set()
    for line in example_text.splitlines():
        key = line.partition("=")[0].strip()
        if key in KEYS and not line.lstrip().startswith("#"):
            lines.append(f"{key}={client_id if key == KEYS[0] else secret}")
            seen.add(key)
        else:
            lines.append(line)
    for key in KEYS:
        if key not in seen:
            lines.append(f"{key}={client_id if key == KEYS[0] else secret}")
    return "\n".join(lines) + "\n"


def sandbox_token_scopes(client_id: str, secret: str, url: str = SANDBOX_TOKEN_URL):
    """One sandbox token request. Returns the scope list, or an int HTTP status, or None (no network). Sandbox host only."""
    if urllib.request.urlsplit(url).hostname != SANDBOX_HOST:
        raise ValueError("the credentials check calls the sandbox host only")
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    request = urllib.request.Request(url, data=b"grant_type=client_credentials", method="POST",
                                     headers={"Authorization": f"Basic {auth}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode()).get("scope", "").split()
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def setup(root: Path, *, ask=getpass.getpass, confirm=input, token=sandbox_token_scopes, ignores=None, say=print) -> int:
    """Returns 0 when nothing is wrong (including a kept file), 1 when nothing was written because of a refusal."""
    root = Path(root)
    target, example = root / TARGET_NAME, root / EXAMPLE_NAME
    ignores = ignores or (lambda path: git_ignores(root, path))
    say("Sandbox credentials -> settings file (nothing you type is shown or stored anywhere else)")
    if not example.exists():
        say("  The example settings file is missing; run this from the project folder. Nothing written.")
        return 1
    if not ignores(target):
        say("  git does not ignore the settings file in this folder; nothing written.")
        return 1
    if target.exists():
        if confirm("  The settings file already exists. Replace it? (s/n): ").strip().lower() not in YES:
            say("  Kept as it is. Nothing changed.")
            return 0
    values = []
    for label in ("Client ID", "Secret"):
        value = ask(f"{label} (hidden): ").strip()
        if not valid(value):
            say(f"  {label}: empty, with spaces or too short. Nothing written.")
            return 1
        values.append(value)
    client_id, secret = values
    if client_id == secret:
        say("  Client ID and Secret are the same. Nothing written.")
        return 1
    if {client_id, secret} & buyer_values(root):
        say("  Those are the buyer app's values; use the seller app's. Nothing written.")
        return 1
    target.write_text(render_settings(example.read_text(encoding="utf-8"), client_id, secret), encoding="utf-8", newline="\n")
    say("  Saved the settings file (git ignores it; no value was shown).")
    scopes = token(client_id, secret)
    if isinstance(scopes, list):
        disputes = any("disputes" in scope for scope in scopes)
        say(f"  The sandbox accepted the credentials: {len(scopes)} scopes; dispute scopes: {'yes' if disputes else 'NO (enable Customer disputes on the app)'}.")
    elif isinstance(scopes, int):
        say(f"  The sandbox refused the credentials (HTTP {scopes}). Check you copied the seller app's id and secret.")
    else:
        say("  Could not reach the sandbox (no network). The file is saved anyway.")
    return 0
