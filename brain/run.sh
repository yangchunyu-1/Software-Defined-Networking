#!/usr/bin/env bash
# 丙-6 一键启动 / 预检 / 自检（docs/03 §6 丙-6；判据：另一个人照 README 盲跑能通）
#
#   ./run.sh check    环境预检，不启动任何东西
#   ./run.sh start    预检后前台启动大脑（Ctrl-C 停）
#   ./run.sh test     跑 pytest
#   ./run.sh smoke    对正在跑的服务做自检（加 --route SRC DST 才下发/撤销一对）
#
# 设计约束（照 docs/03 口径）：
#   * 端口、账号一律从 config.py 读，脚本里不出现第二份 8181/karaf（口径 6）。
#   * 只做「检查 + 启动 + 断言」，不碰乙的算法、不改运行参数。
#   * 判据只建立在**真实 ONOS + Mininet** 上：本脚本不提供模控，连不上就如实报红。
#     假环境里跑出来的绿是负资产（docs/03 口径 8：只实测、不推理）。

set -uo pipefail

BRAIN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

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

# ---------------------------------------------------------------- check

MISSING_DEPS=0
ONOS_OK=0
ONOS_TOTAL=0
ONOS_LIVE=0
ONOS_HOSTS=0

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
        && ok "websockets 已安装（smoke 的 WS 检查要用）" \
        || { warn "未安装 websockets，smoke 会跳过 WS 那一项"; hint "pip install -r brain/requirements.txt"; }
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
    # 三个数字都要：设备总数、available 数、带 IP 的主机数。
    # 只看总数会把「Mininet 已经断开、ONOS 还留着僵尸设备」误报成正常。
    out="$(pybrain - <<'EOF' 2>&1
import base64, json, sys, urllib.request
import config

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
token = base64.b64encode(f"{config.ONOS_USER}:{config.ONOS_PASS}".encode()).decode()

def get(path):
    req = urllib.request.Request(config.ONOS_BASE + path)
    req.add_header("Authorization", "Basic " + token)
    with opener.open(req, timeout=config.HTTP_TIMEOUT) as resp:
        return json.load(resp)

try:
    devices = get("/devices").get("devices", [])
    hosts = get("/hosts").get("hosts", [])
except Exception as exc:
    print("ERR " + type(exc).__name__ + ": " + str(exc))
    sys.exit(1)

alive = sum(1 for d in devices if d.get("available"))
with_ip = sum(1 for h in hosts if h.get("ipAddresses"))
print("OK %d %d %d" % (len(devices), alive, with_ip))
EOF
)"; rc=$?

    if [ "$rc" -ne 0 ]; then
        bad "ONOS 北向不可达（$ONOS_BASE）"
        hint "${out#ERR }"
        hint "排查顺序：docker ps → curl -u karaf:karaf $ONOS_BASE/devices → 再跑本命令"
        hint "判据只认真实环境，本仓库不提供模拟控制器"
        return 1
    fi

    # shellcheck disable=SC2086
    set -- ${out#OK }
    ONOS_TOTAL="$1"; ONOS_LIVE="$2"; ONOS_HOSTS="$3"
    ONOS_OK=1

    if [ "$ONOS_LIVE" -gt 0 ]; then
        ok "ONOS REST 可达，$ONOS_LIVE/$ONOS_TOTAL 台交换机在线，$ONOS_HOSTS 台主机有 IP"
    else
        bad "ONOS 上有 $ONOS_TOTAL 台设备，但 0 台在线（available=false）"
        hint "这是僵尸记录：Mininet 连过一次又断了，或根本没起"
        hint "起拓扑：sudo mn --topo=tree,2,2 --controller=remote,ip=127.0.0.1,port=6653 --mac --switch=ovs,protocols=OpenFlow13"
    fi

    if [ "$ONOS_LIVE" -gt 0 ] && [ "$ONOS_HOSTS" -eq 0 ]; then
        warn "0 台主机有 IP：Mininet 里还没 pingall，主机表建不出来"
        hint "mininet> pingall    （不做这步 /hosts 的 ipAddresses 是空数组）"
    fi

    if [ "$ONOS_LIVE" -gt 0 ]; then
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
    if [ "$ONOS_OK" -eq 1 ] && [ "$ONOS_LIVE" -gt 0 ] && [ "$ONOS_HOSTS" -gt 0 ]; then
        ok "可以启动：./run.sh start   （另开终端看页面 http://<本机IP>:$WEB_PORT）"
        return 0
    fi
    if [ "$ONOS_OK" -eq 1 ] && [ "$ONOS_LIVE" -gt 0 ]; then
        warn "交换机在线，但主机表还是空的——先 pingall 再 ./run.sh start"
        return 0
    fi
    if [ "$ONOS_OK" -eq 1 ]; then
        warn "ONOS 在，但没有一台交换机在线——先起 Mininet，再 ./run.sh check"
        return 0
    fi
    warn "ONOS 不可达：进程仍能启动（轮询失败但不会崩，NF-03），但这不是可验收状态"
    hint "先把 ONOS 容器与 Mininet 起起来——所有判据都要在真实拓扑上取"
    return 0
}

# ---------------------------------------------------------------- 各命令

cmd_start() {
    pick_python
    cmd_check >/dev/null 2>&1 || true
    info ""
    info "启动大脑：$PY main.py  (Ctrl-C 停止)"
    info "自检（另一个终端）：cd brain && ./run.sh smoke"
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

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
    local cmd="${1:-}"
    [ $# -gt 0 ] && shift || true
    case "$cmd" in
        check) cmd_check "$@" ;;
        start) cmd_start "$@" ;;
        test)  cmd_test "$@" ;;
        smoke) cmd_smoke "$@" ;;
        ""|-h|--help|help) usage ;;
        *) bad "不认识的命令：$cmd"; info ""; usage; return 2 ;;
    esac
}

main "$@"
