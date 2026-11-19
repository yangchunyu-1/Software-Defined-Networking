"""S2 链路时延模型 · 纯离线契约测试（unittest.TestCase，pytest 可直接收集）。

约束（docs/05 §3）：
* 不连 ONOS、不依赖执行顺序、不共享全局状态；
* 计数类样本**内联在本测试代码里**，不新增 tests/fixtures/ 文件——
  `/statistics/ports` 的 10 个字段随 docs/02 §2 / §3.2 的契约 PR 一并生效。

运行方式：
    cd brain && python -m unittest tests.test_delay_model -v
    cd brain && python3 -m pytest tests/test_delay_model.py -q   # WSL 侧
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import sys
import unittest
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(BRAIN))
sys.path.insert(0, str(TESTS))

import config  # noqa: E402
import delay  # noqa: E402
import path  # noqa: E402
import topo  # noqa: E402

D1 = "of:0000000000000001"
D2 = "of:0000000000000002"
D3 = "of:0000000000000003"
CAP_10M = 10_000_000.0


class Clock:
    """可控时间源，避免测试真睡 5 秒。"""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _links():
    """与 brain/samples/links.json 同形的 4 条有向边（真响应形状）。"""
    return [
        {"src": {"device": D1, "port": "1"}, "dst": {"device": D2, "port": "3"},
         "type": "DIRECT", "state": "ACTIVE"},
        {"src": {"device": D3, "port": "3"}, "dst": {"device": D1, "port": "2"},
         "type": "DIRECT", "state": "ACTIVE"},
        {"src": {"device": D1, "port": "2"}, "dst": {"device": D3, "port": "3"},
         "type": "DIRECT", "state": "ACTIVE"},
        {"src": {"device": D2, "port": "3"}, "dst": {"device": D1, "port": "1"},
         "type": "DIRECT", "state": "ACTIVE"},
    ]


def _graph():
    return topo.build_graph(_links())


def _ports_by_device(graph):
    out = {}
    for u, nbrs in graph.items():
        for v, edge in nbrs.items():
            out.setdefault(u, set()).add(edge["sport"])
            out.setdefault(v, set()).add(edge["dport"])
    return {k: sorted(v) for k, v in out.items()}


def _stats(totals: dict, *, duration=100):
    """/statistics/ports 真响应形状：每端口 10 键，0 值键在场不省略。"""
    by_device = {}
    for (device, port), total in totals.items():
        by_device.setdefault(device, []).append({
            "port": port,
            "packetsReceived": 10, "packetsSent": 10,
            "bytesReceived": total // 2, "bytesSent": total - total // 2,
            "packetsRxDropped": 0, "packetsTxDropped": 0,
            "packetsRxErrors": 0, "packetsTxErrors": 0,
            "durationSec": duration,
        })
    return {"statistics": [{"device": d, "ports": ps} for d, ps in by_device.items()]}


def _totals(graph, factor=0):
    """按端口序号给不同累计字节数，造出链路间负载差异（合成，非真流量）。"""
    ports = _ports_by_device(graph)
    return {(device, port): factor * (1 + index) * 1_000_000
            for device, numbers in ports.items()
            for index, port in enumerate(numbers)}


def _real_shaped_stats():
    """3 devices / 8 port records，10 键全在场（与落盘真响应同形）。"""
    layout = {D1: [1, 2], D2: [1, 2, 3], D3: [1, 2, 3]}
    totals = {(device, port): 1000 * (port + 1)
              for device, ports in layout.items() for port in ports}
    return _stats(totals)


class TestPrecisionAndFormula(unittest.TestCase):
    def test_01_precision_note_is_explicit(self):
        # 判据 6：答辩口径为「近似、非插件级」，不得冒充真实测量。
        self.assertIn("近似", delay.PRECISION_NOTE)
        self.assertIn("非插件级", delay.PRECISION_NOTE)
        self.assertIn("近似", delay.__doc__)

    def test_02_zero_util_equals_base_prop(self):
        # 判据 3：无流量时全等 base_prop_ms 属正常行为。
        self.assertAlmostEqual(
            delay.delay_from_util(0.0, CAP_10M), config.DELAY_BASE_PROP_MS, places=9
        )

    def test_03_delay_grows_with_util(self):
        values = [delay.delay_from_util(u, CAP_10M) for u in (0.0, 0.2, 0.5, 0.8)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[-1], values[0])

    def test_04_util_cap_never_diverges(self):
        for util in (0.99, 1.5, 100.0):
            value = delay.delay_from_util(util, CAP_10M)
            self.assertTrue(0 < value < 100, f"util={util} -> {value}")
            self.assertFalse(value != value or value == float("inf"))

    def test_05_capacity_and_base_are_honored(self):
        slow = delay.delay_from_util(0.5, 1_000_000.0)
        fast = delay.delay_from_util(0.5, 100_000_000.0)
        self.assertGreater(slow, fast)  # 容量越小，排队越久
        self.assertAlmostEqual(
            delay.delay_from_util(0.0, CAP_10M, base_prop_ms=7.0), 7.0, places=9
        )


class TestResponseParsing(unittest.TestCase):
    def test_06_normalize_statistics_real_shape(self):
        payload = _real_shaped_stats()
        parsed = delay.normalize_statistics(payload)
        self.assertEqual(len(parsed), 8)  # 3 devices / 8 port records
        for key, item in parsed.items():
            self.assertIsInstance(key[1], int)
            self.assertEqual(set(item), {"bytes", "packets", "errors", "durationSec"})
            self.assertGreaterEqual(item["bytes"], 0)
            self.assertEqual(item["errors"], 0)

    def test_07_non_numeric_port_is_skipped(self):
        payload = {"statistics": [{"device": D1, "ports": [
            {"port": "local", "bytesReceived": 1, "bytesSent": 1},
            {"port": "1", "bytesReceived": 10, "bytesSent": 10},
        ]}]}
        parsed = delay.normalize_statistics(payload)
        self.assertEqual(list(parsed), [(D1, 1)])
        self.assertEqual(delay.normalize_statistics(None), {})
        self.assertEqual(delay.normalize_statistics({"statistics": None}), {})

    def test_08_device_ports_single_object_and_speed(self):
        payload = {"id": D1, "ports": [
            {"port": "local", "portSpeed": 0, "isEnabled": False},
            {"port": "1", "portSpeed": 10000, "isEnabled": True},
            {"port": "2", "portSpeed": 10000, "isEnabled": True},
        ]}
        caps = delay.capacity_from_device_ports(D1, payload)
        self.assertEqual(set(caps), {(D1, 1), (D1, 2)})
        self.assertEqual(caps[(D1, 1)], 10000 * config.DELAY_PORT_SPEED_TO_BPS)
        self.assertTrue(all(v > 0 for v in caps.values()))
        self.assertEqual(delay.capacity_from_device_ports(D1, {"devices": []}), {})


class TestCounterSampler(unittest.TestCase):
    def test_09_first_round_builds_baseline_only(self):
        graph = _graph()
        clock = Clock()
        sampler = delay.CounterSampler(graph=graph, min_interval_s=0.0, clock=clock)
        self.assertEqual(sampler.update(_stats(_totals(graph, 1)), ts=clock()), {})

    def test_10_second_round_produces_nonnegative_metrics(self):
        graph = _graph()
        clock = Clock()
        sampler = delay.CounterSampler(graph=graph, min_interval_s=0.0, clock=clock)
        sampler.update(_stats(_totals(graph, 1)), ts=clock())
        clock.t += 5.0
        result = sampler.update(_stats(_totals(graph, 3)), ts=clock())
        self.assertEqual(len(result), 4)
        for metrics in result.values():
            self.assertGreater(metrics["load_bps"], 0)
            self.assertGreaterEqual(metrics["loss"], 0)
            self.assertEqual(metrics["delta_s"], 5.0)
        self.assertGreater(len({round(m["load_bps"]) for m in result.values()}), 1)

    def test_11_min_interval_discards_round(self):
        graph = _graph()
        clock = Clock()
        sampler = delay.CounterSampler(graph=graph, min_interval_s=2.0, clock=clock)
        sampler.update(_stats(_totals(graph, 1)), ts=clock())
        clock.t += 0.5
        self.assertEqual(sampler.update(_stats(_totals(graph, 2)), ts=clock()), {})
        clock.t += 5.0
        self.assertTrue(sampler.update(_stats(_totals(graph, 3)), ts=clock()))

    def test_12_counter_rollover_is_discarded(self):
        graph = _graph()
        clock = Clock()
        sampler = delay.CounterSampler(graph=graph, min_interval_s=0.0, clock=clock)
        sampler.update(_stats(_totals(graph, 5)), ts=clock())
        clock.t += 5.0
        result = sampler.update(_stats(_totals(graph, 1)), ts=clock())
        # 计数减小：该轮不产出负速率，但基线仍推进。
        self.assertFalse(any(m["load_bps"] < 0 for m in result.values()))
        clock.t += 5.0
        self.assertTrue(sampler.update(_stats(_totals(graph, 8)), ts=clock()))

    def test_13_align_same_graph_keeps_baseline(self):
        graph = _graph()
        clock = Clock()
        sampler = delay.CounterSampler(graph=graph, min_interval_s=0.0, clock=clock)
        sampler.update(_stats(_totals(graph, 1)), ts=clock())
        sampler.align(graph)  # 拓扑没变，不应重置差分基线
        clock.t += 5.0
        self.assertEqual(len(sampler.update(_stats(_totals(graph, 2)), ts=clock())), 4)


class TestDelayTable(unittest.TestCase):
    def test_14_no_traffic_all_links_equal_base(self):
        graph = _graph()
        clock = Clock()
        table = delay.DelayTable(clock=clock)
        zero = {k: {"load_bps": 0.0, "loss": 0.0} for k in delay.link_keys(graph)}
        table.update(zero, graph=graph, ts=clock())
        snap = table.snapshot()
        self.assertEqual(len(snap), 4)
        for item in snap.values():
            self.assertAlmostEqual(item["delay_ms"], config.DELAY_BASE_PROP_MS, places=9)
            self.assertEqual(item["source"], "P2")
            self.assertFalse(item["stale"])

    def test_15_median_window_rejects_single_spike(self):
        graph = _graph()
        link = (D1, D2)
        clock = Clock()
        table = delay.DelayTable(window=3, ewma_alpha=1.0, clock=clock)
        table.align(graph, ts=clock())
        for load in (0.0, 0.0, 0.0):
            table.update({link: {"load_bps": load, "loss": 0.0}},
                         capacity={link: CAP_10M}, ts=clock())
        spike = CAP_10M * config.DELAY_UTIL_CAP
        table.update({link: {"load_bps": spike, "loss": 0.0}},
                     capacity={link: CAP_10M}, ts=clock())
        # 3 个 0 与 1 个尖峰：窗口中位数仍是基线附近。
        self.assertLess(table.snapshot()[link]["delay_ms"], 1.0)

    def test_16_ewma_smooths_step_change(self):
        graph = _graph()
        link = (D1, D2)
        clock = Clock()
        table = delay.DelayTable(window=1, ewma_alpha=0.3, clock=clock)
        table.align(graph, ts=clock())
        raw_low = delay.delay_from_util(0.0, CAP_10M)
        raw_high = delay.delay_from_util(0.8, CAP_10M)
        table.update({link: {"load_bps": 0.0, "loss": 0.0}},
                     capacity={link: CAP_10M}, ts=clock())
        table.update({link: {"load_bps": CAP_10M * 0.8, "loss": 0.0}},
                     capacity={link: CAP_10M}, ts=clock())
        smoothed = table.snapshot()[link]["delay_ms"]
        self.assertGreater(smoothed, raw_low)
        self.assertLess(smoothed, raw_high)

    def test_17_missing_measurement_downgrades_to_p3(self):
        graph = _graph()
        clock = Clock()
        table = delay.DelayTable(clock=clock)
        table.update({(D1, D2): {"load_bps": 1_000_000.0, "loss": 0.0}},
                     graph=graph, ts=clock())
        before = table.snapshot()[(D1, D2)]["delay_ms"]
        table.update({}, graph=graph, ts=clock())
        after = table.snapshot()[(D1, D2)]
        self.assertEqual(after["source"], "P3")
        self.assertTrue(after["stale"])
        self.assertAlmostEqual(after["delay_ms"], before, places=9)

    def test_18_stale_by_age(self):
        graph = _graph()
        clock = Clock()
        table = delay.DelayTable(stale_after_s=15.0, clock=clock)
        table.update({(D1, D2): {"load_bps": 0.0, "loss": 0.0}},
                     graph=graph, ts=clock())
        self.assertFalse(table.snapshot()[(D1, D2)]["stale"])
        clock.t += 100.0
        self.assertTrue(table.snapshot()[(D1, D2)]["stale"])

    def test_19_broken_link_is_marked_stale(self):
        # 判据 2：断链后相应链路变化或标失效。
        graph = _graph()
        clock = Clock()
        table = delay.DelayTable(clock=clock)
        table.update({k: {"load_bps": 1_000_000.0, "loss": 0.0}
                      for k in delay.link_keys(graph)}, graph=graph, ts=clock())
        broken = copy.deepcopy(graph)
        del broken[D1][D2]
        table.align(broken, ts=clock())
        snap = table.snapshot()
        self.assertIn((D1, D2), snap)
        self.assertTrue(snap[(D1, D2)]["stale"])
        self.assertEqual(snap[(D1, D2)]["source"], "P3")
        self.assertFalse(snap[(D1, D3)]["stale"])

    def test_20_capacity_by_port_uses_slower_end(self):
        graph = _graph()
        link = (D1, D2)
        clock = Clock()
        table = delay.DelayTable(clock=clock)
        table.align(graph, ts=clock())
        caps = {(D1, 1): 1_000_000.0, (D2, 3): 10_000_000.0}
        table.update({link: {"load_bps": 500_000.0, "loss": 0.0}},
                     caps_by_port=caps, ts=clock())
        self.assertEqual(table.snapshot()[link]["cap_bps"], 1_000_000.0)


class TestApplyDelayWeights(unittest.TestCase):
    def setUp(self):
        self._saved_mode = config.WEIGHT_MODE
        self._saved_table = delay._TABLE

    def tearDown(self):
        config.WEIGHT_MODE = self._saved_mode
        delay._TABLE = self._saved_table

    def _snapshot(self, graph, value=0.5, stale=False):
        return {k: {"delay_ms": value, "stale": stale, "source": "P2"}
                for k in delay.link_keys(graph)}

    def test_21_hop_mode_does_not_change_weight(self):
        # 判据 4 前半：WEIGHT_MODE="hop" 不改 weight。
        graph = _graph()
        config.WEIGHT_MODE = "hop"
        out = delay.apply_delay_weights(graph)
        for nbrs in out.values():
            for edge in nbrs.values():
                self.assertEqual(edge["weight"], 1)
                self.assertEqual(set(edge), {"sport", "dport", "weight"})

    def test_22_explicit_table_overrides_hop(self):
        # 判据 4 后半 / B1 优先级：显式传表优先于全局 mode。
        graph = _graph()
        config.WEIGHT_MODE = "hop"
        out = delay.apply_delay_weights(graph, self._snapshot(graph, value=0.5))
        for edge in (e for nbrs in out.values() for e in nbrs.values()):
            self.assertAlmostEqual(edge["weight"], 1.5, places=9)
        self.assertTrue(all(e["weight"] == 1
                            for nbrs in graph.values() for e in nbrs.values()))

    def test_23_delay_mode_uses_module_table(self):
        graph = _graph()
        table = delay.DelayTable(clock=Clock())
        table.update({k: {"load_bps": 0.0, "loss": 0.0}
                      for k in delay.link_keys(graph)}, graph=graph, ts=0.0)
        delay.set_delay_table(table)
        config.WEIGHT_MODE = "delay"
        out = delay.apply_delay_weights(graph)
        expected = 1.0 + config.DELAY_BASE_PROP_MS / config.DELAY_REF_MS
        for edge in (e for nbrs in out.values() for e in nbrs.values()):
            self.assertAlmostEqual(edge["weight"], expected, places=9)

    def test_24_stale_entry_is_not_injected(self):
        graph = _graph()
        out = delay.apply_delay_weights(graph, self._snapshot(graph, stale=True))
        self.assertTrue(all(e["weight"] == 1
                            for nbrs in out.values() for e in nbrs.values()))

    def test_25_string_keys_are_accepted(self):
        graph = _graph()
        table = {f"{u}->{v}": {"delay_ms": 0.25, "stale": False}
                 for u, v in delay.link_keys(graph)}
        out = delay.apply_delay_weights(graph, table)
        self.assertTrue(all(abs(e["weight"] - 1.25) < 1e-9
                            for nbrs in out.values() for e in nbrs.values()))

    def test_26_dijkstra_accepts_weighted_graph(self):
        # 注入后的图仍满足 docs/02 §4.1 结构，乙的 dijkstra 能直接消费。
        graph = _graph()
        weighted = delay.apply_delay_weights(graph, self._snapshot(graph, value=0.5))
        result = path.dijkstra(weighted, D1, D2)
        self.assertIsNotNone(result)
        self.assertEqual(len(result["out_ports"]), len(result["path"]) - 1)
        self.assertGreaterEqual(result["cost"], 1.0)


class TestPipelineAndCLI(unittest.TestCase):
    def test_27_full_pipeline_with_fake_fetchers(self):
        graph = _graph()
        clock = Clock()
        counter = [0]

        async def stats():
            counter[0] += 1
            return _stats(_totals(graph, factor=counter[0] * 2))

        async def links():
            return _links()

        table = delay.DelayTable(clock=clock)
        sampler = delay.CounterSampler(graph=graph, min_interval_s=2.0, clock=clock)
        monitor = delay.DelayMonitor(fetch_stats=stats, fetch_links=links,
                                     table=table, sampler=sampler, clock=clock)
        first = asyncio.run(monitor.tick())  # 首轮建基线
        clock.t += 5.0
        second = asyncio.run(monitor.tick())
        self.assertTrue(first)  # 首轮快照有 4 条链路（均为 P3/stale）
        self.assertEqual(len(second), 4)
        self.assertEqual(monitor.failures, 0)
        self.assertFalse(any(item["stale"] for item in second.values()))
        self.assertEqual({item["source"] for item in second.values()}, {"P2"})

    def test_28_fetcher_failure_does_not_raise(self):
        async def broken():
            raise RuntimeError("boom")

        monitor = delay.DelayMonitor(fetch_stats=broken, fetch_links=broken,
                                     clock=Clock())
        with self.assertLogs("brain.delay", level="WARNING"):
            self.assertEqual(asyncio.run(monitor.tick()), {})
        self.assertEqual(monitor.failures, 1)

    def test_29_offline_demo_is_self_contained(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = delay.main(["--offline", "--demo"])
        output = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("[OFFLINE/SYNTHETIC]", output)
        self.assertIn("original graph untouched: True", output)
        self.assertIn("after 4 rounds of synthetic load", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
