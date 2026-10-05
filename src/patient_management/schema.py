"""Minimal JSON Schema (2020-12 subset) checker for this project's own schemas. Stdlib only.

Supports: $ref (cross-file), type, enum, const, pattern, min/maxLength, minimum/maximum, required,
properties, additionalProperties=false, items, min/maxItems, uniqueItems, allOf, anyOf, oneOf, not, if/then/else.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ponytail: repo layout assumed; move schemas into the package when the Skill bundle is built.
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
_DOCS: dict[str, dict] = {}
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _doc(name: str) -> dict:
    if name not in _DOCS:
        _DOCS[name] = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return _DOCS[name]


def validate(instance, name: str = "patient-state.schema.json", pointer: str = "") -> list[str]:
    errors: list[str] = []
    doc = _doc(name)
    _check(instance, _resolve(doc, pointer), name, "$", errors)
    return errors


def _resolve(doc: dict, pointer: str) -> dict:
    node = doc
    for part in pointer.strip("/").split("/"):
        if part:
            node = node[part]
    return node


def _is(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def _passes(value, schema, doc) -> bool:
    errs: list[str] = []
    _check(value, schema, doc, "", errs)
    return not errs


def _check(v, s: dict, doc: str, path: str, errs: list[str]) -> None:
    if "$ref" in s:
        file, _, ptr = s["$ref"].partition("#")
        target_doc = file or doc
        _check(v, _resolve(_doc(target_doc), ptr), target_doc, path, errs)
    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_is(v, t) for t in types):
            errs.append(f"{path}: expected {types}, got {type(v).__name__}")
            return
    if "const" in s and v != s["const"]:
        errs.append(f"{path}: must be {s['const']!r}")
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not in enum")
    if isinstance(v, str):
        if len(v) < s.get("minLength", 0) or len(v) > s.get("maxLength", float("inf")):
            errs.append(f"{path}: length {len(v)} out of range")
        if "pattern" in s and not re.search(s["pattern"], v):
            errs.append(f"{path}: {v!r} does not match {s['pattern']}")
    if _is(v, "number"):
        if v < s.get("minimum", float("-inf")) or v > s.get("maximum", float("inf")):
            errs.append(f"{path}: {v} out of range")
    if isinstance(v, list):
        if len(v) < s.get("minItems", 0) or len(v) > s.get("maxItems", float("inf")):
            errs.append(f"{path}: {len(v)} items out of range")
        if s.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in v}) != len(v):
            errs.append(f"{path}: items not unique")
        if "items" in s:
            for i, x in enumerate(v):
                _check(x, s["items"], doc, f"{path}[{i}]", errs)
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v:
                errs.append(f"{path}: missing {k}")
        props = s.get("properties", {})
        for k, x in v.items():
            if k in props:
                _check(x, props[k], doc, f"{path}.{k}", errs)
            elif s.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected field {k}")
    for sub in s.get("allOf", []):
        _check(v, sub, doc, path, errs)
    if "anyOf" in s and not any(_passes(v, sub, doc) for sub in s["anyOf"]):
        errs.append(f"{path}: must match at least one of anyOf")
    if "oneOf" in s and sum(_passes(v, sub, doc) for sub in s["oneOf"]) != 1:
        errs.append(f"{path}: must match exactly one of oneOf")
    if "not" in s and _passes(v, s["not"], doc):
        errs.append(f"{path}: must not match 'not' schema")
    if "if" in s:
        branch = s.get("then") if _passes(v, s["if"], doc) else s.get("else")
        if branch:
            _check(v, branch, doc, path, errs)


def inline(name: str, pointer: str) -> dict:
    """Schema fragment with all $refs expanded (for self-contained task packages)."""
    def expand(node, doc):
        if isinstance(node, list):
            return [expand(x, doc) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k == "$ref":
                file, _, ptr = v.partition("#")
                out.update(expand(_resolve(_doc(file or doc), ptr), file or doc))
            elif k != "description":
                out[k] = expand(v, doc)
        return out
    return expand(_resolve(_doc(name), pointer), name)
