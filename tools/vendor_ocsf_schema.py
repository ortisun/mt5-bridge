"""Derive the vendored OCSF subset schema the tests validate against.

    python3 tools/vendor_ocsf_schema.py

Needs the network; the tests do not. It downloads the JSON Schema export of
each event class the bridge emits from https://schema.ocsf.io (the version
is ``OCSF_VERSION`` below, the same one ``mt5_bridge.core.ocsf`` declares),
keeps the objects the bridge populates and writes one self-contained
document to ``tests/contracts/ocsf/``. Nothing is written by hand: every
``required`` list, constraint, enum and attribute name in the output comes
from the site. Attributes that hold an object outside ``KEPT_OBJECTS`` are
dropped, so an event that starts using one fails validation until this tool
runs again with the object added.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

OCSF_VERSION = "1.9.0"
SITE = "https://schema.ocsf.io"

# Class name -> the profiles applied to its export. Only API Activity can
# carry the trace profile among the classes used here.
CLASSES: dict[str, str] = {
    "base_event": "",
    "api_activity": "trace",
    "application_lifecycle": "",
    "application_error": "",
    "entity_management": "",
}

KEPT_OBJECTS = frozenset(
    {
        "actor",
        "api",
        "http_request",
        "managed_entity",
        "metadata",
        "network_endpoint",
        "object",
        "product",
        "span",
        "trace",
        "url",
        "user",
    }
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "tests" / "contracts" / "ocsf" / f"ocsf-{OCSF_VERSION}-subset.schema.json"
_REF_PREFIX = "#/$defs/"
_ATTEMPTS = 4
_TIMEOUT_SECONDS = 180


def fetch(class_name: str, profiles: str) -> dict[str, Any]:
    url = f"{SITE}/schema/{OCSF_VERSION}/classes/{class_name}?profiles={profiles}"
    request = urllib.request.Request(url, headers={"User-Agent": "ocsf-schema-vendoring"})
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                document: dict[str, Any] = json.load(response)
            return document
        except OSError as error:  # the export is slow and sometimes times out
            print(f"{class_name}: attempt {attempt} failed: {error}", file=sys.stderr)
    raise SystemExit(f"{class_name}: no answer from {url}")


def referenced_object(attribute: dict[str, Any]) -> str | None:
    items: dict[str, Any] = attribute.get("items", {})
    ref: str | None = attribute.get("$ref") or items.get("$ref")
    return ref.removeprefix(_REF_PREFIX) if ref else None


def prune(schema: dict[str, Any], where: str) -> dict[str, Any]:
    """``schema`` without titles and without attributes of objects not kept."""
    properties: dict[str, dict[str, Any]] = schema.get("properties", {})
    kept: dict[str, dict[str, Any]] = {}
    for name, attribute in sorted(properties.items()):
        target = referenced_object(attribute)
        if target is not None and target not in KEPT_OBJECTS:
            continue
        kept[name] = {key: value for key, value in attribute.items() if key != "title"}
    required: list[str] = schema.get("required", [])
    lost = [name for name in required if name not in kept]
    if lost:
        raise SystemExit(f"{where}: required attributes {lost} need an object outside KEPT_OBJECTS")
    pruned = {key: value for key, value in schema.items() if key not in {"$defs", "$id", "title"}}
    pruned.pop("$schema", None)
    pruned["properties"] = kept
    return pruned


def build() -> dict[str, Any]:
    defs: dict[str, Any] = {}
    dispatch: list[dict[str, Any]] = []
    class_uids: list[int] = []
    for class_name, profiles in CLASSES.items():
        export = fetch(class_name, profiles)
        exported_defs: dict[str, dict[str, Any]] = export["$defs"]
        for object_name in sorted(KEPT_OBJECTS & exported_defs.keys()):
            pruned = prune(exported_defs[object_name], object_name)
            if defs.setdefault(object_name, pruned) != pruned:
                raise SystemExit(f"object {object_name!r} differs between class exports")
        class_schema = prune(export, class_name)
        class_uid: int = class_schema["properties"]["class_uid"]["const"]
        defs[f"class_{class_name}"] = class_schema
        class_uids.append(class_uid)
        dispatch.append(
            {
                "if": {
                    "properties": {"class_uid": {"const": class_uid}},
                    "required": ["class_uid"],
                },
                "then": {"$ref": f"{_REF_PREFIX}class_{class_name}"},
            }
        )
    missing = sorted(KEPT_OBJECTS - defs.keys())
    if missing:
        raise SystemExit(f"objects {missing} are in no class export")
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": f"urn:mt5-bridge:ocsf-{OCSF_VERSION}-subset",
        "title": f"OCSF {OCSF_VERSION}: the event classes and objects mt5-bridge emits",
        "description": (
            f"Generated by tools/vendor_ocsf_schema.py from {SITE}/schema/{OCSF_VERSION}/classes/"
            "<class>. Do not edit by hand. Attributes holding an object the bridge does not "
            "populate are left out; everything kept is unchanged."
        ),
        "type": "object",
        "required": ["class_uid"],
        "properties": {"class_uid": {"enum": sorted(class_uids)}},
        "allOf": dispatch,
        "$defs": dict(sorted(defs.items())),
    }


def main() -> int:
    document = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
