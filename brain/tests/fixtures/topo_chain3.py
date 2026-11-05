"""3 跳直线拓扑：4 台交换机串成一条链，最短路唯一。

给乙的 dijkstra / path_to_flows 契约测试用（docs/05 第 3 节）。
手写脚手架，规则见 fixtures/__init__.py。

    s1:1 <-> s2:1      s2:2 <-> s3:1      s3:2 <-> s4:1
    h1 @ s1:2          h2 @ s4:2
"""

S1 = "of:0000000000000001"
S2 = "of:0000000000000002"
S3 = "of:0000000000000003"
S4 = "of:0000000000000004"


def _link(u, uport, v, vport):
    return {
        "src": {"device": u, "port": str(uport)},
        "dst": {"device": v, "port": str(vport)},
        "type": "DIRECT",
        "state": "ACTIVE",
    }


# 3 条物理链路 x 2 个方向 = 6 条记录。
LINKS_RESP = {
    "links": [
        _link(S1, 1, S2, 1),
        _link(S2, 1, S1, 1),
        _link(S2, 2, S3, 1),
        _link(S3, 1, S2, 2),
        _link(S3, 2, S4, 1),
        _link(S4, 1, S3, 2),
    ]
}

HOSTS_RESP = {
    "hosts": [
        {
            "id": "00:00:00:00:00:01/None",
            "mac": "00:00:00:00:00:01",
            "vlan": "None",
            "ipAddresses": ["10.0.0.1"],
            "locations": [{"elementId": S1, "port": "2"}],
            "configured": False,
            "suspended": False,
        },
        {
            "id": "00:00:00:00:00:02/None",
            "mac": "00:00:00:00:00:02",
            "vlan": "None",
            "ipAddresses": ["10.0.0.2"],
            "locations": [{"elementId": S4, "port": "2"}],
            "configured": False,
            "suspended": False,
        },
    ]
}

GRAPH = {
    S1: {S2: {"sport": 1, "dport": 1, "weight": 1}},
    S2: {
        S1: {"sport": 1, "dport": 1, "weight": 1},
        S3: {"sport": 2, "dport": 1, "weight": 1},
    },
    S3: {
        S2: {"sport": 1, "dport": 2, "weight": 1},
        S4: {"sport": 2, "dport": 1, "weight": 1},
    },
    S4: {S3: {"sport": 1, "dport": 2, "weight": 1}},
}

HOST_TABLE = {
    "10.0.0.1": {"dpid": S1, "port": 2, "mac": "00:00:00:00:00:01"},
    "10.0.0.2": {"dpid": S4, "port": 2, "mac": "00:00:00:00:00:02"},
}
