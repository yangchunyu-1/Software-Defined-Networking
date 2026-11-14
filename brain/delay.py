"""甲 · 感知层：S2 链路时延计算（近似值，非插件级测量）。

设计口径（docs/03 §11.4 / §11.5、docs/04 §6.2 FR-18）：

* 时延表**独立维护**，不直接写 ``graph.weight``；只有 ``apply_delay_weights``
  会在一份拷贝上注入 weight，原图保持 ``docs/02`` §4.1 的 ``sport/dport/weight`` 结构。
* ONOS 北向 REST 不提供真实链路时延（P1 不可得，见 samples/SCHEMA_NOTES.md §5），
  因此走 **P2**：``/statistics/ports`` 累计计数两轮差分 -> ``load_bps`` -> M/M/1 排队近似；
  取不到数据的链路降级到 **P3**：传播时延基线常数 + ``stale`` 标记。
* 平滑采用「窗口中位数 -> EWMA」两级，抗单轮计数毛刺。
* 精度口径固定为**近似值，非插件级测量**，不得当真实时延展示。

本模块顶层只 import 标准库 + ``config`` / ``topo``，不 import ``onos_async``（aiohttp）。
网络层只在 ``DelayMonitor`` 与 ``--once`` 入口里**延迟导入**，因此纯离线单测与
``--offline --demo`` 在没有 aiohttp 的 Windows 侧也能真跑。

字段名以 2026-11-14 落盘的真响应为准，见 ``brain/samples/SCHEMA_NOTES.md``。
新增的两个 REST fetch 属于 ``docs/02`` §3.2 冻结契约，须等对应 PR 生效后再落地；
在此之前 ``--once`` 会如实打印 ``[BLOCKED]``，不编造数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import config

log = logging.getLogger("brain.delay")

__all__ = [
    "PRECISION_NOTE",
    "CounterSampler",
    "DelayTable",
    "DelayMonitor",
    "delay_from_util",
    "normalize_statistics",
    "capacity_from_device_ports",
    "link_keys",
    "set_delay_table",
    "get_delay_table",
    "get_link_metrics",
    "apply_delay_weights",
    "main",
]

PRECISION_NOTE = "近似值，非插件级测量（docs/03 §11.4）"

# 链路键用有向二元组，与 topo.build_graph 的 {u: {v: edge}} 一一对应。
LinkKey = tuple[str, str]
PortKey = tuple[str, int]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _port_no(value: Any) -> int | None:
    """/statistics/ports 的 port 是 int，/devices/{id}/ports 的 port 是 str，
    "local" 之类非数字端口不参与链路建模。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def link_keys(graph: dict) -> set[LinkKey]:
    if not isinstance(graph, dict):
        return set()
    return {(u, v) for u, nbrs in graph.items() for v in nbrs}


def _graph_ports(graph: dict) -> tuple[set[LinkKey], dict[LinkKey, list[PortKey]]]:
    """把一条有向链路映射到它两端的观测端口（src 的 sport、dst 的 dport）。"""
    links: set[LinkKey] = set()
    ports: dict[LinkKey, list[PortKey]] = {}
    for u, nbrs in (graph or {}).items():
        for v, edge in (nbrs or {}).items():
            key = (u, v)
            links.add(key)
            pair: list[PortKey] = []
            sport = _port_no(edge.get("sport"))
            dport = _port_no(edge.get("dport"))
            if sport is not None:
                pair.append((u, sport))
            if dport is not None:
                pair.append((v, dport))
            ports[key] = pair
    return links, ports


def delay_from_util(util: float, cap_bps: float, *,
                    base_prop_ms: float | None = None) -> float:
    """M/M/1 排队近似：传播时延 + 排队时延。

    ``service_ms = 8 * AVG_PKT_BYTES * 1000 / cap_bps`` 是单个包的串行服务时间（ms），
    ``queue_ms = service_ms * u / (1 - u)``。``u`` 被裁到 ``DELAY_UTIL_CAP``，
    避免 u -> 1 时 1/(1-u) 发散；返回值有下限，保证 Dijkstra 权重非负。
    """
    base = config.DELAY_BASE_PROP_MS if base_prop_ms is None else float(base_prop_ms)
    cap = max(float(cap_bps), 1.0)
    u = _clip(float(util), 0.0, config.DELAY_UTIL_CAP)
    service_ms = 8.0 * config.DELAY_AVG_PKT_BYTES * 1000.0 / cap
    queue_ms = service_ms * u / (1.0 - u)
    return max(config.DELAY_MIN_DELAY_MS, base + queue_ms)


def normalize_statistics(payload: Any) -> dict[PortKey, dict]:
    """``GET /statistics/ports`` -> {(device, port): 累计计数}。

    字段名来自真响应（SCHEMA_NOTES §1）：bytes/packets 各有收发两个方向，
    4 个 dropped/errors 键在场且值为 0。累计值，不是速率。
    """
    out: dict[PortKey, dict] = {}
    if not isinstance(payload, dict):
        return out
    for item in payload.get("statistics") or []:
        if not isinstance(item, dict):
            continue
        device = item.get("device")
        if not device:
            continue
        for port in item.get("ports") or []:
            if not isinstance(port, dict):
                continue
            number = _port_no(port.get("port"))
            if number is None:
                continue
            def num(key: str) -> float:
                try:
                    return float(port.get(key, 0) or 0)
                except (TypeError, ValueError):
                    return 0.0
            out[(str(device), number)] = {
                "bytes": num("bytesReceived") + num("bytesSent"),
                "packets": num("packetsReceived") + num("packetsSent"),
                "errors": sum(num(k) for k in (
                    "packetsRxDropped", "packetsTxDropped",
                    "packetsRxErrors", "packetsTxErrors",
                )),
                "durationSec": num("durationSec"),
            }
    return out


def capacity_from_device_ports(device_id: str, payload: Any) -> dict[PortKey, float]:
    """``GET /devices/{id}/ports`` -> {(device, port): bps}。

    顶层是**单个 device 对象**（SCHEMA_NOTES §2），不是 ``{"devices":[...]}``。
    ``portSpeed`` 为 0 或端口不可用时跳过，让调用方回落到默认容量。
    """
    out: dict[PortKey, float] = {}
    if not isinstance(payload, dict):
        return out
    for port in payload.get("ports") or []:
        if not isinstance(port, dict):
            continue
        number = _port_no(port.get("port"))
        try:
            speed = float(port.get("portSpeed", 0) or 0)
        except (TypeError, ValueError):
            speed = 0.0
        if number is None or speed <= 0:
            continue
        out[(str(device_id), number)] = speed * config.DELAY_PORT_SPEED_TO_BPS
    return out


# ---------------------------------------------------------------------------
# 计数差分
# ---------------------------------------------------------------------------

class CounterSampler:
    """把两轮累计计数差分成每条链路的 ``load_bps`` / ``loss``。

    首轮只建基线、不产出；计数回绕（差值为负）或 Δt 小于
    ``DELAY_MIN_INTERVAL_S`` 的本轮直接丢弃，不产出负速率。
    拓扑变化时才重置基线，拓扑不变时重复 ``align`` 不会丢掉历史。
    """

    def __init__(self, graph: dict | None = None, *,
                 min_interval_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.min_interval_s = (
            config.DELAY_MIN_INTERVAL_S if min_interval_s is None else float(min_interval_s)
        )
        self._prev: dict[PortKey, dict] = {}
        self._prev_ts: float | None = None
        self._links: set[LinkKey] = set()
        self._link_ports: dict[LinkKey, list[PortKey]] = {}
        if graph is not None:
            self.align(graph)

    def align(self, graph: dict, *, ts: float | None = None) -> None:
        links, ports = _graph_ports(graph)
        if links == self._links and ports == self._link_ports:
            return
        self._links, self._link_ports = links, ports
        self._prev = {}
        self._prev_ts = None

    def update(self, stats_payload: Any, *, ts: float | None = None) -> dict[LinkKey, dict]:
        now = self._clock() if ts is None else float(ts)
        current = normalize_statistics(stats_payload)
        if not self._prev or self._prev_ts is None:
            self._prev, self._prev_ts = current, now
            return {}

        dt = now - self._prev_ts
        if dt < self.min_interval_s:
            return {}  # 不产出、也不推进基线，等下一次足够长的间隔

        per_port: dict[PortKey, dict] = {}
        for key, cur in current.items():
            prev = self._prev.get(key)
            if prev is None:
                continue
            d_bytes = cur["bytes"] - prev["bytes"]
            d_packets = cur["packets"] - prev["packets"]
            d_errors = cur["errors"] - prev["errors"]
            if d_bytes < 0 or d_packets < 0 or d_errors < 0:
                continue  # 计数回绕或端口重置，跳过该端口
            per_port[key] = {
                "load_bps": 8.0 * d_bytes / dt,
                "loss": _clip(d_errors / max(d_packets, 1.0), 0.0, 1.0),
                "delta_s": dt,
            }
        self._prev, self._prev_ts = current, now

        metrics: dict[LinkKey, dict] = {}
        for link, ports in self._link_ports.items():
            seen = [per_port[p] for p in ports if p in per_port]
            if not seen:
                continue
            # 同一条链路两端都会各自观测到流量，取较大值，避免把一份流量算两遍。
            metrics[link] = {
                "load_bps": max(item["load_bps"] for item in seen),
                "loss": max(item["loss"] for item in seen),
                "delta_s": dt,
            }
        return metrics


# ---------------------------------------------------------------------------
# 时延表
# ---------------------------------------------------------------------------

class DelayTable:
    """独立时延表：窗口中位数 -> EWMA，两级降级（P2 -> P3）与 stale 标记。

    快照值形状（供 ``get_delay_table`` / 前端 / ``apply_delay_weights`` 消费）::

        {(u, v): {"delay_ms", "stale", "source", "util", "load_bps",
                  "loss", "cap_bps", "base_prop_ms", "age_s"}}
    """

    def __init__(self, *, window: int | None = None,
                 ewma_alpha: float | None = None,
                 base_prop_ms: float | None = None,
                 stale_after_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.window = config.DELAY_WINDOW if window is None else int(window)
        self.ewma_alpha = config.DELAY_EWMA_ALPHA if ewma_alpha is None else float(ewma_alpha)
        self.base_prop_ms = (
            config.DELAY_BASE_PROP_MS if base_prop_ms is None else float(base_prop_ms)
        )
        self.stale_after_s = (
            config.DELAY_STALE_AFTER_S if stale_after_s is None else float(stale_after_s)
        )
        self._raw: dict[LinkKey, deque] = {}
        self._smooth: dict[LinkKey, float] = {}
        self._aux: dict[LinkKey, dict] = {}
        self._links: set[LinkKey] = set()
        self._retired: set[LinkKey] = set()
        self._link_ports: dict[LinkKey, list[PortKey]] = {}

    def align(self, graph: dict, *, ts: float | None = None) -> None:
        """拓扑变化时把消失的链路移入 ``_retired``（判据 2：断链标失效）。

        链路恢复时从 ``_retired`` 移除；重复 ``align`` 同一张图不产生副作用。
        """
        links, ports = _graph_ports(graph)
        if links == self._links and ports == self._link_ports:
            return
        self._retired |= (self._links - links)
        self._retired -= links
        self._links, self._link_ports = links, ports

    def _capacity_for(self, link: LinkKey, capacity: dict | None,
                      caps_by_port: dict | None) -> float:
        if capacity and link in capacity:
            try:
                return max(float(capacity[link]), 1.0)
            except (TypeError, ValueError):
                pass
        if caps_by_port:
            values = [caps_by_port[p] for p in self._link_ports.get(link, [])
                      if p in caps_by_port]
            if values:
                # 一条链路的有效容量由较慢的一端决定。
                return max(min(float(v) for v in values), 1.0)
        return float(config.DELAY_DEFAULT_CAP_BPS)

    def update(self, metrics: dict | None, *, capacity: dict | None = None,
               caps_by_port: dict | None = None, graph: dict | None = None,
               ts: float | None = None,
               base_prop_by_link: dict | None = None) -> None:
        """写入一轮 CounterSampler 的原始测量（未平滑）。

        ``base_prop_by_link`` 留给 P1：若将来 ONOS /links 自带时延，按链路覆盖基线；
        当前真样本不含 latency/annotations，默认走 ``config.DELAY_BASE_PROP_MS``。
        """
        now = self._clock() if ts is None else float(ts)
        if graph is not None:
            self.align(graph, ts=now)
        metrics = metrics or {}

        for link in sorted(self._links):
            cap = self._capacity_for(link, capacity, caps_by_port)
            base = self.base_prop_ms
            if base_prop_by_link and link in base_prop_by_link:
                base = float(base_prop_by_link[link])

            measurement = metrics.get(link)
            if measurement is None:
                # P3：本轮没有该链路的数据，保留上次平滑值并标失效。
                self._aux[link] = {
                    "source": "P3", "util": 0.0, "load_bps": 0.0, "loss": 0.0,
                    "cap_bps": cap, "base_prop_ms": base,
                    "ts": self._aux.get(link, {}).get("ts", now),
                }
                continue

            load = max(float(measurement.get("load_bps", 0.0)), 0.0)
            util = _clip(load / cap, 0.0, config.DELAY_UTIL_CAP)
            raw = delay_from_util(util, cap, base_prop_ms=base)
            window = self._raw.setdefault(link, deque(maxlen=self.window))
            window.append(raw)
            med = float(statistics.median(window))
            prev = self._smooth.get(link)
            self._smooth[link] = (
                med if prev is None
                else self.ewma_alpha * med + (1.0 - self.ewma_alpha) * prev
            )
            self._aux[link] = {
                "source": "P2", "util": util, "load_bps": load,
                "loss": _clip(float(measurement.get("loss", 0.0)), 0.0, 1.0),
                "cap_bps": cap, "base_prop_ms": base, "ts": now,
            }

    def snapshot(self) -> dict[LinkKey, dict]:
        now = self._clock()
        out: dict[LinkKey, dict] = {}
        for link in sorted(self._links | self._retired):
            aux = self._aux.get(link, {})
            ts = aux.get("ts", now)
            age = max(now - ts, 0.0)
            retired = link in self._retired
            stale = retired or aux.get("source") == "P3" or age > self.stale_after_s
            out[link] = {
                "delay_ms": float(self._smooth.get(link, self.base_prop_ms)),
                "stale": bool(stale),
                "source": "P3" if retired else aux.get("source", "P3"),
                "util": float(aux.get("util", 0.0)),
                "load_bps": float(aux.get("load_bps", 0.0)),
                "loss": float(aux.get("loss", 0.0)),
                "cap_bps": float(aux.get("cap_bps", config.DELAY_DEFAULT_CAP_BPS)),
                "base_prop_ms": float(aux.get("base_prop_ms", self.base_prop_ms)),
                "age_s": float(age),
            }
        return out


# ---------------------------------------------------------------------------
# 对外函数（v4 §3 阶段 C 的 B1 优先级契约）
# ---------------------------------------------------------------------------

_TABLE = DelayTable()


def set_delay_table(table: DelayTable | None) -> None:
    """注入模块级时延表（main.py 或测试用）；传 None 恢复默认空表。"""
    global _TABLE
    _TABLE = table if table is not None else DelayTable()


def get_delay_table() -> dict[LinkKey, dict]:
    return _TABLE.snapshot()


def get_link_metrics(link: LinkKey | None = None) -> dict | None:
    snapshot = _TABLE.snapshot()
    if link is None:
        return snapshot
    return snapshot.get(link)


def _entry_for(delay_table: dict, link: LinkKey) -> dict | None:
    """兼容 tuple 键与 ``"u->v"`` 字符串键（后者便于 JSON / 前端）。"""
    if not delay_table:
        return None
    if link in delay_table:
        return delay_table[link]
    return delay_table.get(f"{link[0]}->{link[1]}")


def apply_delay_weights(graph: dict, delay_table: dict | None = None, *,
                        mode: str | None = None, copy: bool = True) -> dict:
    """在一份拷贝上把时延注入 ``graph[u][v]["weight"]``，原图不被污染。

    B1 优先级契约（v4 §3 阶段 C）：
    * **显式传 ``delay_table``** = 调用方明确要求注入，优先于全局 ``WEIGHT_MODE``；
    * 不传表时读 ``config.WEIGHT_MODE``；``"hop"`` 不改任何 weight；
    * 只有 ``"delay"``（或显式传表）才注入 ``1 + delay_ms / DELAY_REF_MS``；
    * ``stale`` 的条目不注入，保留原 weight，避免把失效值喂给 Dijkstra。
    """
    if delay_table is None:
        active_mode = config.WEIGHT_MODE if mode is None else mode
        if active_mode != "delay":
            if not copy:
                return graph
            return {u: dict(nbrs) for u, nbrs in graph.items()}
        delay_table = get_delay_table()

    out = {u: {v: dict(edge) for v, edge in nbrs.items()}
           for u, nbrs in graph.items()}
    if not copy:
        # copy=False 只用于调用方明确允许原地改的场景；本函数默认不动原图。
        for u, nbrs in graph.items():
            for v, edge in nbrs.items():
                edge.update(out[u][v])
        return graph

    for u, nbrs in out.items():
        for v, edge in nbrs.items():
            entry = _entry_for(delay_table, (u, v))
            if not entry or entry.get("stale"):
                continue
            delay_ms = max(float(entry.get("delay_ms", 0.0)), config.DELAY_MIN_DELAY_MS)
            edge["weight"] = 1.0 + delay_ms / config.DELAY_REF_MS
    return out


# ---------------------------------------------------------------------------
# 轮询监控（网络层延迟导入，异常不外抛）
# ---------------------------------------------------------------------------

class DelayMonitor:
    """把 fetch -> 差分 -> 平滑串起来的一轮 tick。

    fetcher 用依赖注入，便于离线单测；不传时从 ``onos_async`` 延迟导入。
    任何异常都记 ``failures`` 并返回空 dict，不让 S2 把主轮询循环拖崩
    （与 ``main.py`` 的 ``poll_loop`` 接 ``OnosError`` 不 break 的口径一致）。
    """

    def __init__(self, *, fetch_stats=None, fetch_links=None, fetch_ports=None,
                 table: DelayTable | None = None, sampler: CounterSampler | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.fetch_stats = fetch_stats
        self.fetch_links = fetch_links
        self.fetch_ports = fetch_ports
        self.table = table if table is not None else DelayTable()
        self.sampler = sampler
        self._clock = clock
        self.failures = 0
        self._graph: dict = {}

    @staticmethod
    def _missing_network_fetchers() -> list[str]:
        try:
            import onos_async  # noqa: F401  （延迟导入：Windows 侧无 aiohttp 时不触发）
        except Exception as exc:
            raise RuntimeError(f"onos_async 不可导入：{type(exc).__name__}: {exc}") from exc
        needed = ("fetch_port_statistics", "fetch_device_ports", "fetch_links")
        return [name for name in needed if not hasattr(onos_async, name)]

    async def _resolve(self):
        missing = []
        if self.fetch_stats is None or self.fetch_links is None:
            missing = self._missing_network_fetchers()
            if missing:
                raise RuntimeError(
                    "docs/02 §3.2 契约 PR 未生效，onos_async 缺少 " + ", ".join(missing)
                )
        stats_f = self.fetch_stats or self._lazy("fetch_port_statistics")
        links_f = self.fetch_links or self._lazy("fetch_links")
        return stats_f, links_f

    @staticmethod
    def _lazy(name: str):
        import onos_async
        return getattr(onos_async, name)

    async def tick(self) -> dict[LinkKey, dict]:
        now = self._clock()
        try:
            stats_f, links_f = await self._resolve()
            links = await links_f()
            import topo
            graph = topo.build_graph(links)
            self._graph = graph
            if self.sampler is None:
                self.sampler = CounterSampler(graph=graph, clock=self._clock)
            self.sampler.align(graph)
            self.table.align(graph, ts=now)

            caps_by_port: dict[PortKey, float] = {}
            if self.fetch_ports is not None:
                for device_id in graph:
                    try:
                        payload = await self.fetch_ports(device_id)
                        caps_by_port.update(
                            capacity_from_device_ports(device_id, payload)
                        )
                    except Exception as exc:
                        log.warning("fetch_ports failed on %s: %s", device_id, exc)

            stats = await stats_f()
            metrics = self.sampler.update(stats, ts=now)
            self.table.update(metrics, caps_by_port=caps_by_port, graph=graph, ts=now)
            return self.table.snapshot()
        except Exception as exc:
            self.failures += 1
            log.warning("delay tick failed: %s: %s", type(exc).__name__, exc)
            return {}


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------

def _print_table(snapshot: dict, title: str) -> None:
    print(f"--- {title} ---")
    if not snapshot:
        print("  (empty: 首轮只建基线)")
        return
    for (u, v), item in snapshot.items():
        print(
            f"  {u[-2:]}->{v[-2:]}  delay_ms={item['delay_ms']:.4f}  "
            f"weight={1.0 + item['delay_ms'] / config.DELAY_REF_MS:.4f}  "
            f"util={item['util']:.4f}  source={item['source']}  "
            f"stale={item['stale']}"
        )


def _synthetic_stats(devices: dict[str, list[int]], step: int) -> dict:
    """离线演示专用：按真响应形状造累计计数。step 越大计数越大。"""
    statistics_list = []
    for device, ports in devices.items():
        records = []
        for index, port in enumerate(ports):
            # 不同端口给不同增量，保证链路间 delay_ms 出现差异（仅演示，不作精度证据）。
            growth = step * (1 + index) * 1_000_000
            records.append({
                "port": port,
                "packetsReceived": 10 + step * (1 + index),
                "packetsSent": 10 + step * (1 + index),
                "bytesReceived": growth // 2,
                "bytesSent": growth // 2,
                "packetsRxDropped": 0, "packetsTxDropped": 0,
                "packetsRxErrors": 0, "packetsTxErrors": 0,
                "durationSec": 100 + step * 5,
            })
        statistics_list.append({"device": device, "ports": records})
    return {"statistics": statistics_list}


def _offline_demo() -> int:
    print("[OFFLINE/SYNTHETIC] 拓扑取自真响应 samples/links.json；端口计数为合成数据，")
    print("[OFFLINE/SYNTHETIC] 仅用于验证算法流程，不能作为时延精度证据。")
    print(f"[PRECISION] {PRECISION_NOTE}")
    import topo
    sample = Path(__file__).parent / "samples" / "links.json"
    links = json.loads(sample.read_text(encoding="utf-8"))["links"]
    graph = topo.build_graph(links)
    print(f"graph: {len(graph)} switches, {len(link_keys(graph))} directed links")

    ports_by_device: dict[str, list[int]] = {}
    for u, nbrs in graph.items():
        for v, edge in nbrs.items():
            ports_by_device.setdefault(u, [])
            ports_by_device.setdefault(v, [])
            if edge["sport"] not in ports_by_device[u]:
                ports_by_device[u].append(edge["sport"])
            if edge["dport"] not in ports_by_device[v]:
                ports_by_device[v].append(edge["dport"])

    class Clock:
        def __init__(self): self.t = 0.0
        def __call__(self): return self.t
    clock = Clock()
    sampler = CounterSampler(graph=graph, min_interval_s=config.DELAY_MIN_INTERVAL_S,
                             clock=clock)
    table = DelayTable(clock=clock)
    table.align(graph, ts=clock())

    sampler.update(_synthetic_stats(ports_by_device, 0), ts=clock())  # 首轮基线
    clock.t += config.DELAY_INTERVAL_S
    # 无流量轮是「有效测量且负载为 0」，不是「数据缺失」：source 应为 P2、不标 stale。
    zero_metrics = {link: {"load_bps": 0.0, "loss": 0.0} for link in link_keys(graph)}
    table.update(zero_metrics, graph=graph, ts=clock())
    _print_table(table.snapshot(), "no traffic (baseline)")

    for step in range(1, 5):
        clock.t += config.DELAY_INTERVAL_S
        metrics = sampler.update(_synthetic_stats(ports_by_device, step), ts=clock())
        table.update(metrics, graph=graph, ts=clock())
    snapshot = table.snapshot()
    _print_table(snapshot, "after 4 rounds of synthetic load")

    weighted = apply_delay_weights(graph, snapshot)
    print("--- apply_delay_weights (copy) ---")
    for u, nbrs in weighted.items():
        for v, edge in nbrs.items():
            print(f"  {u[-2:]}->{v[-2:]}  weight={edge['weight']:.4f}  "
                  f"keys={sorted(edge)}")
    print("original graph untouched:",
          all(edge["weight"] == 1 for nbrs in graph.values() for edge in nbrs.values()))
    print("dijkstra ready:", all(
        edge["weight"] >= 0 for nbrs in weighted.values() for edge in nbrs.values()
    ))
    return 0


async def _once() -> int:
    """真 ONOS 一轮采样（两轮差分）。fetch 未落地时如实标 BLOCKED。"""
    missing = []
    try:
        missing = DelayMonitor._missing_network_fetchers()
    except RuntimeError as exc:
        print(f"[BLOCKED] {exc}")
        return 0
    if missing:
        print("[BLOCKED] onos_async 缺少 " + ", ".join(missing))
        print("[BLOCKED] 原因：docs/02 §3.2 契约 PR 尚未生效；"
              "本轮不采集、不编造任何时延数值。")
        return 0

    import onos_async
    monitor = DelayMonitor(
        fetch_stats=onos_async.fetch_port_statistics,
        fetch_links=onos_async.fetch_links,
    )
    first = await monitor.tick()
    print(f"[ONOS] 首轮建基线：{len(first)} links, failures={monitor.failures}")
    await asyncio.sleep(config.DELAY_MIN_INTERVAL_S)
    second = await monitor.tick()
    print(f"[PRECISION] {PRECISION_NOTE}")
    _print_table(second, "real ONOS /statistics/ports (two-round diff)")
    print(f"[ONOS] failures={monitor.failures}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S2 链路时延计算")
    parser.add_argument("--once", action="store_true", help="真 ONOS 两轮差分采样一次")
    parser.add_argument("--offline", action="store_true", help="离线模式")
    parser.add_argument("--demo", action="store_true", help="离线合成演示")
    args = parser.parse_args(argv)
    if args.once:
        return asyncio.run(_once())
    if args.offline or args.demo:
        return _offline_demo()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
