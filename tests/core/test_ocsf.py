"""core.ocsf: the vocabulary and the two builders (docs/ocsf.md)."""

import logging
from datetime import UTC, datetime

import pytest

from mt5_bridge.core import ocsf

from testkit.contracts import assert_is_ocsf_event


def test_type_uid_is_the_class_uid_times_100_plus_the_activity() -> None:
    attributes = ocsf.classification(ocsf.API_ACTIVITY, ocsf.ApiActivity.DELETE)

    assert attributes == {
        "class_uid": 6003,
        "category_uid": 6,
        "activity_id": 4,
        "type_uid": 600304,
        "class_name": "API Activity",
        "category_name": "Application Activity",
        "activity_name": "Delete",
        "type_name": "API Activity: Delete",
    }


def test_activity_other_needs_the_sources_own_name() -> None:
    with pytest.raises(ValueError, match="activity_name"):
        ocsf.classification(ocsf.BASE_EVENT, ocsf.BaseActivity.OTHER)


def test_without_captions_only_the_required_classification_remains() -> None:
    attributes = ocsf.classification(
        ocsf.BASE_EVENT, ocsf.BaseActivity.OTHER, activity_name="Tick", captions=False
    )

    assert attributes == {
        "class_uid": 0,
        "category_uid": 0,
        "activity_id": 99,
        "type_uid": 99,
        "activity_name": "Tick",
    }


def test_a_caption_is_the_enum_name_in_title_case() -> None:
    assert ocsf.caption(ocsf.ErrorActivity.GENERAL_ERROR) == "General Error"
    assert ocsf.caption(ocsf.Severity.INFORMATIONAL) == "Informational"


def test_epoch_ms_drops_the_sub_millisecond_part() -> None:
    moment = datetime(2024, 1, 1, 0, 0, 1, 2_999, tzinfo=UTC)

    assert ocsf.epoch_ms(moment) == 1_704_067_201_002


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (logging.DEBUG, ocsf.Severity.INFORMATIONAL),
        (logging.INFO, ocsf.Severity.INFORMATIONAL),
        (logging.WARNING, ocsf.Severity.MEDIUM),
        (logging.ERROR, ocsf.Severity.HIGH),
        (logging.CRITICAL, ocsf.Severity.FATAL),
    ],
)
def test_a_python_level_maps_to_a_severity(level: int, expected: ocsf.Severity) -> None:
    assert ocsf.severity_for_log_level(level) is expected


def test_a_network_endpoint_holds_an_address_as_ip_and_anything_else_as_name() -> None:
    assert ocsf.network_endpoint("10.0.0.7", 5000) == {"ip": "10.0.0.7", "port": 5000}
    assert ocsf.network_endpoint("testclient", 5000) == {"name": "testclient", "port": 5000}
    assert ocsf.network_endpoint(None) == {"name": "unknown"}


def test_the_trace_id_is_in_the_trace_profile_only_where_the_class_has_it() -> None:
    with_profile = ocsf.trace_attributes(ocsf.API_ACTIVITY, "t" * 32, "s" * 16)
    without_profile = ocsf.trace_attributes(ocsf.BASE_EVENT, "t" * 32, "s" * 16)

    assert with_profile == ({"trace": {"uid": "t" * 32}}, {"span_id": "s" * 16})
    assert without_profile == ({}, {"trace_id": "t" * 32, "span_id": "s" * 16})


def test_a_built_event_of_every_class_is_a_valid_ocsf_event() -> None:
    call = ocsf.api_call_attributes(
        account_id="12345678", method="POST", path="/v1/orders", src_host="10.0.0.7", src_port=1
    )
    events = [
        ocsf.event(ocsf.BASE_EVENT, ocsf.BaseActivity.OTHER, activity_name="Log", time_ms=1),
        ocsf.event(ocsf.APPLICATION_ERROR, ocsf.ErrorActivity.GENERAL_ERROR, time_ms=1),
        ocsf.event(
            ocsf.APPLICATION_LIFECYCLE,
            ocsf.LifecycleActivity.START,
            time_ms=1,
            attributes={"app": ocsf.product()},
        ),
        ocsf.event(ocsf.API_ACTIVITY, ocsf.ApiActivity.CREATE, time_ms=1, attributes=call),
        ocsf.event(
            ocsf.ENTITY_MANAGEMENT,
            ocsf.EntityActivity.UPDATE,
            time_ms=1,
            attributes={"entity": ocsf.managed_entity("Order", "co-1", {"state": "FILLED"})},
        ),
    ]

    for event in events:
        assert_is_ocsf_event(event)
        assert event["metadata"] == {"version": "1.9.0", "product": ocsf.product()}
