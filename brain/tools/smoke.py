"""端到端自检：只认 /api/state 里的数字，不认启动日志（docs/03 N4）。

为什么单独一个脚本
------------------
`03` N4 的判据是「另一个没参与搭建的人照 README 盲跑能通」，而 `curl ... | grep`
这种命令有两个毛病：① 每个人写法不一样，结果没法互相比对；② 失败时只看到一行
grep 结果，不知道是哪一步断的。所以把判据固化成一个脚本，README 里只留一条命令。

默认只读（不往网络里下发任何规则），加了 `--route` 才会真的走一遍
POST /api/route → 查表 → DELETE。加 `--mock` 才会做断链与 WS 推送的检查。

跑法
----
    python3 tools/smoke.py                                  # 只读自检
    python3 tools/smoke.py --route 10.0.0.1 10.0.0.3         # 含下发/删除闭环
    python3 tools/smoke.py --mock --route 10.0.0.1 10.0.0.3  # mock ONOS 全流程

退出码：0 没有 FAIL（SKIP 不影响退出码）；1 有 FAIL；2 加了 --strict 且有 SKIP。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# /api/state 的 14 个字段（docs/02 §6.1）。多一个少一个都要报出来。
STATE_FIELDS = {
    "graph",
    "hosts",
    "paths",
    "last_diff",
    "last_poll_ts",
    "poll_count",
    "poll_ms",
    "onos_ok",
    "onos_errors",
    "last_error",
    "ws_clients",
    "push_count",
    "started_at",
    "ts",
}

# 绕过 http_proxy：WSL/Windows 上常见的代理会让 127.0.0.1 也走代理，症状是超时。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, url: str, body: dict | None = None, timeout: float = 5.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # 连接被拒 / 超时：调用方要看到原因，不要抛栈
        return 0, f"{type(exc).__name__}: {exc}"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []  # (状态, 项, 说明)

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("PASS", name, detail))

    def fail(self, name: str, detail: str = "") -> None:
        self.rows.append(("FAIL", name, detail))

    def skip(self, name: str, detail: str = "") -> None:
        self.rows.append(("SKIP", name, detail))

    def dump(self) -> tuple[int, int, int]:
        width = max((len(r[1]) for r in self.rows), default=8)
        for status, name, detail in self.rows:
            print(f"  [{status}] {name.ljust(width)}  {detail}")
        n_pass = sum(1 for r in self.rows if r[0] == "PASS")
        n_fail = sum(1 for r in self.rows if r[0] == "FAIL")
        n_skip = sum(1 for r in self.rows if r[0] == "SKIP")
        return n_pass, n_fail, n_skip


def get_state(base: str) -> tuple[dict | None, str]:
    status, text = http("GET", f"{base}/api/state")
    if status != 200:
        return None, f"HTTP {status}: {text[:160]}"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def wait_onos_ok(base: str, seconds: float) -> tuple[dict | None, float]:
    started = time.time()
    while time.time() - started < seconds:
        state, _ = get_state(base)
        if state and state.get("onos_ok"):
            return state, time.time() - started
        time.sleep(0.3)
    return None, time.time() - started


def check_basics(base: str, rep: Report, args) -> dict | None:
    status, body = http("GET", f"{base}/")
    if status == 200 and len(body) > 2000 and "<svg" in body:
        rep.ok("页面 GET /", f"200, {len(body)}B, 含 <svg>")
    elif status == 200:
        rep.fail("页面 GET /", f"200 但内容不像拓扑页（{len(body)}B）：别忘了 static/index.html")
    else:
        rep.fail("页面 GET /", f"HTTP {status}: {body[:120]}")

    state, err = get_state(base)
    if state is None:
        rep.fail("GET /api/state", err)
        return None

    keys = set(state)
    if keys == STATE_FIELDS:
        rep.ok("字段数 14", "与 docs/02 §6.1 完全一致")
    else:
        missing, extra = STATE_FIELDS - keys, keys - STATE_FIELDS
        rep.fail("字段数 14", f"缺 {sorted(missing)} 多 {sorted(extra)}")

    if not state.get("onos_ok"):
        rep.fail(
            "onos_ok",
            f"False，last_error={str(state.get('last_error'))[:120]}（先看 curl 8181/onos/v1/devices）",
        )
    else:
        rep.ok("onos_ok", "True")

    if state.get("poll_count", 0) >= 1:
        rep.ok("轮询已在跑", f"poll_count={state['poll_count']}, poll_ms={state.get('poll_ms')}")
    else:
        rep.fail("轮询已在跑", "poll_count=0（启动监听器没挂上，或 ONOS 一直失败）")

    n_sw, n_host = len(state.get("graph") or {}), len(state.get("hosts") or {})
    detail = f"{n_sw} switches / {n_host} hosts"
    if args.expect_devices is not None or args.expect_hosts is not None:
        bad = []
        if args.expect_devices is not None and n_sw != args.expect_devices:
            bad.append(f"期望 {args.expect_devices} 台交换机")
        if args.expect_hosts is not None and n_host != args.expect_hosts:
            bad.append(f"期望 {args.expect_hosts} 台主机")
        (rep.fail if bad else rep.ok)("拓扑规模", detail + ("；" + "，".join(bad) if bad else ""))
    else:
        rep.ok("拓扑规模", detail + "（未指定期望值，只记录）")

    if n_host == 0:
        rep.fail("主机表", "为空：Mininet 里没 pingall，主机的 ipAddresses 是 []")

    if state.get("onos_errors"):
        rep.skip("onos_errors", f"{state['onos_errors']} 次失败（恢复过程中的残留计数不算错）")
    return state


def check_route(base: str, rep: Report, src: str, dst: str, mock_base: str | None) -> None:
    total_before = flows_total(mock_base) if mock_base else None

    status, body = http("POST", f"{base}/api/route", {"src": src, "dst": dst}, timeout=15)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        rep.fail(f"POST 下发 {src}->{dst}", f"HTTP {status}，非 JSON：{body[:120]}")
        return
    if status == 200 and payload.get("ok"):
        path = payload.get("path") or []
        rep.ok(f"POST 下发 {src}->{dst}", f"path={path} cost={payload.get('cost')}")
    else:
        rep.fail(f"POST 下发 {src}->{dst}", f"HTTP {status}：{body[:160]}")
        return

    state, err = get_state(base)
    if state is None:
        rep.fail("下发后 /api/state", err)
        return
    hit = [p for p in state.get("paths") or [] if (p.get("src"), p.get("dst")) == (src, dst)]
    if hit:
        rep.ok("deployed 有记录", f"installed_at={round(hit[0].get('installed_at') or 0)}")
    else:
        rep.fail("deployed 有记录", f"paths 里找不到 {src}->{dst}")

    if mock_base:
        n = flows_total(mock_base)
        if n and n >= len(payload.get("path") or []):
            rep.ok("流表已落到设备", f"设备上共 {n} 条规则（路径 {len(payload['path'])} 跳）")
        else:
            rep.fail("流表已落到设备", f"设备上 {n} 条规则，应有 >= 路径跳数")

    status, body = http("DELETE", f"{base}/api/route", {"src": src, "dst": dst}, timeout=15)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        rep.fail(f"DELETE 撤销 {src}->{dst}", f"HTTP {status}，非 JSON：{body[:120]}")
        return
    if status == 200 and payload.get("ok"):
        rep.ok(f"DELETE 撤销 {src}->{dst}", "ok")
    else:
        rep.fail(f"DELETE 撤销 {src}->{dst}", f"HTTP {status}：{body[:160]}")

    state, _ = get_state(base)
    still = [p for p in (state or {}).get("paths") or [] if (p.get("src"), p.get("dst")) == (src, dst)]
    if still:
        rep.fail("撤销后 deployed 已清", "记录还在，说明 uninstall 没删干净")
    else:
        rep.ok("撤销后 deployed 已清", "paths 里已无该对")

    if mock_base:
        n = flows_total(mock_base)
        if n == 0:
            rep.ok("撤销后流表已清", "设备上 0 条规则（精确删除，没留残规则）")
        else:
            rep.fail("撤销后流表已清", f"设备上还剩 {n} 条规则，会越积越多")
        if total_before is not None and total_before == 0:
            rep.ok("起始状态干净", "下发前设备上没有本项目规则")


def flows_total(mock_base: str) -> int:
    status, body = http("GET", f"{mock_base}/_mock/state")
    if status != 200:
        return -1
    try:
        return sum(json.loads(body).get("flows", {}).values())
    except (json.JSONDecodeError, AttributeError):
        return -1


def check_ws_and_break(base: str, rep: Report, mock_base: str, pair: tuple[str, str]) -> None:
    """连 /ws/topo → 断一条链路 → 期望收到 edges_removed；恢复 → edges_added。

    这是 FR-14「断链 10s 内变红」的服务端一半；能不能真变红还得开浏览器看。
    """
    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as exc:  # 没装 websockets 不算失败
        rep.skip("WS 断链推送", f"未安装 websockets（{exc}），跳过")
        return

    uri = base.replace("http://", "ws://").rstrip("/") + "/ws/topo"
    # websockets>=14 的 connect() 是上下文管理器（直接 .recv 会告警），老版本没有 proxy 参数。
    try:
        cm = ws_connect(uri, proxy=None, open_timeout=5)
    except TypeError:
        cm = ws_connect(uri, open_timeout=5)
    except Exception as exc:
        rep.fail("WS 握手", f"{type(exc).__name__}: {exc}")
        return

    a, b = pair
    try:
        with cm as conn:
            rep.ok("WS 握手", uri)
            status, body = http("POST", f"{mock_base}/_mock/link", {"a": a, "b": b, "up": False})
            if status != 200:
                rep.fail("mock 断链", f"HTTP {status}: {body[:120]}")
                return
            t0 = time.time()
            try:
                msg = json.loads(conn.recv(timeout=15))
            except Exception as exc:
                rep.fail("断链推送", f"15s 内没收到推送：{type(exc).__name__}: {exc}")
                return
            removed = (msg.get("diff") or {}).get("edges_removed") or []
            cost = time.time() - t0
            if removed:
                rep.ok("断链推送", f"{cost:.1f}s 收到 edges_removed={removed}")
            else:
                rep.fail("断链推送", f"收到了推送但 edges_removed 为空：{json.dumps(msg)[:140]}")

            state, _ = get_state(base)
            seen = (state or {}).get("last_diff") or {}
            if seen.get("edges_removed"):
                rep.ok("/api/diff 同步", f"edges_removed={seen['edges_removed']}")
            else:
                rep.fail("/api/diff 同步", "last_diff 里没有断链记录")

            http("POST", f"{mock_base}/_mock/link", {"a": a, "b": b, "up": True})
            try:
                msg = json.loads(conn.recv(timeout=15))
                added = (msg.get("diff") or {}).get("edges_added") or []
                (rep.ok if added else rep.fail)("恢复推送", f"edges_added={added}")
            except Exception as exc:
                rep.fail("恢复推送", f"没收链路恢复的推送：{type(exc).__name__}: {exc}")
    except Exception as exc:
        rep.fail("WS 会话", f"{type(exc).__name__}: {exc}")
    finally:
        # 无论上面哪一步失败，链路都要恢复，否则后面的人看到的是断网环境。
        http("POST", f"{mock_base}/_mock/link", {"a": a, "b": b, "up": True})


def main() -> int:
    parser = argparse.ArgumentParser(description="大脑端到端自检（docs/03 N4）")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="大脑地址")
    parser.add_argument("--mock", default=None, help="mock ONOS 地址，例如 http://127.0.0.1:8181")
    parser.add_argument("--route", nargs=2, metavar=("SRC", "DST"), help="额外做一次下发/删除闭环")
    parser.add_argument("--expect-devices", type=int, default=None)
    parser.add_argument("--expect-hosts", type=int, default=None)
    parser.add_argument("--break-pair", nargs=2, metavar=("DPID_A", "DPID_B"), default=None,
                        help="mock 模式下要断开的链路，默认 of:...2 与 of:...3")
    parser.add_argument("--wait", type=float, default=20.0, help="等 onos_ok 的最长秒数")
    parser.add_argument("--strict", action="store_true", help="把 SKIP 也当成不完整（退出码 2）")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    mock_base = args.mock.rstrip("/") if args.mock else None
    rep = Report()

    print(f"自检目标: {base}" + (f"  (mock ONOS: {mock_base})" if mock_base else ""))
    print()

    state, waited = wait_onos_ok(base, args.wait)
    if state is None:
        rep.fail("等 onos_ok", f"{args.wait:.0f}s 内没变成 True，先看 ./run.sh check")
    else:
        rep.ok("等 onos_ok", f"{waited:.1f}s 内就绪")

    check_basics(base, rep, args)

    if args.route:
        check_route(base, rep, args.route[0], args.route[1], mock_base)
    else:
        rep.skip("下发/删除闭环", "未加 --route SRC DST")

    if mock_base:
        pair = tuple(args.break_pair) if args.break_pair else ("of:0000000000000002", "of:0000000000000003")
        check_ws_and_break(base, rep, mock_base, pair)  # type: ignore[arg-type]
    else:
        rep.skip("WS 断链推送", "未加 --mock（真网络断链请按 README 手动做）")

    print()
    n_pass, n_fail, n_skip = rep.dump()
    print()
    print(f"合计: {n_pass} 通过 / {n_fail} 失败 / {n_skip} 跳过")
    if n_fail:
        return 1
    if n_skip and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
