r"""本地联调用的「假 ONOS」——**不是控制器**，只复刻 docs/02 用到的北向端点。

为什么需要它
------------
`./run.sh demo` 要让「另一个人照 README 盲跑」时不依赖 Docker/WSL：本脚本用
aiohttp 在 127.0.0.1:8181 返回一份**写死的拓扑**，接口形状照 brain/samples/ 里的
真实响应（2026-11-14 抓的 ONOS 2.7.0）复制，所以 brain/ 一行都不用改：

    GET    /onos/v1/devices                       4 台交换机
    GET    /onos/v1/links                         5 条物理链路（双向各报一次）
    GET    /onos/v1/hosts                         6 台主机
    POST   /onos/v1/flows/{dpid}?appId=...        201 + 空 body（与真 ONOS 一致）
    GET    /onos/v1/flows/{dpid}                  已下发规则（带 id，供反查）
    DELETE /onos/v1/flows/{dpid}/{flowId}         204
    POST   /onos/v1/devices/{dpid}/portstate/{p}  {"enabled": bool}，断/通该端口链路

三个只属于本脚本的控制端点（真 ONOS 没有，别拿去验收）：

    POST /_mock/link  {"a": "...", "b": "...", "up": false}   断/通一条链路
    POST /_mock/down  {"down": true}                          整个控制器回 503（测 NF-03）
    POST /_mock/hosts {"pingall": false}                      hosts 的 ipAddresses 变空（测 N6-6）
    GET  /_mock/state                                         当前拓扑 + 流表

拓扑（带环 + 一条对角线，断一条还能绕）
--------------------------------------
        h1(10.0.0.1)                      h4(10.0.0.4)
             |                                 |
            s1 ────────────── s4
            | \             / |
            |   \         /   |        s1-s2 / s2-s3 / s3-s4 / s4-s1 成环
            |     \     /     |        s1-s3 是对角线
            |       \ /       |
            s2 ──────X────── s3        h2(10.0.0.2) 挂 s2，h5(10.0.0.5) 也挂 s2
             |               |         h3(10.0.0.3) 挂 s3，h6(10.0.0.6) 也挂 s3
        h2(10.0.0.2)     h3(10.0.0.3)

跑法
----
    python3 tools/mock_onos.py                  # 监听 8181，前台
    python3 tools/mock_onos.py --port 18181     # 换端口（要同时改 config.ONOS_PORT）
    # 或者直接用 ./run.sh demo，它会把本脚本和 main.py 都拉起来
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import time

from aiohttp import web

log = logging.getLogger("mock_onos")


def dpid(n: int) -> str:
    """ONOS 的 dpid 是 16 位零填充的十进制，不是十六进制。"""
    return "of:%016d" % n


# --- 拓扑：一处定义，后面所有端点都从这里生成 -----------------------------

SWITCH_IDS = [dpid(i) for i in range(1, 5)]

# (a, a_port, b, b_port)
LINK_DEFS = [
    (1, 1, 2, 1),
    (2, 2, 3, 1),
    (3, 2, 4, 1),
    (4, 2, 1, 2),
    (1, 3, 3, 3),  # 对角线，让「跨两台交换机」的路径有两条可选
]

# (ip, mac, 挂载交换机号, 挂载端口)
HOST_DEFS = [
    ("10.0.0.1", "00:00:00:00:00:01", 1, 10),
    ("10.0.0.2", "00:00:00:00:00:02", 2, 10),
    ("10.0.0.3", "00:00:00:00:00:03", 3, 10),
    ("10.0.0.4", "00:00:00:00:00:04", 4, 10),
    ("10.0.0.5", "00:00:00:00:00:05", 2, 11),
    ("10.0.0.6", "00:00:00:00:00:06", 3, 11),
]


class State:
    def __init__(self) -> None:
        self.links = [
            {"a": dpid(a), "a_port": ap, "b": dpid(b), "b_port": bp, "up": True}
            for a, ap, b, bp in LINK_DEFS
        ]
        self.flows: dict[str, list[dict]] = {d: [] for d in SWITCH_IDS}
        self.seq = itertools.count(1)
        self.down = False
        self.pingall = True  # False 时 hosts 的 ipAddresses 返回 []（模拟没 pingall）
        self.started = time.time()
        self.requests = 0

    # ---- 视图 ----------------------------------------------------------

    def devices_payload(self) -> dict:
        out = []
        for i, d in enumerate(SWITCH_IDS, start=1):
            out.append(
                {
                    "id": d,
                    "type": "SWITCH",
                    "available": True,
                    "role": "MASTER",
                    "mfr": "Open vSwitch",
                    "hw": "Open vSwitch",
                    "sw": "2.17.3",
                    "serial": "%016d" % i,
                    "chassisId": str(i),
                    "annotations": {},
                }
            )
        return {"devices": out}

    def links_payload(self) -> dict:
        """一物理链路报两条有向记录；down 的报 state=INACTIVE（不是删掉）。

        这样 topo.build_graph 的 `state != ACTIVE` 过滤才会被真正走到，
        前端也能拿到「断链」这条证据（FR-14）。
        """
        out = []
        for link in self.links:
            state = "ACTIVE" if link["up"] else "INACTIVE"
            out.append(
                {
                    "src": {"device": link["a"], "port": str(link["a_port"])},
                    "dst": {"device": link["b"], "port": str(link["b_port"])},
                    "type": "DIRECT",
                    "state": state,
                    "annotations": {},
                }
            )
            out.append(
                {
                    "src": {"device": link["b"], "port": str(link["b_port"])},
                    "dst": {"device": link["a"], "port": str(link["a_port"])},
                    "type": "DIRECT",
                    "state": state,
                    "annotations": {},
                }
            )
        return {"links": out}

    def hosts_payload(self) -> dict:
        out = []
        for ip, mac, sw, port in HOST_DEFS:
            out.append(
                {
                    "id": f"{mac}/None",
                    "mac": mac,
                    "vlan": "None",
                    "innerVlan": "None",
                    "outerTpid": "0x0000",
                    "configured": False,
                    "suspended": False,
                    "ipAddresses": ([ip] if self.pingall else []),
                    "locations": [{"elementId": dpid(sw), "port": str(port)}],
                }
            )
        return {"hosts": out}

    def flows_payload(self, device: str) -> dict:
        return {"flows": list(self.flows.get(device, []))}


STATE = State()


# --- 中间件：模拟控制器整体不可用（NF-03 自愈） ---------------------------


@web.middleware
async def outage_middleware(request: web.Request, handler):
    STATE.requests += 1
    if STATE.down and request.path.startswith("/onos/v1/"):
        return web.Response(status=503, text="mock: controller down")
    return await handler(request)


# --- 北向端点 -------------------------------------------------------------


async def get_devices(request: web.Request) -> web.Response:
    return web.json_response(STATE.devices_payload())


async def get_links(request: web.Request) -> web.Response:
    return web.json_response(STATE.links_payload())


async def get_hosts(request: web.Request) -> web.Response:
    return web.json_response(STATE.hosts_payload())


async def post_flow(request: web.Request) -> web.Response:
    device = request.match_info["device"]
    if device not in STATE.flows:
        return web.Response(status=404, text=f"unknown device {device}")
    try:
        flow = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="body is not JSON")

    flow_id = "mock-%d" % next(STATE.seq)
    stored = dict(flow)
    stored.update(
        {
            "id": flow_id,
            "deviceId": device,
            "appId": request.query.get("appId", "org.onosproject.rest"),
            "state": "ADDED",
            "life": 0,
            "packets": 0,
            "bytes": 0,
        }
    )
    STATE.flows[device].append(stored)
    # 真 ONOS 实测：201 + 空 body，不带 flowId（docs/02 §2.4），所以这里也返回空。
    return web.Response(status=201)


async def get_flows(request: web.Request) -> web.Response:
    """注意：真 ONOS 的 appId 查询参数**不过滤**（docs/02 §2.5 实测），这里照抄。"""
    device = request.match_info["device"]
    return web.json_response(STATE.flows_payload(device))


async def delete_flow(request: web.Request) -> web.Response:
    device = request.match_info["device"]
    flow_id = request.match_info["flow_id"]
    before = len(STATE.flows.get(device, []))
    STATE.flows[device] = [f for f in STATE.flows.get(device, []) if f["id"] != flow_id]
    if len(STATE.flows[device]) == before:
        return web.Response(status=404, text=f"no such flow {flow_id}")
    return web.Response(status=204)


async def port_state(request: web.Request) -> web.Response:
    device = request.match_info["device"]
    port = int(request.match_info["port"])
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    hit = 0
    for link in STATE.links:
        if (link["a"], link["a_port"]) == (device, port):
            link["up"], hit = enabled, hit + 1
        elif (link["b"], link["b_port"]) == (device, port):
            link["up"], hit = enabled, hit + 1
    if hit == 0:
        return web.Response(status=404, text=f"no link at {device}:{port}")
    log.info("portstate %s:%s enabled=%s (%d link)", device, port, enabled, hit)
    return web.Response(status=200, text="")


# --- 只属于 mock 的控制端点 ----------------------------------------------


async def mock_link(request: web.Request) -> web.Response:
    body = await request.json()
    a, b = body.get("a"), body.get("b")
    up = bool(body.get("up", True))
    for link in STATE.links:
        if {link["a"], link["b"]} == {a, b}:
            link["up"] = up
            log.info("link %s<->%s up=%s", a, b, up)
            return web.json_response({"ok": True, "up": up})
    return web.json_response({"ok": False, "error": "no such link"}, status=404)


async def mock_down(request: web.Request) -> web.Response:
    body = await request.json()
    STATE.down = bool(body.get("down", True))
    log.warning("controller down=%s", STATE.down)
    return web.json_response({"ok": True, "down": STATE.down})


async def mock_hosts(request: web.Request) -> web.Response:
    """模拟「Mininet 里没 pingall」：hosts 还在，但 ipAddresses 全空（docs/02 §2.3）。"""
    body = await request.json()
    STATE.pingall = bool(body.get("pingall", True))
    log.warning("hosts pingall=%s", STATE.pingall)
    return web.json_response({"ok": True, "pingall": STATE.pingall})


async def mock_state(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "topology": {
                "switches": SWITCH_IDS,
                "links": [
                    {
                        "a": l["a"],
                        "a_port": l["a_port"],
                        "b": l["b"],
                        "b_port": l["b_port"],
                        "up": l["up"],
                    }
                    for l in STATE.links
                ],
                "hosts": [{"ip": ip, "dpid": dpid(sw), "port": p} for ip, _m, sw, p in HOST_DEFS],
            },
            "flows": {d: len(v) for d, v in STATE.flows.items()},
            "down": STATE.down,
            "pingall": STATE.pingall,
            "requests": STATE.requests,
            "uptime_s": round(time.time() - STATE.started, 1),
            "note": "这是 mock，不是 ONOS；别在验收里当控制器的证据",
        }
    )


async def index(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "what": "mock ONOS for local dev (not a controller)",
            "端点数": [
                "GET /onos/v1/devices",
                "GET /onos/v1/links",
                "GET /onos/v1/hosts",
                "GET/POST /onos/v1/flows/{dpid}",
                "DELETE /onos/v1/flows/{dpid}/{flowId}",
                "POST /onos/v1/devices/{dpid}/portstate/{port}",
                "POST /_mock/link",
                "POST /_mock/down",
                "GET /_mock/state",
            ],
        }
    )


def build_app() -> web.Application:
    app = web.Application(middlewares=[outage_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/onos/v1/devices", get_devices)
    app.router.add_get("/onos/v1/links", get_links)
    app.router.add_get("/onos/v1/hosts", get_hosts)
    app.router.add_post("/onos/v1/flows/{device}", post_flow)
    app.router.add_get("/onos/v1/flows/{device}", get_flows)
    app.router.add_delete("/onos/v1/flows/{device}/{flow_id}", delete_flow)
    app.router.add_post("/onos/v1/devices/{device}/portstate/{port}", port_state)
    app.router.add_post("/_mock/link", mock_link)
    app.router.add_post("/_mock/down", mock_down)
    app.router.add_post("/_mock/hosts", mock_hosts)
    app.router.add_get("/_mock/state", mock_state)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="mock ONOS（本地联调用，不是控制器）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8181, help="默认与 config.ONOS_PORT 一致")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [mock_onos] %(message)s"
    )
    log.info(
        "mock ONOS on http://%s:%d  —— 4 switches / 5 links / 6 hosts",
        args.host,
        args.port,
    )
    log.info("拓扑与行为见 docs: brain/tools/mock_onos.py 头部注释")
    web.run_app(build_app(), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
