"""tree,2,2 拓扑：3 台交换机 + 4 台主机。

手写脚手架，字段形状照 docs/02 §2.1 / §2.2 / §2.3，结构照 §4.1 / §4.2。
端口分配按 Mininet tree,2,2 的常见布局：
    s1:1 <-> s2:3      s1:2 <-> s3:3
    h1/h2 挂在 s2:1 / s2:2      h3/h4 挂在 s3:1 / s3:2
ONOS /links 对每条物理链路按两个方向各报一次，所以 2 条物理链路 = 4 条记录。

✅ 已实测（ONOS 2.7.0 + tree,2,2 + pingall 0% dropped）：
   brain/samples/{devices,links,hosts}.json 落盘后，
   test_topo.TestRealSamples.test_real_response_matches_handwritten_contract
   逐字段比对通过 —— /links 是 4 条记录（2 条物理链路各报双向），
   dpid、sport/dport、主机挂载端口与这里的字面量完全一致。
   真响应以 samples/*.json 为准；本样本不再标「未实测」。
"""

S1 = "of:0000000000000001"
S2 = "of:0000000000000002"
S3 = "of:0000000000000003"


def _dev(dpid):
    return {"id": dpid, "type": "SWITCH", "available": True}


def _host(mac_suffix, ip, dpid, port):
    mac = f"00:00:00:00:00:{mac_suffix:02d}"
    return {
        "id": f"{mac}/None",
        "mac": mac,
        "vlan": "None",
        "ipAddresses": [ip],
        "locations": [{"elementId": dpid, "port": str(port)}],
        "configured": False,
        "suspended": False,
    }


def _link(u, uport, v, vport, ltype="DIRECT", state="ACTIVE"):
    return {
        "src": {"device": u, "port": str(uport)},
        "dst": {"device": v, "port": str(vport)},
        "type": ltype,
        "state": state,
    }


DEVICES_RESP = {"devices": [_dev(S1), _dev(S2), _dev(S3)]}

LINKS_RESP = {
    "links": [
        _link(S1, 1, S2, 3),
        _link(S2, 3, S1, 1),
        _link(S1, 2, S3, 3),
        _link(S3, 3, S1, 2),
    ]
}

HOSTS_RESP = {
    "hosts": [
        _host(1, "10.0.0.1", S2, 1),
        _host(2, "10.0.0.2", S2, 2),
        _host(3, "10.0.0.3", S3, 1),
        _host(4, "10.0.0.4", S3, 2),
    ]
}

# 期望的 graph / host_table：手写字面量，不依赖 topo.py 的实现。
# 乙的 dijkstra / path_to_flows 契约测试也喂这一份。
GRAPH = {
    S1: {
        S2: {"sport": 1, "dport": 3, "weight": 1},
        S3: {"sport": 2, "dport": 3, "weight": 1},
    },
    S2: {S1: {"sport": 3, "dport": 1, "weight": 1}},
    S3: {S1: {"sport": 3, "dport": 2, "weight": 1}},
}

HOST_TABLE = {
    "10.0.0.1": {"dpid": S2, "port": 1, "mac": "00:00:00:00:00:01"},
    "10.0.0.2": {"dpid": S2, "port": 2, "mac": "00:00:00:00:00:02"},
    "10.0.0.3": {"dpid": S3, "port": 1, "mac": "00:00:00:00:00:03"},
    "10.0.0.4": {"dpid": S3, "port": 2, "mac": "00:00:00:00:00:04"},
}

# 用于验证过滤规则：以下三条都不该进 graph。
INVALID_LINKS = [
    _link(S1, 1, S2, 3, ltype="INDIRECT"),
    _link(S1, 2, S3, 3, state="INACTIVE"),
    _link(S1, "LOCAL", S3, 3),
]
