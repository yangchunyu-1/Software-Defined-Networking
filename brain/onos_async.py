"""甲 · 感知层：ONOS 北向 REST 客户端（aiohttp，全项目唯一 HTTP 客户端）。

唯一事实来源：docs/01 ADR-01 / docs/02 §3.3 / §5 / §7。
2026-11-05 团队决定：去掉 requests 双轨，只保留 Sanic（Web 框架）+ aiohttp（客户端）。
本模块同时承载主链路轮询、诊断脚本与 N7 断链演示，不再有同步版 onos.py。

硬约束（docs/03 口径 8）：所有 I/O 都 await，不引入任何同步 HTTP 客户端。
在 async def 里调同步 HTTP 会堵住唯一的事件循环。

Session 生命周期：main.py 在 Sanic startup 调 make_session()、cleanup 里
`await session.close()`。fetch_* 有懒创建兜底，诊断脚本不起 Sanic 也能直接 await。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

import config

log = logging.getLogger("brain.onos_async")

__all__ = [
    "OnosError",
    "make_session",
    "fetch_devices",
    "fetch_links",
    "fetch_hosts",
    "fetch_flows",
    "post_flow",
    "delete_flow",
    "set_port_state",
]

# ⚠ 实测（ONOS 2.7.0）：GET 带 Content-Type: application/json 会被 JAX-RS 回 415。
# 默认只发 Accept，有 json_body 的 POST 才补 Content-Type（aiohttp 用 json= 也会自动补）。
_HEADERS = {"Accept": "application/json"}

_SESSION: aiohttp.ClientSession | None = None


class OnosError(Exception):
    """非 2xx / 超时 / JSON 解析失败统一抛这个（docs/02 §7）。

    status_code 为 None 表示压根没拿到 HTTP 响应（超时、连接被拒）。
    body 只留前 200 字，避免把整份拓扑刷进日志。
    全项目统一异常类型，poll_loop 一个 except 就能捕获。
    """

    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = (body or "")[:200]


def flow_id_from_response(payload: Any) -> str | None:
    """从 POST /flows 响应里取 flowId。

    ✅ 实测（2026-11-12，ONOS 2.7.0）：POST 返回 2xx 但**响应体无可用 JSON**，
    取不到 id，必须走 match_flow_id 的 GET 反查。docs/02 §2.4 的 ❓ 已结。
    这里仍兼容文档原先预期的 {"flows":[{...,"id":"123"}]} / {"id":"123"}，
    以及个别版本可能给的 {"flowId":"123"}。
    """
    if not isinstance(payload, dict):
        return None
    for key in ("id", "flowId"):
        if payload.get(key) is not None:
            return str(payload[key])
    flows = payload.get("flows")
    if isinstance(flows, list) and flows:
        first = flows[0]
        if isinstance(first, dict) and first.get("id") is not None:
            return str(first["id"])
    return None


def _canon(value: Any):
    """把 ONOS 归一化过的字段还原成可比较的结构。

    实测：提交时写 ethType="0x0800"，GET 回来可能是 "0x800" / 2048；
    treatment 还会多出 deferred / clear 等键。直接拿两个 dict 做 == 永远匹配不上。
    """
    if isinstance(value, dict):
        return tuple(sorted((key, _canon(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canon(item) for item in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("0x"):
            try:
                return int(text, 16)
            except ValueError:
                pass
        elif text.isdigit():
            return int(text)
        return text.lower()
    return value


def _criteria_key(selector: Any) -> tuple:
    if not isinstance(selector, dict):
        return ()
    items = [_canon(item) for item in (selector.get("criteria") or [])]
    return tuple(sorted(items, key=repr))


def _treatment_key(treatment: Any) -> tuple:
    """只比 instructions：ONOS 会追加 deferred 等键，比较整个 treatment 会误判。

    ⚠ 实测（2026-11-12，ONOS 2.7.0）：提交空 `instructions: []` 时，GET 回来是
    `{"instructions":[{"type":"NOACTION"}],"deferred":[]}`。NOACTION 就是空指令的
    规范化写法，必须过滤掉，否则 want=() 而 got 非空，自己刚下发的规则会被跳过
    （19:36 第二轮 SMOKE 即因此 `flowId=None`）。
    """
    if not isinstance(treatment, dict):
        return ()
    items = []
    for item in (treatment.get("instructions") or []):
        if (isinstance(item, dict) and len(item) == 1
                and str(item.get("type", "")).upper() == "NOACTION"):
            continue
        items.append(_canon(item))
    return tuple(sorted(items, key=repr))


def _items_match(want: tuple, got: tuple) -> bool:
    """我们提交的每个字段都要在返回的同一条 criterion/instruction 里出现且相等；
    返回里多出来的键（ONOS 补的）忽略。"""
    left, right = dict(want), dict(got)
    if "type" in left and "type" in right and left["type"] != right["type"]:
        return False
    return all(key in right and right[key] == value for key, value in left.items())


def _subset_match(want: tuple, got: tuple) -> bool:
    if want:
        return all(any(_items_match(item, actual) for actual in got) for item in want)
    return not got


def match_flow_id(flows: list[dict], new_flow: dict) -> str | None:
    """POST 响应没给 id 时的退路：GET 反查。

    按**归一化后**的 selector/treatment 匹配：我们提交的 criteria 与 instructions
    只要被返回规则包含即可（ONOS 会改写字段、补键），再用 priority 排除同一
    appId 下别的通信对。匹配不上宁可返回 None（调用方 log.warning），也不猜 ——
    猜错会让 delete_flow 删掉别人的流表。
    """
    want_sel = _criteria_key(new_flow.get("selector"))
    want_trt = _treatment_key(new_flow.get("treatment"))
    want_priority = new_flow.get("priority")
    for item in flows:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        got_sel = _criteria_key(item.get("selector"))
        got_trt = _treatment_key(item.get("treatment"))
        if not _subset_match(want_sel, got_sel) or not _subset_match(want_trt, got_trt):
            continue
        if want_priority is not None and item.get("priority") is not None:
            if _canon(item["priority"]) != _canon(want_priority):
                continue
        return str(item["id"])
    return None


def make_session() -> aiohttp.ClientSession:
    """由 main.py 在 Sanic startup（事件循环内）调用一次。

    aiohttp 要求 ClientSession 在 running loop 里创建；重复调用返回同一实例。
    超时、认证、请求头只在这里设一次，fetch_* 不再各自传。
    """
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(config.ONOS_USER, config.ONOS_PASS),
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT),
        )
    return _SESSION


async def _request(method: str, path: str, *, params=None, json_body=None, expect_json=True):
    session = make_session()  # 在 async def 里调用，一定有 running loop
    url = f"{config.ONOS_BASE}{path}"
    headers = dict(_HEADERS)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with session.request(
            method, url, headers=headers, params=params, json=json_body
        ) as resp:
            status = resp.status
            text = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise OnosError(
            f"{method} {url} failed: {type(exc).__name__}: {exc}"
        ) from exc

    if not 200 <= status < 300:
        raise OnosError(
            f"{method} {url} -> HTTP {status}", status_code=status, body=text
        )
    if not expect_json or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise OnosError(
            f"{method} {url} -> invalid JSON", status_code=status, body=text
        ) from exc


def _unwrap(payload: Any, key: str, path: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise OnosError(f"{path}: response has no '{key}' list", body=repr(payload)[:200])
    return payload[key]


async def fetch_devices() -> list[dict]:
    return _unwrap(await _request("GET", "/devices"), "devices", "/devices")


async def fetch_links() -> list[dict]:
    return _unwrap(await _request("GET", "/links"), "links", "/links")


async def fetch_hosts() -> list[dict]:
    return _unwrap(await _request("GET", "/hosts"), "hosts", "/hosts")


async def fetch_flows(device_id: str) -> list[dict]:
    path = f"/flows/{device_id}"
    return _unwrap(await _request("GET", path), "flows", path)


async def post_flow(device_id: str, flow: dict) -> str | None:
    """下发流表，appId 走 query（docs/02 §2.4）。响应不带 id 时 GET 反查。"""
    path = f"/flows/{device_id}"
    params = {"appId": config.APP_ID}
    flow_id = flow_id_from_response(
        await _request("POST", path, params=params, json_body=flow)
    )
    if flow_id is not None:
        return flow_id

    try:
        existing = await _request("GET", path, params=params)
        if isinstance(existing, dict):
            flows = existing.get("flows", []) or []
        elif isinstance(existing, list):
            flows = existing  # 个别版本直接返回数组
        else:
            flows = []
        flow_id = match_flow_id(flows, flow)
    except OnosError as exc:
        log.warning("post_flow fallback GET failed on %s: %s", device_id, exc)
    if flow_id is None:
        log.warning(
            "post_flow: cannot resolve flowId on %s (selector=%s)",
            device_id,
            flow.get("selector"),
        )
    return flow_id


async def delete_flow(device_id: str, flow_id: str) -> bool:
    """精确删一条，成功 200/204 返回 True。"""
    path = f"/flows/{device_id}/{flow_id}"
    await _request("DELETE", path, expect_json=False)
    return True


async def set_port_state(device_id: str, port_id: str, enabled: bool) -> bool:
    """制造/恢复链路故障（N7 演示的断链步骤），body 是 {"enabled": bool}。

    按 docs/02 §2 端点表第 7 行。原先只有同步版 onos.py 有这个函数，
    单客户端方案下补进异步版，N7 不再依赖 requests。
    ❓ 本函数仍未实测（2026-11-12：/links 为空，`--port-state` 会 SKIP）。
    `post_flow`（201、空 body）与 `delete_flow`（204、立即生效）均已实测；
    端口 down/up 留到起好 Mininet 拓扑后的 N7 验证。
    """
    path = f"/devices/{device_id}/portstate/{port_id}"
    await _request(
        "POST", path, json_body={"enabled": bool(enabled)}, expect_json=False
    )
    return True
