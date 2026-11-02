"""Offline request/response views with only OpenAPI's documented translations."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import best_match
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .extract import FIXTURES, pointer, read_pinned
from .pins import PINS


def view(node, drop):
    if isinstance(node, list):
        return [view(v, drop) for v in node]
    if not isinstance(node, dict):
        return node
    result = {k: view(v, drop) for k, v in node.items()}
    props = result.get("properties")
    if isinstance(props, dict):
        removed = {k for k, v in props.items() if isinstance(v, dict) and v.get(drop) is True}
        result["properties"] = {k: v for k, v in props.items() if k not in removed}
        if isinstance(result.get("required"), list):
            result["required"] = [k for k in result["required"] if k not in removed]
    if result.get("nullable") is True:
        kind = result.get("type")
        if isinstance(kind, str):
            result["type"] = [kind, "null"]
        elif isinstance(kind, list) and "null" not in kind:
            result["type"] = kind + ["null"]
        if isinstance(result.get("enum"), list) and None not in result["enum"]:
            result["enum"] += [None]
    return result


def artefact(error):
    if isinstance(error.instance, str) and (error.instance == "[redacted]" or "[email]" in error.instance):
        return True
    leaf = best_match(error.context) if error.context else None
    return leaf is not None and artefact(leaf)


def issues(validator, instance):
    # Error messages embed values; only generalized field pointers and keyword names are exposed.
    return [{"pointer": "/" + "/".join("*" if isinstance(p, int) else str(p).replace("~", "~0").replace("/", "~1") for p in e.absolute_path),
             "keyword": e.validator, "artefact": artefact(e)} for e in validator.iter_errors(instance)]


class Documents:
    def __init__(self, full_directory=None):
        self.raw = read_pinned(full_directory) if full_directory is not None else {key: json.loads((FIXTURES / (key + ".json")).read_text(encoding="utf-8")) for key in PINS}
        resources = []
        for key, doc in self.raw.items():
            if full_directory is None:
                prov = doc["provenance"]
                if prov["full_document_sha256"] != PINS[key]["sha256"] or prov["document_version"] != PINS[key]["version"]:
                    raise ValueError("subset provenance mismatch: " + key)
            for mode, drop in (("req", "readOnly"), ("res", "writeOnly")):
                resources.append((f"urn:proofie:contract:{key}:{mode}", Resource.from_contents(view(doc, drop), default_specification=DRAFT202012)))
        self.registry = Registry().with_resources(resources)
        self.format_checker = FormatChecker()

    def validator(self, key, mode, path):
        return Draft202012Validator({"$ref": f"urn:proofie:contract:{key}:{mode}#{path.lstrip('#')}"}, registry=self.registry, format_checker=self.format_checker)

    def deref(self, key, node):
        while isinstance(node, dict) and "$ref" in node:
            node = pointer(self.raw[key], node["$ref"])
        return node

    def evidence_validator(self):
        return Draft202012Validator({"type": "object", "required": ["evidences"], "properties": {"evidences": {"$ref": "urn:proofie:contract:disputes:req#/components/schemas/evidence_list"}}}, registry=self.registry, format_checker=self.format_checker)
