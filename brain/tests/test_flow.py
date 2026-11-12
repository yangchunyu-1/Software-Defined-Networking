"""乙 · 决策/执行层契约测试（docs/02 §3.5 / §3.6）。

之前 flow.py 完全没有单测，全部被 test_api 的 monkeypatch 替换掉——
所以「丙的 4 参数调用 vs 乙的 8 参数签名」这个 TypeError 一路绿灯到联调。
本文件只打桩 ONOS（FakeOnos），flow / path 的真实逻辑全部参与。

跑法：cd brain && python3 -m pytest tests/test_flow.py -q
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import flow
import path

A = "of:0000000000000001"
B = "of:0000000000000002"
C = "of:0000000000000004"

# A --2/3--> B --5/4--> C （sport/dport）
GRAPH = {
    A: {B: {"sport": 2, "dport": 3, "weight": 1}},
    B: {A: {"sport": 3, "dport": 2, "weight": 1},
        C: {"sport": 5, "dport": 4, "weight": 1}},
    C: {B: {"sport": 4, "dport": 5, "weight": 1}},
}

H1 = {"dpid": A, "port": 1, "mac": "00:00:00:00:00:01"}
H4 = {"dpid": C, "port": 1, "mac": "00:00:00:00:00:04"}
KEY = ("10.0.0.1", "10.0.0.4")
PRIORITY = 40000


class FakeOnos:
    """只实现 install_path / uninstall / recompute 用到的那三个方法。"""

    def __init__(self, fail_on=None):
        self.flows: dict[str, list[dict]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.calls = 0

    async def post_flow(self, device_id, flow):
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise RuntimeError("simulated POST failure")
        fid = "f%d" % self.calls
        self.flows.setdefault(device_id, []).append(
            {"id": fid, "selector": flow["selector"], "treatment": flow["treatment"]}
        )
        return fid

    async def fetch_flows(self, device_id):
        return list(self.flows.get(device_id, []))

    async def delete_flow(self, device_id, flow_id):
        self.deleted.append((device_id, flow_id))
        self.flows[device_id] = [
            f for f in self.flows.get(device_id, []) if f["id"] != flow_id
        ]
        return True


def test_path_to_flows_ports_follow_hop_semantics():
    """首跳入端口来自源主机，中间出了再进，末跳出端口来自目的主机。"""
    res = path.dijkstra(GRAPH, A, C)
    assert res == {"path": [A, B, C], "cost": 2, "out_ports": [2, 5]}

    flows = flow.path_to_flows(KEY, res, GRAPH, H1, H4, PRIORITY)
    assert [dev for dev, _ in flows] == [A, B, C]

    in_ports = []
    out_ports = []
    for _, f in flows:
        crit = {c["type"]: c.get("port") or c.get("ip") for c in f["selector"]["criteria"]}
        in_ports.append(int(crit["IN_PORT"]))
        out_ports.append(int(f["treatment"]["instructions"][0]["port"]))
        assert crit["IPV4_SRC"] == "10.0.0.1/32"
        assert crit["IPV4_DST"] == "10.0.0.4/32"
        assert f["priority"] == PRIORITY
        assert f["isPermanent"] is True

    assert in_ports == [H1["port"], 3, 4]   # 首跳主机口，其后取上一条边的 dport
    assert out_ports == [2, 5, H4["port"]]  # 中间取 sport，末跳主机口


def test_path_to_flows_rejects_mismatched_host_location():
    res = {"path": [A, B], "cost": 1, "out_ports": [2]}
    with pytest.raises(ValueError):
        flow.path_to_flows(KEY, res, GRAPH, H4, H1, PRIORITY)


def test_install_path_writes_record_and_confirms_each_flow():
    onos = FakeOnos()
    res = path.dijkstra(GRAPH, A, C)
    deployed: dict = {}

    result = asyncio.run(
        flow.install_path(KEY, res, GRAPH, H1, H4, deployed, onos, PRIORITY)
    )

    assert result["ok"] is True
    assert len(result["flow_ids"]) == len(res["path"]) == 3
    rec = deployed[KEY]
    assert rec["status"] == "active"
    assert rec["path"] == [A, B, C]
    assert rec["out_ports"] == [2, 5]
    assert len(rec["flow_ids"]) == 3


def test_install_path_rolls_back_on_midway_failure():
    onos = FakeOnos(fail_on=2)  # 第二条下发失败
    res = path.dijkstra(GRAPH, A, C)
    deployed: dict = {}

    result = asyncio.run(
        flow.install_path(KEY, res, GRAPH, H1, H4, deployed, onos, PRIORITY)
    )

    assert result["ok"] is False
    assert result["error"] == "post_result_unknown"
    # 第一条已下的必须回滚
    assert (A, "f1") in onos.deleted
    # 无法确认的规则要保留为待清理状态，不能悄悄丢掉
    assert deployed[KEY]["status"] == "cleanup_required"
    assert deployed[KEY]["unknown_flows"]


def test_install_path_refuses_duplicate_key():
    onos = FakeOnos()
    res = path.dijkstra(GRAPH, A, C)
    deployed = {KEY: {"path": [A, C], "out_ports": [9], "flow_ids": []}}

    result = asyncio.run(
        flow.install_path(KEY, res, GRAPH, H1, H4, deployed, onos, PRIORITY)
    )

    assert result == {"ok": False, "error": "already_exists", "flow_ids": []}
    assert onos.calls == 0


def test_uninstall_removes_only_its_own_flows():
    onos = FakeOnos()
    res = path.dijkstra(GRAPH, A, C)
    deployed: dict = {}
    asyncio.run(
        flow.install_path(KEY, res, GRAPH, H1, H4, deployed, onos, PRIORITY)
    )

    result = asyncio.run(flow.uninstall(KEY, deployed, onos))

    assert result == {"ok": True, "error": None, "remaining": []}
    assert KEY not in deployed
    assert all(not v for v in onos.flows.values())


def test_affected_hits_only_paths_crossing_removed_edge():
    removed = {"changed": True, "nodes_removed": [], "edges_removed": [[A, B]],
               "edges_added": [], "hosts_removed": [], "hosts_moved": {}}
    deployed = {
        KEY: {"path": [A, B, C]},
        ("10.0.0.2", "10.0.0.3"): {"path": [B, C]},
    }

    assert flow.affected(removed, deployed) == [KEY]


def test_affected_ignores_edge_added_without_shorter_path():
    """链路新增只影响已有部署（是否更短由 recompute 判定），不误伤其他对。"""
    added = {"changed": True, "nodes_removed": [], "edges_removed": [],
             "edges_added": [[A, C]], "hosts_removed": [], "hosts_moved": {}}
    deployed = {KEY: {"path": [A, B, C]}}

    assert flow.affected(added, deployed) == [KEY]


def test_call_signatures_match_api_contract():
    """把 02 §3.6 的签名钉成断言：丙侧传参数量再对不上，这里先红。"""
    args = {
        "install_path": (("10.0.0.1", "10.0.0.4"), {}, {}, {}, {}, {}, None, 40000),
        "uninstall": (("10.0.0.1", "10.0.0.4"), {}, None),
        "recompute": ([("10.0.0.1", "10.0.0.4")], {}, {}, {}, None, 40000),
        "path_to_flows": (("10.0.0.1", "10.0.0.4"), {}, {}, {}, {}, 40000),
        "affected": ({}, {}),
    }
    for name, call_args in args.items():
        inspect.signature(getattr(flow, name)).bind(*call_args)  # 绑不上就抛 TypeError
