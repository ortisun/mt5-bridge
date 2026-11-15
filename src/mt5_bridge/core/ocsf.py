"""The bridge's OCSF vocabulary: schema version, classes, activities, objects.

Every identifier, caption and attribute name the bridge writes in a log line,
an audit event or a WebSocket message comes from this module, taken from the
Open Cybersecurity Schema Framework at https://schema.ocsf.io/ (version
``OCSF_VERSION``). No other module spells a ``class_uid`` or an attribute
name by hand; ``docs/ocsf.md`` holds the mapping table and the tests validate
what is emitted against a schema derived from the site
(``tools/vendor_ocsf_schema.py``).

Two builders share the vocabulary: ``event`` returns a complete event (audit
records, WebSocket messages) and ``log_extra`` returns the ``extra=`` a call
site passes to ``logging`` so that ``core.log_formatting.JsonLogFormatter``
renders the record as an event of the chosen class. This module has no
framework and no I/O.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from importlib import metadata as distribution_metadata

OCSF_VERSION = "1.9.0"

PRODUCT_NAME = "mt5-bridge"
PRODUCT_VENDOR_NAME = "mt5-bridge"


def _installed_version() -> str:
    try:
        return distribution_metadata.version(PRODUCT_NAME)
    except distribution_metadata.PackageNotFoundError:  # a source tree that is not installed
        return "0.0.0"


PRODUCT_VERSION = _installed_version()

# The key of the ``extra=`` field that carries OCSF attributes on a log record.
LOG_EXTRA_KEY = "ocsf"

TRACE_PROFILE = "trace"
ENTITY_TYPE_OTHER = 99
UNKNOWN_NAME = "unknown"


class Severity(IntEnum):
    UNKNOWN = 0
    INFORMATIONAL = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5
    FATAL = 6
    OTHER = 99


class Status(IntEnum):
    UNKNOWN = 0
    SUCCESS = 1
    FAILURE = 2
    OTHER = 99


class BaseActivity(IntEnum):
    UNKNOWN = 0
    OTHER = 99


class ApiActivity(IntEnum):
    UNKNOWN = 0
    CREATE = 1
    READ = 2
    UPDATE = 3
    DELETE = 4
    OTHER = 99


class LifecycleActivity(IntEnum):
    UNKNOWN = 0
    INSTALL = 1
    REMOVE = 2
    START = 3
    STOP = 4
    RESTART = 5
    ENABLE = 6
    DISABLE = 7
    UPDATE = 8
    OTHER = 99


class ErrorActivity(IntEnum):
    UNKNOWN = 0
    GENERAL_ERROR = 1
    TRANSLATION_ERROR = 2
    OTHER = 99


class EntityActivity(IntEnum):
    UNKNOWN = 0
    CREATE = 1
    READ = 2
    UPDATE = 3
    DELETE = 4
    MOVE = 5
    ENROLL = 6
    UNENROLL = 7
    ENABLE = 8
    DISABLE = 9
    ACTIVATE = 10
    DEACTIVATE = 11
    SUSPEND = 12
    RESUME = 13
    OTHER = 99


ACTIVITY_OTHER = 99


def caption(member: IntEnum) -> str:
    """The OCSF caption of an enum member: ``GENERAL_ERROR`` is ``General Error``."""
    return member.name.replace("_", " ").title()


def _captions(enum: type[IntEnum]) -> dict[int, str]:
    return {int(member): caption(member) for member in enum}


@dataclass(frozen=True, slots=True)
class EventClass:
    uid: int
    name: str
    category_uid: int
    category_name: str
    activities: Mapping[int, str]
    profiles: frozenset[str] = frozenset()

    def type_uid(self, activity_id: int) -> int:
        return self.uid * 100 + activity_id


BASE_EVENT = EventClass(0, "Base Event", 0, "Uncategorized", _captions(BaseActivity))
ENTITY_MANAGEMENT = EventClass(
    3004, "Entity Management", 3, "Identity & Access Management", _captions(EntityActivity)
)
APPLICATION_LIFECYCLE = EventClass(
    6002, "Application Lifecycle", 6, "Application Activity", _captions(LifecycleActivity)
)
API_ACTIVITY = EventClass(
    6003,
    "API Activity",
    6,
    "Application Activity",
    _captions(ApiActivity),
    profiles=frozenset({TRACE_PROFILE}),
)
APPLICATION_ERROR = EventClass(
    6008, "Application Error", 6, "Application Activity", _captions(ErrorActivity)
)

EVENT_CLASSES: Mapping[int, EventClass] = {
    event_class.uid: event_class
    for event_class in (
        BASE_EVENT,
        ENTITY_MANAGEMENT,
        APPLICATION_LIFECYCLE,
        API_ACTIVITY,
        APPLICATION_ERROR,
    )
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def epoch_ms(moment: datetime) -> int:
    """``timestamp_t``: whole milliseconds since the epoch (sub-millisecond part dropped)."""
    return (moment - _EPOCH) // _MILLISECOND


def classification(
    event_class: EventClass,
    activity_id: int,
    *,
    activity_name: str | None = None,
    captions: bool = True,
) -> dict[str, object]:
    """The classification attributes. ``activity_name`` is required for activity 99
    (Other), where OCSF wants the source's own label; ``captions=False`` leaves out
    the optional names, for messages sent at a high rate."""
    label = activity_name or event_class.activities.get(activity_id)
    if label is None or (activity_id == ACTIVITY_OTHER and activity_name is None):
        raise ValueError(f"{event_class.name}: activity {activity_id} needs an activity_name")
    attributes: dict[str, object] = {
        "class_uid": event_class.uid,
        "category_uid": event_class.category_uid,
        "activity_id": activity_id,
        "type_uid": event_class.type_uid(activity_id),
    }
    if captions:
        attributes["class_name"] = event_class.name
        attributes["category_name"] = event_class.category_name
        attributes["activity_name"] = label
        attributes["type_name"] = f"{event_class.name}: {label}"
    elif activity_id == ACTIVITY_OTHER:
        attributes["activity_name"] = label
    return attributes


def outcome(
    status: Status | None, *, status_code: str | None = None, status_detail: str | None = None
) -> dict[str, object]:
    """``status_id``/``status`` and the source's own code and detail, when known."""
    attributes: dict[str, object] = {}
    if status is not None:
        attributes["status_id"] = int(status)
        attributes["status"] = caption(status)
    if status_code is not None:
        attributes["status_code"] = status_code
    if status_detail is not None:
        attributes["status_detail"] = status_detail
    return attributes


def severity(level: Severity) -> dict[str, object]:
    return {"severity_id": int(level), "severity": caption(level)}


def product() -> dict[str, object]:
    return {"name": PRODUCT_NAME, "vendor_name": PRODUCT_VENDOR_NAME, "version": PRODUCT_VERSION}


def metadata(**attributes: object) -> dict[str, object]:
    """The ``metadata`` object: schema version and product, then the given
    attributes (``log_name``, ``uid``, ``sequence``, ``correlation_uid``,
    ``event_code``, ``original_time``, ``processed_time``, ``log_level``,
    ``labels``, ``profiles``); a ``None`` value is left out."""
    result: dict[str, object] = {"version": OCSF_VERSION, "product": product()}
    result.update({name: value for name, value in attributes.items() if value is not None})
    return result


def event(
    event_class: EventClass,
    activity_id: int,
    *,
    time_ms: int,
    level: Severity = Severity.INFORMATIONAL,
    activity_name: str | None = None,
    captions: bool = True,
    status: Status | None = None,
    status_code: str | None = None,
    status_detail: str | None = None,
    message: str | None = None,
    metadata_attributes: Mapping[str, object] | None = None,
    attributes: Mapping[str, object] | None = None,
    unmapped: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """A complete, JSON-ready OCSF event of ``event_class``."""
    result = classification(
        event_class, activity_id, activity_name=activity_name, captions=captions
    )
    result["time"] = time_ms
    if captions:
        result.update(severity(level))
    else:
        result["severity_id"] = int(level)
    result.update(outcome(status, status_code=status_code, status_detail=status_detail))
    if message is not None:
        result["message"] = message
    result["metadata"] = metadata(**(metadata_attributes or {}))
    if attributes:
        result.update(attributes)
    if unmapped:
        result["unmapped"] = dict(unmapped)
    return result


def log_extra(
    event_class: EventClass,
    activity_id: int,
    *,
    activity_name: str | None = None,
    status: Status | None = None,
    status_code: str | None = None,
    status_detail: str | None = None,
    metadata_attributes: Mapping[str, object] | None = None,
    attributes: Mapping[str, object] | None = None,
    unmapped: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The ``extra=`` for a log call that is an event of ``event_class``:

        logger.info("stopped", extra=log_extra(APPLICATION_LIFECYCLE, LifecycleActivity.STOP))

    The formatter adds what the record itself knows (time, severity from the
    level, message, logger name) and merges ``metadata`` and ``unmapped``.
    """
    fragment = classification(event_class, activity_id, activity_name=activity_name)
    fragment.update(outcome(status, status_code=status_code, status_detail=status_detail))
    if attributes:
        fragment.update(attributes)
    if metadata_attributes:
        fragment["metadata"] = dict(metadata_attributes)
    if unmapped:
        fragment["unmapped"] = dict(unmapped)
    return {LOG_EXTRA_KEY: fragment}


def event_as_log_extra(complete_event: Mapping[str, object]) -> dict[str, object]:
    """The ``extra=`` that makes a log record render as an already built event."""
    return {LOG_EXTRA_KEY: dict(complete_event)}


# Python logging levels to ``severity_id``. DEBUG and INFO need no action; a
# WARNING asks for attention without urgency; an ERROR needs action; CRITICAL
# (``logging.FATAL``) means the process may be unable to continue.
_SEVERITY_BY_LEVEL: tuple[tuple[int, Severity], ...] = (
    (50, Severity.FATAL),
    (40, Severity.HIGH),
    (30, Severity.MEDIUM),
    (0, Severity.INFORMATIONAL),
)


def severity_for_log_level(levelno: int) -> Severity:
    for threshold, level in _SEVERITY_BY_LEVEL:
        if levelno >= threshold:
            return level
    return Severity.INFORMATIONAL


def user(account_id: str | None) -> dict[str, object]:
    """A ``user`` object for the terminal account a request acts on. The bridge
    does not identify its callers, so this account is the only principal."""
    return {"uid": account_id} if account_id else {"name": UNKNOWN_NAME}


def actor(account_id: str | None) -> dict[str, object]:
    return {"user": user(account_id)}


def api(operation: str) -> dict[str, object]:
    return {"operation": operation}


def http_request(method: str, path: str) -> dict[str, object]:
    return {"http_method": method.upper(), "url": {"path": path}}


def network_endpoint(host: str | None, port: int | None = None) -> dict[str, object]:
    """A ``network_endpoint``: ``ip`` when ``host`` is an address, ``name`` otherwise
    (the object needs at least one of them)."""
    endpoint: dict[str, object] = {}
    if host:
        try:
            endpoint["ip"] = str(ipaddress.ip_address(host))
        except ValueError:
            endpoint["name"] = host
    else:
        endpoint["name"] = UNKNOWN_NAME
    if port is not None:
        endpoint["port"] = port
    return endpoint


def managed_entity(kind: str, uid: str, data: Mapping[str, object]) -> dict[str, object]:
    """A ``managed_entity`` of a type OCSF does not list: ``type_id`` 99, the
    type's own label and the entity's content under ``data``."""
    return {"type_id": ENTITY_TYPE_OTHER, "type": kind, "uid": uid, "data": dict(data)}


def trace_attributes(
    event_class: EventClass, trace_id: str, span_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Where a trace and span id go: ``(attributes, unmapped)``.

    OCSF carries them in the ``trace`` profile (``trace.uid`` and
    ``trace.span.uid``), which in this schema version applies only to API
    Activity and HTTP Activity. A ``span`` object also requires the span's
    start and end time, which a log line written inside a running span does
    not have, so the span id is always under ``unmapped``; the trace id is
    under ``trace.uid`` where the profile applies and under ``unmapped``
    elsewhere.
    """
    if TRACE_PROFILE in event_class.profiles:
        return {"trace": {"uid": trace_id}}, {"span_id": span_id}
    return {}, {"trace_id": trace_id, "span_id": span_id}


_ACTIVITY_BY_HTTP_METHOD: Mapping[str, ApiActivity] = {
    "POST": ApiActivity.CREATE,
    "GET": ApiActivity.READ,
    "HEAD": ApiActivity.READ,
    "PUT": ApiActivity.UPDATE,
    "PATCH": ApiActivity.UPDATE,
    "DELETE": ApiActivity.DELETE,
}


def api_activity_for_method(method: str) -> tuple[ApiActivity, str | None]:
    """An HTTP method as an API Activity ``(activity, activity_name)``; a method
    with no CRUD meaning is activity 99 named after the method."""
    activity = _ACTIVITY_BY_HTTP_METHOD.get(method.upper())
    if activity is None:
        return ApiActivity.OTHER, method.upper()
    return activity, None


def api_call_attributes(
    *, account_id: str | None, method: str, path: str, src_host: str | None, src_port: int | None
) -> dict[str, object]:
    """The API Activity attributes of one HTTP call: who, what, from where."""
    return {
        "actor": actor(account_id),
        "api": api(f"{method.upper()} {path}"),
        "http_request": http_request(method, path),
        "src_endpoint": network_endpoint(src_host, src_port),
    }


def lifecycle_log_extra(activity: LifecycleActivity) -> dict[str, object]:
    """``extra=`` for a start or stop of the bridge: an Application Lifecycle event."""
    return log_extra(
        APPLICATION_LIFECYCLE,
        activity,
        status=Status.SUCCESS,
        attributes={"app": product()},
    )
