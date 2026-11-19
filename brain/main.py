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
import weakref
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

# 每个事件循环一把锁：生产环境单进程单循环，等价于全局锁；契约测试用 asyncio.run
# 为每个请求新建循环，按循环分锁可以避免 Lock 跨循环绑定报错。
_DEPLOY_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _deploy_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _DEPLOY_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _DEPLOY_LOCKS[loop] = lock
    return lock


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
    # static/index.html：拓扑视图（交换机/主机/已下发路径 + 断链高亮），零外网依赖。
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


async def _install_one(src: str, dst: str, work: dict) -> tuple[int, dict, dict | None]:
    """单向下发，record 写进调用方给的 work 副本。

    出参 (HTTP 状态, 响应体, dijkstra 结果)。调用方负责把 work 整体替换回 app.ctx。
    """
    src_host = app.ctx.host_table.get(src)
    dst_host = app.ctx.host_table.get(dst)
    if src_host is None or dst_host is None:
        # 主机没 pingall 过就没有 IP，返回 404 让调用方知道先去 pingall。
        return 404, {"ok": False, "error": "unknown_host"}, None

    key = (src, dst)
    if key in work:
        # 02 §3.6：已存在的 key 禁止静默覆盖，替换只能走 recompute。
        return 409, {"ok": False, "error": "already_exists"}, None

    res = path.dijkstra(app.ctx.graph, src_host["dpid"], dst_host["dpid"])
    if res is None:
        return 200, {"ok": False, "error": "unreachable"}, None

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
        log.error("install_path failed for %s->%s: %s", src, dst, exc)
        return 502, {"ok": False, "error": str(exc)[:200]}, None

    if not result.get("ok"):
        log.error("install_path refused %s->%s: %s", src, dst, result.get("error"))
        return 502, {"ok": False, "error": result.get("error") or "install_failed"}, None
    return 200, {"ok": True, "path": res["path"], "cost": res["cost"]}, res


@app.post("/api/route")
async def post_route(request):
    """下发一对（或双向）主机的路径：查主机位置 -> dijkstra -> install_path。

    `both:true` 时按 src->dst、dst->src 各下发一次（FR-17/同子网 ping 需要回程）。
    出参（docs/02 §6.2）：成功 {"ok":true,"path":[...],"cost":N}；
    双向成功额外带 "bidirectional":true 与 "paths":[{src,dst,path,cost,reused}]；
    不可达 {"ok":false,"error":"unreachable"}（HTTP 200，不抛异常）。
    """
    body = request.json or {}
    src, dst = body.get("src"), body.get("dst")
    if not src or not dst:
        return response.json({"ok": False, "error": "missing src/dst"}, status=400)
    if src == dst:
        return response.json({"ok": False, "error": "invalid_pair"}, status=400)

    both = body.get("both") is True
    keys = [(src, dst)] + ([(dst, src)] if both else [])

    async with _deploy_lock():
        work = dict(app.ctx.deployed)
        installed: list[tuple[str, str]] = []
        paths: list[dict] = []

        for a, b in keys:
            status, payload, _ = await _install_one(a, b, work)

            if status == 409 and both:
                # 双向模式下已存在的一侧直接复用，只补缺的那一侧（可修复半装状态）
                record = work[(a, b)]
                paths.append(
                    {
                        "src": a,
                        "dst": b,
                        "path": record["path"],
                        "cost": record.get("cost", len(record["path"]) - 1),
                        "reused": True,
                    }
                )
                continue

            if not payload.get("ok"):
                # 任一方向失败：把本次已装的方向回滚，不把网络留在半通状态
                for key in reversed(installed):
                    try:
                        await _maybe_await(flow.uninstall(key, work, onos_async))
                    except OnosError as exc:
                        log.error("rollback %s failed: %s", key, exc)
                # 失败方向可能留下 cleanup_required 记录，必须写回
                app.ctx.deployed = work
                return response.json(payload, status=status)

            installed.append((a, b))
            paths.append(
                {
                    "src": a,
                    "dst": b,
                    "path": payload["path"],
                    "cost": payload["cost"],
                    "reused": False,
                }
            )

        app.ctx.deployed = work

    if not both:
        return response.json({"ok": True, "path": paths[0]["path"], "cost": paths[0]["cost"]})

    if all(p["reused"] for p in paths):
        return response.json({"ok": False, "error": "already_exists"}, status=409)

    log.info("route installed both %s<->%s", src, dst)
    return response.json(
        {
            "ok": True,
            "bidirectional": True,
            "path": paths[0]["path"],
            "cost": paths[0]["cost"],
            "paths": paths,
        }
    )


@app.delete("/api/route")
async def delete_route(request):
    """撤销一对（或双向）通信。`both:true` 时两个方向都删，缺一侧不算错。"""
    body = request.json or {}
    src, dst = body.get("src"), body.get("dst")
    if not src or not dst:
        return response.json({"ok": False, "error": "missing src/dst"}, status=400)

    both = body.get("both") is True
    keys = [(src, dst)] + ([(dst, src)] if both else [])

    async with _deploy_lock():
        work = dict(app.ctx.deployed)
        removed: list[dict] = []
        failed: list[dict] = []

        for key in keys:
            if key not in work:
                continue
            # uninstall 会原地改 record，传副本以保持「只整体替换引用」（口径 8）
            work[key] = dict(work[key])
            try:
                result = await _maybe_await(flow.uninstall(key, work, onos_async))
            except OnosError as exc:
                log.error("uninstall failed for %s->%s: %s", key[0], key[1], exc)
                failed.append({"src": key[0], "dst": key[1], "error": str(exc)[:200]})
                continue
            if result.get("ok"):
                removed.append({"src": key[0], "dst": key[1]})
            else:
                log.error("uninstall incomplete %s->%s: %s", key[0], key[1], result.get("error"))
                failed.append(
                    {
                        "src": key[0],
                        "dst": key[1],
                        "error": result.get("error") or "uninstall_failed",
                        "remaining": result.get("remaining", []),
                    }
                )

        app.ctx.deployed = work

    if failed:
        return response.json(
            {"ok": False, "error": "cleanup_required", "removed": removed, "failed": failed},
            status=502,
        )
    if not removed:
        return response.json({"ok": False, "error": "not_found"}, status=404)

    if not both:
        return response.json({"ok": True})

    log.info("route removed both %s<->%s", src, dst)
    return response.json({"ok": True, "removed": removed})


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
                # 重算与 HTTP 下发都会读改写 deployed，必须串行（否则并发丢更新）
                async with _deploy_lock():
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
