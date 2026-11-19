#!/usr/bin/env bash
# 丙-6 一键启动 / 预检 / 自检（docs/03 §6 丙-6；判据：另一个人照 README 盲跑能通）
#
#   ./run.sh check    环境预检，不启动任何东西
#   ./run.sh start    预检后前台启动大脑（Ctrl-C 停）
#   ./run.sh test     跑 pytest
#   ./run.sh smoke    对正在跑的服务做只读自检（加 --route SRC DST 才下发）
#   ./run.sh demo     不需要 ONOS/Docker：假 ONOS + 大脑 + 全流程自检（含断链推送）
#   ./run.sh stop     停掉 demo 起的后台进程
#
# 设计约束（照 docs/03 口径）：
#   * 端口、账号一律从 config.py 读，脚本里不出现第二份 8181/karaf（口径 6）。
#   * 只做「检查 + 启动 + 断言」，不碰乙的算法、不改运行参数。
#   * 任何检查失败都打印「下一步敲什么」，盲跑的人不用回来问。

set -uo pipefail

BRAIN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$BRAIN_DIR/.run"
ONOS_MOCK_LOG="$RUN_DIR/mock_onos.log"
BRAIN_LOG="$RUN_DIR/brain.log"

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_DIM=""; C_OFF=""
fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$C_YEL" "$C_OFF" "$*"; }
bad()  { printf '%s[FAIL]%s %s\n' "$C_RED" "$C_OFF" "$*"; }
hint() { printf '%s       %s%s\n' "$C_DIM" "$*" "$C_OFF"; }
die()  { bad "$*"; exit 1; }

# ---------------------------------------------------------------- Python

pick_python() {
    if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"; return; fi
    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
}

# 在 brain/ 下跑一段 python（能 import config，脚本里就不用抄端口）
pybrain() { ( cd "$BRAIN_DIR" && "$PY" "$@" ); }

read_config() {
    pybrain - <<'EOF'
import config
print(config.WEB_PORT, config.ONOS_HOST, config.ONOS_PORT, config.ONOS_BASE)
EOF
}

# ---------------------------------------------------------------- 小工具

# 端口有没有人在听。用 python 而不是 ss/netstat：WSL、macOS、Git Bash 都有 python，
# 但 ss 在 macOS 没有、netstat 的输出格式各平台还不一样。
listening() {
    "$PY" - "$1" <<'EOF'
import socket, sys
s = socket.socket()
s.settimeout(0.4)
try:
    sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
finally:
    s.close()
EOF
}

wait_http() {  # wait_http <url> <秒>
    "$PY" - "$1" "$2" <<'EOF'
import sys, time, urllib.request
url, limit = sys.argv[1], float(sys.argv[2])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
end = time.time() + limit
while time.time() < end:
    try:
        with opener.open(url, timeout=2) as r:
            if r.status < 500:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(0.4)
sys.exit(1)
EOF
}

stop_pidfile() {  # stop_pidfile <pidfile> <名字>
    local f="$1" name="$2" pid
    [ -f "$f" ] || return 0
    pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        ok "已停止 $name (pid $pid)"
    else
        warn "$name 的进程已经不在了（pid ${pid:-?}），只清理 pid 文件"
    fi
    rm -f "$f"
}

# ---------------------------------------------------------------- check

MISSING_DEPS=0
ONOS_OK=0
ONOS_DEVICES=0

check_python() {
    local ver
    ver="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || {
        bad "找不到可用的 python（试过 python3 / python）"; hint "装 Python 3.10+ 后重跑"; return 1; }
    if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        ok "Python $ver（$PY）"
    else
        bad "Python $ver 太旧，README 要求 3.10+"; return 1
    fi
}

check_deps() {
    local out
    if out="$(pybrain - <<'EOF' 2>&1
import sanic, aiohttp
print(f"sanic {sanic.__version__} / aiohttp {aiohttp.__version__}")
EOF
)"; then
        ok "运行依赖 $out"
    else
        MISSING_DEPS=1
        bad "缺运行依赖（sanic / aiohttp）"
        hint "pip install -r brain/requirements.txt"
        return 1
    fi

    out="$(pybrain - <<'EOF' 2>&1
import pytest, sanic_testing
print(f"pytest {pytest.__version__} / sanic-testing {getattr(sanic_testing, '__version__', '?')}")
EOF
)"; if [ $? -eq 0 ]; then
        ok "测试依赖 $out"
    else
        warn "缺测试依赖（pytest / sanic-testing），run.sh test 会失败"
        hint "pip install -r brain/requirements.txt"
    fi

    pybrain -c 'import websockets' >/dev/null 2>&1 \
        && ok "websockets 已安装（run.sh demo 的 WS 检查要用）" \
        || { warn "未安装 websockets，demo 会跳过 WS 推送检查"; hint "pip install -r brain/requirements.txt"; }
}

check_files() {
    local miss=0 f
    for f in main.py config.py flow.py path.py topo.py onos_async.py static/index.html; do
        [ -f "$BRAIN_DIR/$f" ] || { bad "缺文件 brain/$f"; miss=1; }
    done
    [ "$miss" -eq 0 ] && ok "代码文件齐全（main/config/flow/path/topo/onos_async/static）"
    return "$miss"
}

check_ports() {
    local WEB_PORT="$1"
    if listening "$WEB_PORT"; then
        warn "端口 $WEB_PORT 已在监听——可能已经有一个大脑在跑"
        hint "要用现成的：./run.sh smoke；要重开：先杀掉旧进程"
    else
        ok "端口 $WEB_PORT 空闲"
    fi
}

check_onos() {
    local ONOS_BASE="$1" out rc
    out="$(pybrain - <<'EOF' 2>&1
import base64, json, sys, urllib.request
import config

req = urllib.request.Request(config.ONOS_BASE + "/devices")
token = base64.b64encode(f"{config.ONOS_USER}:{config.ONOS_PASS}".encode()).decode()
req.add_header("Authorization", "Basic " + token)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(req, timeout=config.HTTP_TIMEOUT) as resp:
        devices = json.load(resp).get("devices", [])
except Exception as exc:
    print("ERR " + type(exc).__name__ + ": " + str(exc))
    sys.exit(1)
print("OK %d" % len(devices))
EOF
)"; rc=$?

    if [ "$rc" -ne 0 ]; then
        bad "ONOS 北向不可达（$ONOS_BASE）"
        hint "${out#ERR }"
        hint "排查顺序：docker ps → curl -u karaf:karaf $ONOS_BASE/devices → 再跑本命令"
        hint "没有 ONOS 也能看效果：./run.sh demo（假 ONOS，不碰真网络）"
        return 1
    fi

    ONOS_DEVICES="${out#OK }"
    ONOS_OK=1
    ok "ONOS REST 可达，$ONOS_DEVICES 台设备"
    if [ "$ONOS_DEVICES" -eq 0 ]; then
        warn "ONOS 上 0 台交换机：Mininet 还没连控制器"
        hint "终端 B：sudo mn --topo=tree,2,2 --controller=remote,ip=127.0.0.1,port=6653 --mac --switch=ovs,protocols=OpenFlow13"
    else
        hint "别忘了关 fwd（否则 ping 通了也不是大脑的功劳）：ssh -p 8101 karaf@localhost → app deactivate org.onosproject.fwd"
    fi
}

cmd_check() {
    pick_python
    local cfg; cfg="$(read_config 2>/dev/null)" || die "无法 import brain/config.py，先确认仓库完整"
    # shellcheck disable=SC2086
    set -- $cfg
    local WEB_PORT="$1" ONOS_HOST="$2" ONOS_PORT="$3" ONOS_BASE="$4"

    info "== 1/4 Python 与依赖 =="
    check_python || true
    check_deps || true
    check_files || true

    info ""
    info "== 2/4 端口 =="
    check_ports "$WEB_PORT"

    info ""
    info "== 3/4 ONOS（$ONOS_HOST:$ONOS_PORT）=="
    check_onos "$ONOS_BASE" || true

    info ""
    info "== 4/4 结论 =="
    if [ "$MISSING_DEPS" -eq 1 ]; then
        bad "依赖没装齐，先 pip install -r brain/requirements.txt"
        return 1
    fi
    if [ "$ONOS_OK" -eq 1 ] && [ "$ONOS_DEVICES" -gt 0 ]; then
        ok "可以启动：./run.sh start   （另开终端看页面 http://<本机IP>:$WEB_PORT）"
        return 0
    fi
    if [ "$ONOS_OK" -eq 1 ]; then
        warn "ONOS 在，但拓扑是空的——先起 Mininet 并 pingall，再 ./run.sh start"
        return 0
    fi
    warn "ONOS 不可达：大脑仍能启动（轮询会一直失败但进程不崩，NF-03）"
    hint "只想看页面/联调逻辑：./run.sh demo"
    return 0
}

# ---------------------------------------------------------------- 各命令

cmd_start() {
    pick_python
    cmd_check >/dev/null 2>&1 || true
    info ""
    info "启动大脑：$PY main.py  (Ctrl-C 停止)"
    info "自检（另一个终端）：curl -s localhost:8000/api/state | head -c 200"
    cd "$BRAIN_DIR" || die "进不去 $BRAIN_DIR"
    exec "$PY" main.py
}

cmd_test() {
    pick_python
    cd "$BRAIN_DIR" || die "进不去 $BRAIN_DIR"
    info "pytest tests/ （口径：全绿才算做完）"
    exec "$PY" -m pytest tests/ -q -p no:cacheprovider "$@"
}

cmd_smoke() {
    pick_python
    cd "$BRAIN_DIR" || die "进不去 $BRAIN_DIR"
    exec "$PY" tools/smoke.py "$@"
}

cmd_stop() {
    stop_pidfile "$RUN_DIR/brain.pid" "大脑"
    stop_pidfile "$RUN_DIR/mock_onos.pid" "假 ONOS"
    rmdir "$RUN_DIR" 2>/dev/null || true
}

cmd_demo() {
    pick_python
    local keep=1
    local -a smoke_args=()
    for a in "$@"; do
        case "$a" in
            --no-keep) keep=0 ;;
            *) smoke_args+=("$a") ;;
        esac
    done

    local cfg; cfg="$(read_config 2>/dev/null)" || die "无法 import brain/config.py"
    # shellcheck disable=SC2086
    set -- $cfg
    local WEB_PORT="$1" ONOS_PORT="$3" ONOS_BASE="$4"

    if listening "$ONOS_PORT"; then
        die "端口 $ONOS_PORT 已被占用（可能是真的 ONOS 在跑）。真环境请用 ./run.sh start + ./run.sh smoke"
    fi
    if listening "$WEB_PORT"; then
        die "端口 $WEB_PORT 已被占用（已有大脑在跑）。先 ./run.sh stop 或杀掉旧进程"
    fi

    mkdir -p "$RUN_DIR"
    info "== demo：假 ONOS + 大脑 + 全流程自检（不依赖 Docker/WSL）=="
    info ""

    ( cd "$BRAIN_DIR" && exec "$PY" tools/mock_onos.py >"$ONOS_MOCK_LOG" 2>&1 ) &
    echo $! >"$RUN_DIR/mock_onos.pid"
    if ! wait_http "http://127.0.0.1:$ONOS_PORT/_mock/state" 10; then
        bad "假 ONOS 没起来，日志：$ONOS_MOCK_LOG"
        tail -n 15 "$ONOS_MOCK_LOG" 2>/dev/null
        cmd_stop
        return 1
    fi
    ok "假 ONOS 就绪（$ONOS_BASE，4 交换机 / 5 链路 / 6 主机）"

    ( cd "$BRAIN_DIR" && exec "$PY" main.py >"$BRAIN_LOG" 2>&1 ) &
    echo $! >"$RUN_DIR/brain.pid"
    if ! wait_http "http://127.0.0.1:$WEB_PORT/api/state" 15; then
        bad "大脑没起来，日志：$BRAIN_LOG"
        tail -n 20 "$BRAIN_LOG" 2>/dev/null
        cmd_stop
        return 1
    fi
    ok "大脑就绪（http://127.0.0.1:$WEB_PORT）"

    info ""
    local rc=0
    ( cd "$BRAIN_DIR" && "$PY" tools/smoke.py \
        --base "http://127.0.0.1:$WEB_PORT" \
        --mock "http://127.0.0.1:$ONOS_PORT" \
        --expect-devices 4 --expect-hosts 6 \
        --route 10.0.0.1 10.0.0.3 \
        "${smoke_args[@]+"${smoke_args[@]}"}" ) || rc=$?

    info ""
    if [ "$rc" -eq 0 ]; then
        ok "demo 全流程通过"
    else
        bad "demo 有失败项（退出码 $rc），看上面 [FAIL] 行和 $BRAIN_LOG"
    fi

    if [ "$keep" -eq 1 ] && [ "$rc" -eq 0 ]; then
        info ""
        info "服务仍在后台跑，浏览器打开 http://127.0.0.1:$WEB_PORT 看拓扑："
        hint "页面里断链看不到效果，要断链就：curl -s -X POST localhost:$ONOS_PORT/_mock/link -H 'Content-Type: application/json' -d '{\"a\":\"of:0000000000000002\",\"b\":\"of:0000000000000003\",\"up\":false}'"
        hint "停止：./run.sh stop        日志：brain/.run/brain.log"
    else
        cmd_stop
    fi
    return "$rc"
}

usage() {
    sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
    local cmd="${1:-}"
    [ $# -gt 0 ] && shift || true
    case "$cmd" in
        check) cmd_check "$@" ;;
        start) cmd_start "$@" ;;
        test)  cmd_test "$@" ;;
        smoke) cmd_smoke "$@" ;;
        demo)  cmd_demo "$@" ;;
        stop)  cmd_stop ;;
        ""|-h|--help|help) usage ;;
        *) bad "不认识的命令：$cmd"; info ""; usage; return 2 ;;
    esac
}

main "$@"
