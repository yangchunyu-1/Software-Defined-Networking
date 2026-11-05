"""甲 · 感知层契约测试（离线，绝不连 ONOS）。

覆盖 FR-02 / FR-03 / FR-04 / FR-05 与 docs/04 的 A1 / A3 判据。

两类数据来源，互不替代：
  - tests/fixtures/：按 docs/02 §4 手写的样本，恒定可跑，是格式契约。
  - brain/samples/：ONOS 2.7.0 的真响应（pingall 之后落盘），跑 A1 判据；
    文件不存在时相关用例自动 skip，绝不手编冒充实测。

跑法（Windows 侧零第三方依赖）：
    cd brain && python -m unittest discover -s tests -v
跑法（WSL 侧，按 docs/05 第 3 节的三人一致约定）：
    cd brain && python3 -m pytest tests/ -q
pytest 能直接收集 unittest.TestCase，两种跑法断言完全一致。
"""

import copy
import json
import sys
import unittest
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _p in (str(BRAIN), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import topo  # noqa: E402
import fixtures.topo_tree22 as t22  # noqa: E402
import fixtures.topo_chain3 as chain3  # noqa: E402

FIXTURES = TESTS / "fixtures"
SAMPLES = BRAIN / "samples"
_REAL_READY = all(
    (SAMPLES / (name + ".json")).is_file()
    for name in ("devices", "links", "hosts")
)

DIFF_KEYS = {
    "changed",
    "nodes_added",
    "nodes_removed",
    "edges_added",
    "edges_removed",
    "hosts_added",
    "hosts_removed",
    "hosts_moved",
    "ts",
}


def _load(name):
    with (FIXTURES / name).open(encoding="utf-8") as f:
        return json.load(f)


def _load_real(name):
    """读 brain/samples/ 的真响应；没落盘就是没有，不许造。"""
    with (SAMPLES / (name + ".json")).open(encoding="utf-8") as f:
        return json.load(f)


def _snapshot(links, hosts):
    return topo.make_snapshot(
        topo.build_graph(links),
        topo.build_host_table(hosts),
    )


def _tree22_snapshot():
    return _snapshot(t22.LINKS_RESP["links"], t22.HOSTS_RESP["hosts"])


class TestBuildGraph(unittest.TestCase):
    """FR-02：邻接表建图。"""

    def test_matches_contract_literal(self):
        g = topo.build_graph(t22.LINKS_RESP["links"])
        self.assertEqual(g, t22.GRAPH)
        self.assertEqual(len(g), 3)  # tree,2,2 是 3 台交换机

    def test_reverse_edges_have_different_sport(self):
        g = topo.build_graph(t22.LINKS_RESP["links"])
        self.assertNotEqual(g[t22.S1][t22.S2]["sport"], g[t22.S2][t22.S1]["sport"])
        self.assertEqual(g[t22.S1][t22.S2]["sport"], 1)
        self.assertEqual(g[t22.S2][t22.S1]["sport"], 3)

    def test_ports_are_int_not_onos_strings(self):
        g = topo.build_graph(t22.LINKS_RESP["links"])
        for _u, nbrs in g.items():
            for _v, edge in nbrs.items():
                self.assertIsInstance(edge["sport"], int)
                self.assertIsInstance(edge["dport"], int)
                self.assertEqual(edge["weight"], 1)

    def test_only_direct_active_links_enter_graph(self):
        mixed = t22.LINKS_RESP["links"] + t22.INVALID_LINKS
        with self.assertLogs("brain.topo", level="WARNING"):
            g = topo.build_graph(mixed)
        self.assertEqual(g, t22.GRAPH)  # type!=DIRECT / state!=ACTIVE / 非数字端口全被丢掉

    def test_chain3_graph_for_downstream_path_tests(self):
        """给乙的 dijkstra 契约测试铺路：3 跳直线，最短路唯一。"""
        g = topo.build_graph(chain3.LINKS_RESP["links"])
        self.assertEqual(g, chain3.GRAPH)
        self.assertEqual(len(g), 4)


class TestBuildHostTable(unittest.TestCase):
    """FR-03：主机表构建。"""

    def test_matches_contract_literal(self):
        ht = topo.build_host_table(t22.HOSTS_RESP["hosts"])
        self.assertEqual(ht, t22.HOST_TABLE)
        self.assertEqual(len(ht), 4)

    def test_empty_ip_is_skipped_with_warning(self):
        host = copy.deepcopy(t22.HOSTS_RESP["hosts"][0])
        host["ipAddresses"] = []
        with self.assertLogs("brain.topo", level="WARNING") as cm:
            ht = topo.build_host_table([host])
        self.assertEqual(ht, {})
        self.assertIn("ipAddresses", "\n".join(cm.output))

    def test_empty_locations_is_skipped_with_warning(self):
        host = copy.deepcopy(t22.HOSTS_RESP["hosts"][0])
        host["locations"] = []
        with self.assertLogs("brain.topo", level="WARNING") as cm:
            ht = topo.build_host_table([host])
        self.assertEqual(ht, {})
        self.assertIn("locations", "\n".join(cm.output))

    def test_location_port_is_int(self):
        ht = topo.build_host_table(t22.HOSTS_RESP["hosts"])
        self.assertIsInstance(ht["10.0.0.1"]["port"], int)
        self.assertIn("mac", ht["10.0.0.1"])


class TestSnapshot(unittest.TestCase):
    """docs/02 §4.3：快照形状与链路归一化。"""

    def test_empty_snapshot_shape(self):
        s = topo.empty_snapshot()
        self.assertEqual(set(s), {"nodes", "edges", "hosts"})
        self.assertEqual(s["nodes"], set())
        self.assertEqual(s["edges"], set())
        self.assertEqual(s["hosts"], {})

    def test_edges_normalized_to_two_not_four(self):
        """/links 报了 4 条记录，但物理链路只有 2 条。"""
        s = _tree22_snapshot()
        self.assertEqual(len(t22.LINKS_RESP["links"]), 4)
        self.assertEqual(len(s["edges"]), 2)
        for e in s["edges"]:
            self.assertIsInstance(e, frozenset)
            self.assertEqual(len(e), 2)

    def test_hosts_value_includes_port(self):
        s = _tree22_snapshot()
        self.assertEqual(s["hosts"]["10.0.0.1"], (t22.S2, 1))


class TestDiff(unittest.TestCase):
    """FR-04 / FR-05：差异检测与主机迁移。"""

    def test_identical_snapshots_report_no_change(self):
        d = topo.diff(_tree22_snapshot(), _tree22_snapshot())
        self.assertFalse(d["changed"])
        for k in ("nodes_added", "nodes_removed", "edges_added", "edges_removed", "hosts_removed"):
            self.assertEqual(d[k], [])
        for k in ("hosts_added", "hosts_moved"):
            self.assertEqual(d[k], {})

    def test_diff_has_exactly_the_nine_contract_keys(self):
        d = topo.diff(topo.empty_snapshot(), _tree22_snapshot())
        self.assertEqual(set(d), DIFF_KEYS)
        self.assertIsInstance(d["changed"], bool)
        self.assertIsInstance(d["ts"], float)

    def test_first_poll_reports_all_additions(self):
        d = topo.diff(topo.empty_snapshot(), _tree22_snapshot())
        self.assertTrue(d["changed"])
        self.assertEqual(len(d["nodes_added"]), 3)
        self.assertEqual(len(d["edges_added"]), 2)
        self.assertEqual(len(d["hosts_added"]), 4)
        self.assertEqual(d["edges_removed"], [])

    def test_everything_disappears_reports_all_removals(self):
        d = topo.diff(_tree22_snapshot(), topo.empty_snapshot())
        self.assertEqual(len(d["nodes_removed"]), 3)
        self.assertEqual(len(d["edges_removed"]), 2)
        self.assertEqual(d["hosts_removed"], ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"])

    def test_broken_link_yields_exactly_one_removed_edge(self):
        """A3 判据：断 1 条物理链路 -> edges_removed 长度 == 1，不是 2、不是 0。"""
        prev = _tree22_snapshot()
        broken = _load("links_broken.json")["links"]
        cur = _snapshot(broken, t22.HOSTS_RESP["hosts"])
        d = topo.diff(prev, cur)
        self.assertEqual(len(d["edges_removed"]), 1)
        self.assertEqual(d["edges_removed"][0], sorted([t22.S1, t22.S3]))
        self.assertEqual(d["edges_added"], [])
        self.assertTrue(d["changed"])

    def test_link_restored_yields_exactly_one_added_edge(self):
        broken = _load("links_broken.json")["links"]
        prev = _snapshot(broken, t22.HOSTS_RESP["hosts"])
        cur = _tree22_snapshot()
        d = topo.diff(prev, cur)
        self.assertEqual(len(d["edges_added"]), 1)
        self.assertEqual(d["edges_removed"], [])

    def test_host_moved_is_fr05(self):
        """FR-05：同 IP 换了交换机/端口 -> hosts_moved，且不出现在 added/removed。"""
        prev = _tree22_snapshot()
        cur = _snapshot(t22.LINKS_RESP["links"], _load("hosts_moved.json")["hosts"])
        d = topo.diff(prev, cur)
        self.assertEqual(len(d["hosts_moved"]), 1)
        self.assertIn("10.0.0.3", d["hosts_moved"])
        self.assertEqual(d["hosts_moved"]["10.0.0.3"]["from"], [t22.S3, 1])
        self.assertEqual(d["hosts_moved"]["10.0.0.3"]["to"], [t22.S2, 4])
        self.assertNotIn("10.0.0.3", d["hosts_added"])
        self.assertNotIn("10.0.0.3", d["hosts_removed"])
        self.assertEqual(d["hosts_added"], {})
        self.assertEqual(d["hosts_removed"], [])

    def test_port_only_change_also_counts_as_moved(self):
        """FR-04 第 4 条：换了端口也算迁移，只比 dpid 会漏。"""
        hosts = copy.deepcopy(t22.HOSTS_RESP["hosts"])
        hosts[2]["locations"][0]["port"] = "9"
        d = topo.diff(_tree22_snapshot(), _snapshot(t22.LINKS_RESP["links"], hosts))
        self.assertEqual(d["hosts_moved"]["10.0.0.3"], {"from": [t22.S3, 1], "to": [t22.S3, 9]})

    def test_host_added_and_removed(self):
        hosts = copy.deepcopy(t22.HOSTS_RESP["hosts"])
        hosts.pop()  # 10.0.0.4 消失
        extra = copy.deepcopy(hosts[0])
        extra["id"] = "00:00:00:00:00:09/None"
        extra["mac"] = "00:00:00:00:00:09"
        extra["ipAddresses"] = ["10.0.0.9"]
        hosts.append(extra)
        d = topo.diff(_tree22_snapshot(), _snapshot(t22.LINKS_RESP["links"], hosts))
        self.assertEqual(d["hosts_removed"], ["10.0.0.4"])
        self.assertEqual(d["hosts_added"]["10.0.0.9"], {"dpid": t22.S2, "port": 1})
        self.assertEqual(d["hosts_moved"], {})

    def test_diff_is_json_serializable(self):
        """diff 要进 /api/diff 和 WebSocket，frozenset 不能泄漏到出参里。"""
        d = topo.diff(topo.empty_snapshot(), _tree22_snapshot())
        decoded = json.loads(json.dumps(d))
        self.assertEqual(decoded["edges_added"], d["edges_added"])


@unittest.skipUnless(_REAL_READY, "brain/samples/*.json 未落盘（真响应需 ONOS + Mininet）")
class TestRealSamples(unittest.TestCase):
    """docs/04 A1：拿 ONOS 2.7.0 真响应跑一遍，验证手写格式契约没跑偏。

    样本来源：tree,2,2 起好后 pingall（0% dropped），再从
    /onos/v1/{devices,links,hosts} 整份落盘。只允许整份替换，不许手编。
    """

    @classmethod
    def setUpClass(cls):
        cls.devices = _load_real("devices")["devices"]
        cls.links = _load_real("links")["links"]
        cls.hosts = _load_real("hosts")["hosts"]

    def test_a1_graph_node_set_equals_device_set(self):
        g = topo.build_graph(self.links)
        self.assertEqual(len(g), len(self.devices))
        self.assertEqual(set(g), {d["id"] for d in self.devices})

    def test_a1_host_table_equals_host_set(self):
        ht = topo.build_host_table(self.hosts)
        self.assertEqual(len(ht), len(self.hosts))
        for h in self.hosts:
            for ip in h["ipAddresses"]:
                self.assertIn(ip, ht)

    def test_every_host_attached_to_a_known_switch(self):
        g = topo.build_graph(self.links)
        for loc in topo.build_host_table(self.hosts).values():
            self.assertIn(loc["dpid"], g)

    def test_bidirectional_records_normalize_to_half(self):
        snap = topo.make_snapshot(
            topo.build_graph(self.links), topo.build_host_table(self.hosts)
        )
        self.assertEqual(len(snap["edges"]), len(self.links) // 2)
        for e in snap["edges"]:
            self.assertIsInstance(e, frozenset)

    def test_real_snapshot_diff_against_itself_is_no_change(self):
        snap = topo.make_snapshot(
            topo.build_graph(self.links), topo.build_host_table(self.hosts)
        )
        self.assertFalse(topo.diff(snap, copy.deepcopy(snap))["changed"])

    def test_real_response_matches_handwritten_contract(self):
        """真响应解析结果与 fixtures 的手写字面量逐字段一致。"""
        self.assertEqual(topo.build_graph(self.links), t22.GRAPH)
        self.assertEqual(topo.build_host_table(self.hosts), t22.HOST_TABLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
