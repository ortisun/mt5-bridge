"""Locate and load the canonical contract files (``contracts/mt5_bridge/`` at the repository root).

Only test code reads ``contracts/mt5_bridge/`` like this — the bridge's own
runtime code never depends on this path, so the installed package runs
without the contract files beside it.

The ``referencing``/``jsonschema`` registry types below are generic in a way
this Pyright/library version combination does not fully resolve (the
``Registry``/``Resource`` type parameter stays ``Unknown``); the four
``pyright: ignore`` markers are contained to this one module, and every
validator built here is exercised end to end by the contract-conformance
tests, so the gap is a static-typing limitation, not an unverified behavior.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator, Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

# tests/testkit/contracts.py -> the repository root is 2 parents up.
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "mt5_bridge"
OPENAPI_PATH = CONTRACTS_DIR / "openapi.yaml"
WS_SCHEMA_PATH = CONTRACTS_DIR / "ws_messages.schema.json"

_OPENAPI_RESOURCE_URI = "urn:mt5-bridge-openapi"


def load_openapi() -> dict[str, Any]:
    with OPENAPI_PATH.open(encoding="utf-8") as stream:
        document: dict[str, Any] = yaml.safe_load(stream)
    return document


def load_ws_schema() -> dict[str, Any]:
    with WS_SCHEMA_PATH.open(encoding="utf-8") as stream:
        schema: dict[str, Any] = json.load(stream)
    return schema


def openapi_schema_validator(document: dict[str, Any], schema_name: str) -> Draft202012Validator:
    """A ``$ref: '#/components/schemas/X'``-resolving validator for one
    OpenAPI component schema, keyed by a synthetic in-memory ``$id`` (the
    document itself carries none — OpenAPI does not use one — this augments
    only the copy used for validation)."""
    addressable = {**document, "$id": _OPENAPI_RESOURCE_URI}
    resource = Resource.from_contents(addressable, default_specification=DRAFT202012)
    registry: Registry[Any] = resource @ Registry()  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    ref = f"{_OPENAPI_RESOURCE_URI}#/components/schemas/{schema_name}"
    return Draft202012Validator({"$ref": ref}, registry=registry)


def ws_schema_validator(schema: dict[str, Any], def_name: str) -> Draft202012Validator:
    """A ``$ref: '#/$defs/X'``-resolving validator for one ws-message definition."""
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry: Registry[Any] = resource @ Registry()  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    schema_id = schema["$id"]
    return Draft202012Validator({"$ref": f"{schema_id}#/$defs/{def_name}"}, registry=registry)


def assert_conforms(validator: Draft202012Validator, instance: object) -> None:
    """Raise ``jsonschema.ValidationError`` if ``instance`` does not conform.

    ``Draft202012Validator.validate`` itself resolves to a partially unknown
    signature under this jsonschema version; contained here instead of at
    each of dozens of call sites.
    """
    validator.validate(instance)  # pyright: ignore[reportUnknownMemberType]


# The OCSF classes and objects the bridge emits, derived from schema.ocsf.io by
# tools/vendor_ocsf_schema.py; validating against it needs no network.
OCSF_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "ocsf" / "ocsf-1.9.0-subset.schema.json"
)


def load_ocsf_schema() -> dict[str, Any]:
    with OCSF_SCHEMA_PATH.open(encoding="utf-8") as stream:
        schema: dict[str, Any] = json.load(stream)
    return schema


_OCSF_VALIDATOR = Draft7Validator(load_ocsf_schema())


def assert_is_ocsf_event(event: Mapping[str, Any]) -> None:
    """``event`` is a valid event of its OCSF class: every required attribute
    is present, no attribute is outside the schema, enums and object
    constraints hold, and ``type_uid`` is ``class_uid * 100 + activity_id``."""
    _OCSF_VALIDATOR.validate(event)  # pyright: ignore[reportUnknownMemberType]
    assert event["type_uid"] == event["class_uid"] * 100 + event["activity_id"]
    if event["activity_id"] == 99:
        assert event.get("activity_name"), "activity 99 (Other) needs the source's activity_name"
