"""Read-only pilot attempt/lock facts. Exit0 informational,2 invalid,3 outcome pending.

This tool never deletes, steals or resends. A missing/dead/unknown owner never authorizes a POST.
Existing empty lock files contain no PID, so their owner's liveness is unavailable.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import sys

STATES = {"pending", "sent_unconfirmed", "confirmed", "failed", "blocked", "abandoned"}


def pid_liveness(pid):
    if type(pid) is not int or not 0 < pid <= 2**32-1:
        return "unknown"
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return "dead" if ctypes.get_last_error() == 87 else "unknown"
        try:
            status = ctypes.c_uint32()
            return ("alive" if status.value == 259 else "dead") if kernel.GetExitCodeProcess(handle, ctypes.byref(status)) else "unknown"
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # Signal zero probes existence; it never stops a process.
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        return "unknown"
    return "alive"


def _safe_file(path):
    for item in (path, *path.parents):
        if item.is_symlink() or item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked paths cannot be inspected safely.")
    if path.stat().st_size > 2_000_000:
        raise ValueError("Inspection input exceeds the bounded size.")
    return path.read_bytes()


def _object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate metadata key.")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("Inspection input must be a JSON object.")
    return value


def inspect(package_path, attempts_dir=None, *, liveness=pid_liveness):
    package_path = Path(package_path).absolute()
    if package_path.suffix != ".json":
        raise ValueError("A package JSON file is required.")
    package = _object(_safe_file(package_path))
    did = package.get("dispute_id")
    sha = package.get("paypal_request_sha256")
    if not isinstance(did, str) or re.fullmatch(r"PP-[A-Z0-9-]{1,64}", did) is None:
        raise ValueError("The package dispute identifier is invalid.")
    if not isinstance(sha, str) or re.fullmatch(r"[a-f0-9]{64}", sha) is None:
        raise ValueError("The package request hash is invalid.")
    directory = Path(attempts_dir).absolute() if attempts_dir is not None else package_path.parent / "send_attempts"
    attempt = directory / (did + "__" + sha + ".json")
    lock = attempt.with_suffix(".lock")
    result = {"lock_present": (lock.exists() or lock.is_symlink()), "owner_pid": None, "owner_liveness": "unavailable",
              "attempt_present": (attempt.exists() or attempt.is_symlink()), "attempt_state": None, "sent_unconfirmed": False,
              "verify_argv": [".venv/Scripts/python.exe", "pilot/step5_send_evidence.py", "verify", "--package", str(package_path)],
              "retry_authorized": False, "limitation": "Absence of evidence is not proof that a POST did not land."}
    if result["lock_present"]:
        raw = _safe_file(lock)
        if raw.strip():
            metadata = _object(raw)
            pid = metadata.get("pid")
            if type(pid) is int and 0 < pid <= 2**32-1:
                result.update(owner_pid=pid, owner_liveness=liveness(pid))
            else:
                raise ValueError("Lock owner metadata is invalid.")
    if result["attempt_present"]:
        state = _object(_safe_file(attempt)).get("state")
        if state not in STATES:
            raise ValueError("Attempt state is invalid.")
        result.update(attempt_state=state, sent_unconfirmed=state == "sent_unconfirmed")
    code = 3 if result["lock_present"] or result["attempt_state"] in ("pending", "sent_unconfirmed") else 0
    return code, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--attempts-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        code, result = inspect(args.package, args.attempts_dir)
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"invalid": True, "retry_authorized": False,
                          "limitation": "Cannot inspect these records safely; nothing was changed or sent."}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
