"""丙 · 应用层契约测试（docs/05 §3）。

只验证接口形状与字段，**不连 ONOS**：dijkstra / install_path / uninstall
一律用 monkeypatch 替换，app.ctx 每个测试自己重造，不共享全局状态。
跑法：cd brain && python3 -m pytest tests/test_api.py -q
"""

from __future__ import annotations

import pytest

import main as main_module
from main import app

S1 = "of:0000000000000001"
S3 = "of:0000000000000003"

STATE_FIELDS = {
    "graph", "hosts", "paths", "last_diff",
    "last_poll_ts", "poll_count", "poll_ms",
    "onos_ok", "onos_errors", "last_error",
    "ws_clients", "push_count", "started_at", "ts",
}

FAKE_GRAPH = {S1: {S3: {"sport": 2, "dport": 3, "weight": 1}}, S3: {S1: {"sport": 3, "dport": 2, "weight": 1}}}
FAKE_HOSTS = {
    "10.0.0.1": {"dpid": S1, "port": 1, "mac": "00:00:00:00:00:01"},
    "10.0.0.3": {"dpid": S3, "port": 1, "mac": "00:00:00:00:00:03"},
}
FAKE_RES = {"path": [S1, S3], "cost": 1, "out_ports": [2]}
FAKE_RECORD = {
    "path": [S1, S3],
    "out_ports": [2],
    "flow_ids": [(S1, "111"), (S3, "222")],
    "installed_at": 1730000000.0,
}


@pytest.fixture
def ctx():
    """每个测试独立重造 app.ctx（docs/05 §3：不许依赖执行顺序）。"""
    main_module.init_ctx(app)
    app.ctx.graph = FAKE_GRAPH
    app.ctx.host_table = FAKE_HOSTS
    return app.ctx


def test_state_has_14_fields(ctx):
    _, res = app.test_client.get("/api/state")
    assert res.status == 200
    assert STATE_FIELDS <= set(res.json.keys())
    assert res.json["graph"] == FAKE_GRAPH
    assert res.json["hosts"] == FAKE_HOSTS
    assert res.json["paths"] == []
    assert res.json["onos_errors"] == 0
    assert res.json["ws_clients"] == 0


def test_route_ok(ctx, monkeypatch):
    monkeypatch.setattr(main_module.path, "dijkstra", lambda g, s, d: FAKE_RES)
    monkeypatch.setattr(
        main_module.flow, "install_path", lambda key, res, sh, dh: FAKE_RECORD
    )
    _, res = app.test_client.post(
        "/api/route", json={"src": "10.0.0.1", "dst": "10.0.0.3"}
    )
    assert res.status == 200
    assert res.json == {"ok": True, "path": [S1, S3], "cost": 1}
    # 下发记录进入 deployed，/api/state 的 paths 摘要随之可见。
    _, state = app.test_client.get("/api/state")
    assert state.json["paths"] == [
        {"src": "10.0.0.1", "dst": "10.0.0.3", "path": [S1, S3], "installed_at": 1730000000.0}
    ]


def test_route_unreachable(ctx, monkeypatch):
    monkeypatch.setattr(main_module.path, "dijkstra", lambda g, s, d: None)
    _, res = app.test_client.post(
        "/api/route", json={"src": "10.0.0.1", "dst": "10.0.0.3"}
    )
    assert res.status == 200  # 不可达不是异常（docs/02 §7）
    assert res.json == {"ok": False, "error": "unreachable"}


def test_route_unknown_host(ctx):
    _, res = app.test_client.post(
        "/api/route", json={"src": "10.0.0.1", "dst": "10.0.0.99"}
    )
    assert res.status == 404
    assert res.json["error"] == "unknown_host"


def test_route_missing_params(ctx):
    _, res = app.test_client.post("/api/route", json={})
    assert res.status == 400


def test_delete_route_ok(ctx, monkeypatch):
    ctx.deployed = {("10.0.0.1", "10.0.0.3"): FAKE_RECORD}
    monkeypatch.setattr(main_module.flow, "uninstall", lambda key: True)
    # sanic-testing 的 delete() 不收 json 参数；request() 用 http_method 指定方法。
    _, res = app.test_client.request(
        "/api/route", http_method="DELETE", json={"src": "10.0.0.1", "dst": "10.0.0.3"}
    )
    assert res.status == 200
    assert res.json == {"ok": True}
    assert app.ctx.deployed == {}


def test_delete_route_not_found(ctx):
    _, res = app.test_client.request(
        "/api/route", http_method="DELETE", json={"src": "10.0.0.1", "dst": "10.0.0.3"}
    )
    assert res.status == 404
    assert res.json["error"] == "not_found"


def test_diff_empty_before_first_poll(ctx):
    _, res = app.test_client.get("/api/diff")
    assert res.status == 200
    assert res.json == {}


def test_diff_returns_last_diff(ctx):
    fake_diff = {"changed": True, "edges_removed": [[S1, S3]], "ts": 1730000000.0}
    ctx.last_diff = fake_diff
    _, res = app.test_client.get("/api/diff")
    assert res.json == fake_diff


def test_multicast_not_implemented_in_phase1(ctx):
    _, res = app.test_client.post(
        "/api/multicast", json={"src": "10.0.0.1", "dsts": ["10.0.0.3"], "group_id": 1}
    )
    assert res.status == 501
    assert res.json["error"] == "not_implemented"
