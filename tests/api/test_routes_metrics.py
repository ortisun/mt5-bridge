"""GET /v1/metrics: Prometheus exposition format (plan §84)."""

from testkit.app_builder import build_test_app
from testkit.http_client import TypedTestClient


def test_metrics_route_serves_prometheus_exposition_format_with_registered_gauges() -> None:
    # build_test_app_state already wires configure_metrics/register_state_gauges
    # against the same registry AppState.metrics_registry exposes.
    app, _fake_client, _state = build_test_app()

    with TypedTestClient(app) as http:
        response = http.get("/v1/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "mt5_bridge_kill_switch_state" in body
    assert "mt5_bridge_dead_man_switch_armed" in body
    assert "mt5_bridge_terminal_connected" in body
    assert "mt5_bridge_clock_drift_seconds" in body
