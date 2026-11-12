"""丙 · 应用层：Sanic 服务端（HTTP API + 轮询挂载 + WebSocket 推送）。

唯一事实来源：docs/02_接口契约与数据结构.md §3.3 / §5 / §6 / §7。

设计要点：
  * 单进程单事件循环（docs/03 口径 8）：所有共享状态挂在 app.ctx，
    轮询协程写、HTTP 处理器读，**只整体替换引用，不原地修改**。
  * 全项目唯一 REST 客户端是 onos_async（aiohttp），本文件不引入任何同步 HTTP。
  * 启动/停止监听器只在 `python3 main.py` 时注册；契约测试（tests/test_api.py）
    import 本模块不会触发轮询、不会连 ONOS。
  * 对乙的 dijkstra / install_path / uninstall / affected / recompute 的调用
    严格按 docs/02 §3.5 / §3.6 签名；_maybe_await 兼容将来改为 async 的实现。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path

from sanic import Sanic, response

import config
import flow
import onos_async
import path
import topo
from onos_async import OnosError

log = logging.getLogger("brain.main")

STATIC_INDEX = Path(__file__).parent / "static" / "index.html"

app = Sanic("sdn_brain")
app.config.FALLBACK_ERROR_FORMAT = "json"


def init_ctx(app: Sanic) -> None:
    """初始化全部共享状态。服务启动和契约测试都从这里进，保证字段一致。"""
    app.ctx.graph = {}
    app.ctx.host_table = {}
    app.ctx.snapshot = topo.empty_snapshot()
    app.ctx.last_diff = None
    app.ctx.deployed = {}
    app.ctx.ws_set = set()
    app.ctx.last_poll_ts = None
    app.ctx.poll_count = 0
    app.ctx.poll_ms = 0
    app.ctx.onos_ok = False
    app.ctx.onos_errors = 0
    app.ctx.last_error = None
    app.ctx.push_count = 0
    app.ctx.started_at = time.time()


init_ctx(app)


async def _maybe_await(value):
    """兼容乙的同步 / 异步两种实现，调用点不用改。

    现状（2026-11-12）：flow 的 install_path / uninstall / recompute 均为 async def，
    签名见 02 §3.6（8/3/6 参数，含 deployed 与 onos）。丙侧调用必须按该签名传参，
    否则一调就 TypeError。
    """
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------- HTTP API


@app.get("/")
async def index(request):
    # 前端可视化属后续迭代；当前 static/index.html 是占位自检页。
    if STATIC_INDEX.exists():
        return await response.file(str(STATIC_INDEX))
    return response.text("sdn-brain is running. See /api/state")


@app.get("/api/state")
async def get_state(request):
    """14 个字段，每个字段对应一条 FR/NF（docs/02 §6.1），不许随手增删。"""
    paths = [
        {
            "src": key[0],
            "dst": key[1],
            "path": record["path"],
            "installed_at": record["installed_at"],
        }
        for key, record in app.ctx.deployed.items()
    ]
    return response.json(
        {
            "graph": app.ctx.graph,
            "hosts": app.ctx.host_table,
            "paths": paths,
            "last_diff": app.ctx.last_diff,
            "last_poll_ts": app.ctx.last_poll_ts,
            "poll_count": app.ctx.poll_count,
            "poll_ms": app.ctx.poll_ms,
            "onos_ok": app.ctx.onos_ok,
            "onos_errors": app.ctx.onos_errors,
            "last_error": app.ctx.last_error,
            "ws_clients": len(app.ctx.ws_set),
            "push_count": app.ctx.push_count,
            "started_at": app.ctx.started_at,
            "ts": time.time(),
        }
    )


@app.post("/api/route")
async def post_route(request):
    """下发一对主机的路径：查主机位置 -> dijkstra -> install_path。

    出参（docs/02 §6）：成功 {"ok":true,"path":[...],"cost":N}；
    不可达 {"ok":false,"error":"unreachable"}（HTTP 200，不抛异常，见 §7）。
    """
    body = request.json or {}
    src, dst = body.get("src"), body.get("dst")
    if not src or not dst:
        return response.json({"ok": False, "error": "missing src/dst"}, status=400)

    src_host = app.ctx.host_table.get(src)
    dst_host = app.ctx.host_table.get(dst)
    if src_host is None or dst_host is None:
        # 主机没 pingall 过就没有 IP，返回 404 让调用方知道先去 pingall。
        return response.json({"ok": False, "error": "unknown_host"}, status=404)

    key = (src, dst)
    if key in app.ctx.deployed:
        # 02 §3.6：已存在的 key 禁止静默覆盖，替换只能走 recompute。
        return response.json({"ok": False, "error": "already_exists"}, status=409)

    res = path.dijkstra(app.ctx.graph, src_host["dpid"], dst_host["dpid"])
    if res is None:
        return response.json({"ok": False, "error": "unreachable"})

    # 02 §3.6 / §6.2：install_path 自己把 record 写进传入的 deployed（乙侧写入，
    # 丙侧不再写第二遍）。这里传工作副本，成功后整体替换引用（口径 8）；
    # 失败时副本里可能留有 cleanup_required 记录——必须写回，否则那些规则永远删不掉。
    work = dict(app.ctx.deployed)
    try:
        result = await _maybe_await(
            flow.install_path(
                key,
                res,
                app.ctx.graph,
                src_host,
                dst_host,
                work,
                onos_async,
                config.FLOW_PRIORITY,
            )
        )
    except OnosError as exc:
        # install_path 内部已按 §7 回滚，这里只上报错误原文。
        log.error("install_path failed for %s->%s: %s", src, dst, exc)
        return response.json({"ok": False, "error": str(exc)[:200]}, status=502)

    app.ctx.deployed = work
    if not result.get("ok"):
        log.error("install_path refused %s->%s: %s", src, dst, result.get("error"))
        return response.json(
            {"ok": False, "error": result.get("error") or "install_failed"}, status=502
        )

    log.info("route installed %s->%s path=%s", src, dst, res["path"])
    return response.json({"ok": True, "path": res["path"], "cost": res["cost"]})


@app.delete("/api/route")
async def delete_route(request):
    body = request.json or {}
    src, dst = body.get("src"), body.get("dst")
    if not src or not dst:
        return response.json({"ok": False, "error": "missing src/dst"}, status=400)
    key = (src, dst)
    if key not in app.ctx.deployed:
        return response.json({"ok": False, "error": "not_found"}, status=404)

    # uninstall 返回 {"ok","error","remaining"}，不是 bool；且它会原地改 record、
    # 全部删净时 del deployed[key]。传副本以保持「只整体替换引用」（口径 8）。
    work = dict(app.ctx.deployed)
    work[key] = dict(work[key])
    try:
        result = await _maybe_await(flow.uninstall(key, work, onos_async))
    except OnosError as exc:
        log.error("uninstall failed for %s->%s: %s", src, dst, exc)
        return response.json({"ok": False, "error": str(exc)[:200]}, status=502)

    app.ctx.deployed = work
    if not result.get("ok"):
        log.error("uninstall incomplete %s->%s: %s", src, dst, result.get("error"))
        return response.json(
            {
                "ok": False,
                "error": result.get("error") or "uninstall_failed",
                "remaining": result.get("remaining", []),
            },
            status=502,
        )

    log.info("route removed %s->%s", src, dst)
    return response.json({"ok": True})


@app.get("/api/diff")
async def get_diff(request):
    # 首轮轮询完成前返回 {}（docs/02 §6.2）。
    return response.json(app.ctx.last_diff or {})


@app.post("/api/multicast")
async def post_multicast(request):
    # 组播属后续迭代（第一阶段只做单对单），见 docs/02 §6.2。
    return response.json({"ok": False, "error": "not_implemented"}, status=501)


# ------------------------------------------------------------- WebSocket


@app.websocket("/ws/topo")
async def ws_topo(request, ws):
    """拓扑变化时服务端主动推送；页面首屏全量走 GET /api/state。"""
    app.ctx.ws_set.add(ws)
    log.info("ws client connected, total=%d", len(app.ctx.ws_set))
    try:
        async for _ in ws:
            pass  # 只推不收，挂着等断开
    finally:
        app.ctx.ws_set.discard(ws)
        log.info("ws client disconnected, total=%d", len(app.ctx.ws_set))


async def push_ws(app: Sanic, msg: dict) -> None:
    """轮询里 diff.changed 时调用；单个客户端失败不影响其他客户端。"""
    import json

    dead = []
    for ws in list(app.ctx.ws_set):
        try:
            await ws.send(json.dumps(msg))
            app.ctx.push_count += 1
        except Exception as exc:  # 连接已断等情况，踢出集合即可
            log.warning("ws push failed: %s", exc)
            dead.append(ws)
    for ws in dead:
        app.ctx.ws_set.discard(ws)


# ------------------------------------------------------------- 轮询主循环


async def poll_loop(app: Sanic) -> None:
    """按 docs/02 §5 实现，额外维护 NF-01/03/04 要求的计数器。

    硬要求：ONOS 挂掉、网络断了，服务不能崩——捕获一切异常，继续下一轮。
    """
    prev = topo.empty_snapshot()
    while True:
        started = time.monotonic()
        try:
            _, links, hosts = await asyncio.gather(
                onos_async.fetch_devices(),
                onos_async.fetch_links(),
                onos_async.fetch_hosts(),
            )
            graph = topo.build_graph(links)
            host_table = topo.build_host_table(hosts)
            cur = topo.make_snapshot(graph, host_table)
            d = topo.diff(prev, cur)

            app.ctx.poll_count += 1
            app.ctx.last_poll_ts = time.time()
            app.ctx.poll_ms = int((time.monotonic() - started) * 1000)
            app.ctx.onos_ok = True

            if d["changed"]:
                log.info("diff: %s", d)
                # 整体替换引用（口径 8），读方不会看到半成品。
                app.ctx.graph, app.ctx.host_table = graph, host_table
                app.ctx.snapshot, app.ctx.last_diff = cur, d
                prev = cur
                await push_ws(
                    app, {"type": "topo", "diff": d, "graph": graph, "hosts": host_table}
                )
                keys = await _maybe_await(flow.affected(d, app.ctx.deployed))
                if keys:
                    # recompute 会替换 / 标记 record，同样走副本 + 整体替换引用。
                    work = {k: dict(v) for k, v in app.ctx.deployed.items()}
                    records = await _maybe_await(
                        flow.recompute(
                            keys,
                            graph,
                            host_table,
                            work,
                            onos_async,
                            config.FLOW_PRIORITY,
                        )
                    )
                    app.ctx.deployed = work
                    log.info("recompute: %s", records)
        except OnosError as exc:
            app.ctx.onos_ok = False
            app.ctx.onos_errors += 1
            app.ctx.last_error = str(exc)[:200]
            log.warning("poll failed: %s", exc)  # 不 break，不退出
        except Exception as exc:  # 兜底：任何意外都不能弄死轮询
            app.ctx.onos_ok = False
            app.ctx.onos_errors += 1
            app.ctx.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.exception("poll unexpected error")
        await asyncio.sleep(config.POLL_INTERVAL)


# ------------------------------------------------------------- 启动入口


async def _setup(app: Sanic, loop) -> None:
    onos_async.make_session()
    app.add_task(poll_loop(app))


async def _teardown(app: Sanic) -> None:
    session = onos_async._SESSION
    if session is not None and not session.closed:
        await session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app.register_listener(_setup, "before_server_start")
    app.register_listener(_teardown, "after_server_stop")
    # 硬约束：单进程，全系统只有一份内存状态（docs/03 口径 8）。
    app.run(host="0.0.0.0", port=config.WEB_PORT, single_process=True, access_log=False)
