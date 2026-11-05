"""甲 · 感知层：拓扑建模、主机表、快照与两轮差异检测。

函数签名与出参严格按 docs/02_接口契约与数据结构.md §3.4 / §4。
**改 diff 的 key 名 = 乙丙静默失效**（不报错，只是永远筛出零条受影响路径），禁止。

约定落点：
  * docs/03 口径 1 —— 链路归一化 frozenset 只在 _edge_key() 出现一次；
    make_snapshot 调它，diff 只做集合差集，不写第二套去重逻辑。
  * docs/02 §7 —— 日志用 brain.topo，空 IP / 空 locations 跳过并 WARNING。
  * snapshot 内部用 frozenset（可哈希、可做差集），但 diff 出参必须转成 list，
    因为 diff 要进 /api/diff 和 WebSocket，frozenset 不能 JSON 序列化。

NOTE（契约里两处矛盾，本实现按「不改签名」处理，待 N3 前评审）：
  1. §2.1 要求过滤 available=false 的交换机，但 build_graph(links) 收不到 /devices。
     缓解：ONOS 上掉线的交换机其链路 state 也会变为非 ACTIVE，已被 state 过滤覆盖。
  2. FR-02 判据是 len(graph) == 交换机数，但纯靠 links 感知不到「一台交换机都没连」的孤立节点。
     缓解：build_graph 为每条链路的 src 和 dst 都建 key，至少连通拓扑下节点数正确。
  真要彻底解决，得给 build_graph 加 devices 入参 —— 那是改冻结签名，必须先提 PR 改 docs/02。
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("brain.topo")


def _edge_key(u: str, v: str) -> frozenset:
    """把一条有向边归一化成无向键。

    /links 里一条物理链路按 A->B、B->A 各报一次。不断言这个就会把
    断一条链路算成 edges_removed == 2，A3 验收直接挂。
    """
    return frozenset((u, v))


def _as_port(value, where: str):
    """ONOS 给的端口是字符串，契约要求 graph / host_table 里是 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("skip non-numeric port %r at %s", value, where)
        return None


def build_graph(links: list[dict]) -> dict:
    """ONOS /links 的 links 列表 -> 有向邻接表。

    出参：{dpid: {nbr_dpid: {"sport": int, "dport": int, "weight": 1}}}
    只收 type == "DIRECT" 且 state == "ACTIVE" 的链路。
    """
    graph: dict[str, dict[str, dict]] = {}

    for link in links:
        # 断链重算的触发点就在这里：state 不再是 ACTIVE 就不进图。
        if link.get("type") != "DIRECT" or link.get("state") != "ACTIVE":
            continue

        src = link.get("src") or {}
        dst = link.get("dst") or {}
        u, v = src.get("device"), dst.get("device")
        if not u or not v or u == v:
            log.warning("skip link with missing or self endpoints: %r", link)
            continue

        sport = _as_port(src.get("port"), f"{u}->{v} src.port")
        dport = _as_port(dst.get("port"), f"{u}->{v} dst.port")
        if sport is None or dport is None:
            continue

        # 两端都建 key：即使某端暂时没有出边，也要出现在 nodes 里。
        graph.setdefault(u, {})
        graph.setdefault(v, {})
        graph[u][v] = {"sport": sport, "dport": dport, "weight": 1}

    return graph


def build_host_table(hosts: list[dict]) -> dict:
    """ONOS /hosts 的 hosts 列表 -> IP 到位置的映射。

    出参：{ip: {"dpid": str, "port": int, "mac": str}}
    坑（docs/02 §2.3）：id 是 MAC/VLAN 不是 IP；没 pingall 时 ipAddresses 为空；
    locations 里 elementId 和 port 两项都要读，否则识别不出「只换了端口」的迁移。
    """
    table: dict[str, dict] = {}

    for host in hosts:
        hid = host.get("id", "<no-id>")
        ips = host.get("ipAddresses") or []
        locs = host.get("locations") or []

        if not ips:
            log.warning("skip host %s: ipAddresses is empty (run pingall first)", hid)
            continue
        if not locs:
            log.warning("skip host %s: locations is empty", hid)
            continue

        loc = locs[0]
        dpid = loc.get("elementId")
        port = _as_port(loc.get("port"), f"host {hid} locations[0].port")
        if not dpid or port is None:
            log.warning("skip host %s: bad location %r", hid, loc)
            continue

        ip = ips[0]
        if len(ips) > 1:
            log.warning("host %s has %d ipAddresses, using first: %s", hid, len(ips), ip)

        # mac 优先取字段，缺失时从 id 的 "MAC/VLAN" 前半段兜底。
        mac = host.get("mac") or str(hid).split("/", 1)[0]
        table[ip] = {"dpid": dpid, "port": port, "mac": mac}

    return table


def empty_snapshot() -> dict:
    """轮询主循环的初始 prev，形状与 make_snapshot 一致。"""
    return {"nodes": set(), "edges": set(), "hosts": {}}


def make_snapshot(graph: dict, host_table: dict) -> dict:
    """把当前图与主机表压成可比较的快照（结构见 docs/02 §4.3）。

    hosts 的值必须是 (dpid, port) 二元组：FR-05 要求同 IP 换了交换机**或**
    换了端口都算迁移，只比 dpid 会漏掉后者。
    """
    return {
        "nodes": set(graph),
        "edges": {_edge_key(u, v) for u, nbrs in graph.items() for v in nbrs},
        "hosts": {ip: (info["dpid"], info["port"]) for ip, info in host_table.items()},
    }


def diff(prev: dict, cur: dict) -> dict:
    """两轮快照比对，出参 9 个 key，字段名一个都不许改。

    hosts_added / hosts_removed / hosts_moved 三者互斥：
    位置变化的主机只进 hosts_moved，绝不重复出现在 added 或 removed 里（FR-05 判据）。
    """
    p_nodes, c_nodes = prev["nodes"], cur["nodes"]
    p_edges, c_edges = prev["edges"], cur["edges"]
    p_hosts, c_hosts = prev["hosts"], cur["hosts"]

    nodes_added = sorted(c_nodes - p_nodes)
    nodes_removed = sorted(p_nodes - c_nodes)

    # frozenset -> 有序 list，保证 JSON 可序列化且输出稳定。
    def _edge_list(edge_set):
        return [sorted(e) for e in sorted(edge_set, key=lambda e: sorted(e))]

    edges_added = _edge_list(c_edges - p_edges)
    edges_removed = _edge_list(p_edges - c_edges)

    added_ips = c_hosts.keys() - p_hosts.keys()
    removed_ips = p_hosts.keys() - c_hosts.keys()
    common_ips = c_hosts.keys() & p_hosts.keys()

    hosts_added = {
        ip: {"dpid": c_hosts[ip][0], "port": c_hosts[ip][1]}
        for ip in sorted(added_ips)
    }
    hosts_removed = sorted(removed_ips)

    hosts_moved = {}
    for ip in sorted(common_ips):
        if c_hosts[ip] != p_hosts[ip]:
            hosts_moved[ip] = {
                "from": [p_hosts[ip][0], p_hosts[ip][1]],
                "to": [c_hosts[ip][0], c_hosts[ip][1]],
            }

    changed = any(
        (
            nodes_added,
            nodes_removed,
            edges_added,
            edges_removed,
            hosts_added,
            hosts_removed,
            hosts_moved,
        )
    )

    return {
        "changed": changed,
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "hosts_added": hosts_added,
        "hosts_removed": hosts_removed,
        "hosts_moved": hosts_moved,
        "ts": time.time(),
    }
