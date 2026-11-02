"""Deterministic offline extraction; verify source bytes before using any schema."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from .pins import PINS

FIXTURES = Path(__file__).with_name("fixtures")


def pointer(doc, value):
    node = doc
    for part in value.lstrip("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def clean(node, named_map=False):
    """Remove documentation annotations only; validation keywords remain byte-for-byte values."""
    if isinstance(node, list):
        return [clean(v) for v in node]
    if isinstance(node, dict):
        if named_map:
            return {k: clean(v) for k, v in node.items()}
        return {k: copy.deepcopy(v) if k in ("enum", "const", "default") else clean(v, k in ("properties", "patternProperties", "definitions", "$defs", "schemas", "parameters", "requestBodies", "responses"))
                for k, v in node.items() if k not in ("description", "title", "example", "examples", "externalDocs") and not k.startswith("x-")}
    return node


def refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"]
        for v in node.values():
            yield from refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs(v)


def place(doc, path, value):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]
    target = doc
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def read_pinned(directory):
    raw = {}
    for key, pin in PINS.items():
        path = Path(directory) / pin["file"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != pin["sha256"]:
            raise ValueError("pinned schema hash mismatch: " + pin["file"])
        raw[key] = data
    # Verify every file before any document is parsed or used.
    return {key: json.loads(data) for key, data in raw.items()}


def subset(doc, pin):
    if str(doc["info"]["version"]) != pin["version"]:
        raise ValueError("pinned document version mismatch")
    result = {"openapi": doc["openapi"], "info": {"version": pin["version"]}}
    pending, selected = list(pin["roots"]), set()
    while pending:
        path = pending.pop()
        if path in selected:
            continue
        value = clean(copy.deepcopy(pointer(doc, path)))
        place(result, path, value)
        selected.add(path)
        for ref in refs(value):
            if not ref.startswith("#/"):
                raise ValueError("external schema reference not permitted")
            pending.append(ref[1:])
    result["provenance"] = {"source_url": pin["url"], "fetch_date": "2026-10-06", "document_version": pin["version"],
                            "full_document_sha256": pin["sha256"], "json_pointers": ["#" + p for p in sorted(selected)],
                            "annotation_policy": "description/title/examples/externalDocs/x-* omitted; validation keywords preserved"}
    return result


def encoded(doc):
    return (json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def extract(directory):
    return {key: encoded(subset(doc, PINS[key])) for key, doc in read_pinned(directory).items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--specs-dir", required=True)
    ap.add_argument("--output-dir", type=Path, default=FIXTURES)
    args = ap.parse_args(argv)
    output = extract(args.specs_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for key, data in output.items():
        (args.output_dir / (key + ".json")).write_bytes(data)
        print(f"{key}: verified and extracted; bytes={len(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
