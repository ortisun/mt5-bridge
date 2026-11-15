"""mt5_bridge.proto: the generated code matches it, and it covers what the
REST and WebSocket contracts cover: the operations, the channels, the problem
types and the OCSF events of the stream."""

import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.protobuf.internal.enum_type_wrapper import EnumTypeWrapper

from mt5_bridge.api.errors import CATALOGUE, ProblemType
from mt5_bridge.api.grpc_api import convert
from mt5_bridge.api.grpc_api.calls import Failure, problem_message
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.core import ocsf
from mt5_bridge.core.kill_switch import KillSwitchAction, KillSwitchState
from mt5_bridge.core.oms_state import BridgeOrderState, ReplaceOutcome
from mt5_bridge.mt5 import models

from testkit.contracts import load_openapi, load_ws_schema

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "src" / "mt5_bridge" / "api" / "grpc_api" / "generated"
GENERATED_FILES = (
    "mt5_bridge_pb2.py",
    "mt5_bridge_pb2.pyi",
    "mt5_bridge_pb2_grpc.py",
    "mt5_bridge_pb2_grpc.pyi",
)

# The REST operations whose RPC is not simply the operationId with a capital.
RENAMED = {"getBars": "ListBars", "getTicks": "ListTicks"}


def test_the_committed_generated_code_is_what_the_contract_generates(tmp_path: Path) -> None:
    """A change to the .proto without `make proto` fails here."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_proto.py"), "--output", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    fresh = tmp_path / "mt5_bridge" / "api" / "grpc_api" / "generated"

    # read_text: a Windows run may write other line endings than the checkout has.
    stale = [
        name
        for name in GENERATED_FILES
        if (fresh / name).read_text(encoding="utf-8")
        != (GENERATED / name).read_text(encoding="utf-8")
    ]

    assert stale == []


def test_every_rest_operation_has_an_rpc() -> None:
    operations = [
        operation["operationId"]
        for path in load_openapi()["paths"].values()
        for operation in path.values()
    ]
    rpcs = {
        method.name
        for service in pb.DESCRIPTOR.services_by_name.values()
        for method in service.methods
    }

    missing = [
        operation
        for operation in operations
        if RENAMED.get(operation, operation[0].upper() + operation[1:]) not in rpcs
    ]

    assert len(operations) > 20
    assert missing == []


def test_every_stream_channel_is_a_proto_channel() -> None:
    channels: list[str] = load_ws_schema()["$defs"]["Channel"]["enum"]

    assert sorted("CHANNEL_" + c.upper() for c in channels) == sorted(
        name for name in pb.Channel.keys() if name != "CHANNEL_UNSPECIFIED"
    )


@pytest.mark.parametrize(
    ("codec", "members", "wrapper"),
    [
        (convert.ORDER_SIDE, models.OrderSide, pb.OrderSide),
        (convert.ORDER_TYPE, models.OrderType, pb.OrderType),
        (convert.TIME_IN_FORCE, models.TimeInForce, pb.TimeInForce),
        (convert.FILLING_MODE, models.FillingMode, pb.FillingMode),
        (convert.POSITION_ACCOUNTING, models.PositionAccounting, pb.PositionAccounting),
        (convert.TRADING_PHASE, models.TradingPhase, pb.TradingPhase),
        (convert.ORDER_STATE, BridgeOrderState, pb.OrderState),
        (convert.REPLACE_OUTCOME, ReplaceOutcome, pb.ReplaceOutcome),
        (convert.KILL_SWITCH_ACTION, KillSwitchAction, pb.KillSwitchAction),
        (convert.KILL_SWITCH_STATE, KillSwitchState, pb.KillSwitchState),
        (convert.TIMEFRAME, models.Timeframe, pb.Timeframe),
        (convert.BOOK_SIDE, models.BookSide, pb.BookSide),
        (convert.SOURCE_TIME_UNIT, models.SourceTimeUnit, pb.SourceTimeUnit),
        (convert.TIME_BASIS, models.TimeBasis, pb.TimeBasis),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else "",
)
def test_every_enum_member_has_exactly_one_proto_value(
    codec: convert.EnumCodec[Any, Any], members: type[Enum], wrapper: EnumTypeWrapper
) -> None:
    """The conversion is by name at run time, so a member the .proto lacks
    would only fail when a response first carried it."""
    round_trip = [codec.from_pb(codec.to_pb(member), field="x") for member in members]

    assert round_trip == list(members)
    # The values of the .proto are these members plus the unspecified zero.
    assert len(wrapper.keys()) == len(members) + 1
    assert wrapper.Name(0).endswith("_UNSPECIFIED")


@pytest.mark.parametrize("problem_type", CATALOGUE, ids=lambda problem: problem.slug)
def test_every_problem_type_has_a_grpc_status_and_a_problem_message(
    problem_type: ProblemType,
) -> None:
    failure = Failure(problem_type, "what went wrong")

    problem = problem_message(failure, instance="/mt5_bridge.v1.Trading/SubmitOrder")

    assert failure.status is grpc.StatusCode[problem_type.grpc_status]
    assert failure.status is not grpc.StatusCode.OK
    assert (problem.type, problem.title, problem.code) == (
        problem_type.uri,
        problem_type.title,
        problem_type.code,
    )
    assert (problem.detail, problem.instance) == (
        "what went wrong",
        "/mt5_bridge.v1.Trading/SubmitOrder",
    )
    assert problem.retryable is problem_type.retryable
    assert problem.HasField("retry_after_seconds") is problem_type.retryable


def test_the_contract_lists_every_problem_type_under_its_status() -> None:
    header = (ROOT / "contracts" / "mt5_bridge" / "mt5_bridge.proto").read_text(encoding="utf-8")
    listed: dict[str, str] = {}
    status = ""
    for line in header.split('syntax = "proto3"')[0].splitlines():
        words = line.removeprefix("//").replace(",", " ").split()
        if words and words[0].isupper() and words[0] in grpc.StatusCode.__members__:
            status, words = words[0], words[1:]
        if status:
            listed.update(dict.fromkeys(words, status))

    assert listed == {problem.slug: problem.grpc_status for problem in CATALOGUE}


def test_an_rpc_has_the_audit_activity_of_the_rest_operation_it_mirrors() -> None:
    """The audit trail derives the activity of a gRPC write from the RPC's
    name; for REST it comes from the HTTP method. Both must agree."""
    rest = {
        operation["operationId"]: method
        for path in load_openapi()["paths"].values()
        for method, operation in path.items()
        if method != "get"
    }
    rest.pop("heartbeat")  # not audited by either transport
    rest.pop("checkOrder")  # a POST that reads: REST does not audit it either

    differing = {
        operation: method
        for operation, method in rest.items()
        if ocsf.api_activity_for_rpc(operation[0].upper() + operation[1:])
        != ocsf.api_activity_for_method(method)
    }

    assert len(rest) == 7
    assert differing == {}
