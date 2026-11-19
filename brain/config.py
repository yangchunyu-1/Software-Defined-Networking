"""甲 · 感知层：环境常量。

唯一事实来源是 docs/02_接口契约与数据结构.md 第 1 节。
**任何人不得在其他文件里硬编码 IP / 端口 / 凭据**，一律从这里 import。

docs/03 硬口径 2 / 6：APP_ID、POLL_INTERVAL、ONOS_USER/PASS 只在本文件出现一次。
"""

ONOS_HOST = "127.0.0.1"
ONOS_PORT = 8181
ONOS_BASE = f"http://{ONOS_HOST}:{ONOS_PORT}/onos/v1"
ONOS_USER = "karaf"  # REST 用 karaf/karaf，不是 Web GUI 的 onos/rocks
ONOS_PASS = "karaf"
APP_ID = "org.sdn.brain"  # 所有通信对共用一个 appId，所以不能按 appId 批量删流表
HTTP_TIMEOUT = 3  # 秒
POLL_INTERVAL = 5  # 秒
WEB_PORT = 8000

OFP_PORT = 6653  # OpenFlow，ONOS 监听
CLI_PORT = 8101  # SSH CLI

# 流表常量：docs/03 硬口径 2 要求集中在这里，不要在 flow.py 里重写。
FLOW_PRIORITY = 40000
FLOW_TIMEOUT = 0
FLOW_IS_PERMANENT = True

# --- S2 链路时延计算（只追加，不改动上面任何一行）-------------------------
# 算法参数，不是环境值；环境值仍只在本文件前半部分出现一次。
# 精度口径（docs/03 §11.4）：近似值，非插件级测量。
DELAY_INTERVAL_S        = 5.0          # 采集周期（秒）
DELAY_WINDOW            = 10           # 中位数窗口长度
DELAY_EWMA_ALPHA        = 0.3          # 对窗口中位数再做一次指数加权
DELAY_BASE_PROP_MS      = 0.2          # P1 不可得时的传播时延基线（ms）
DELAY_DEFAULT_CAP_BPS   = 10_000_000   # portSpeed 缺失/为 0 时的链路容量兜底
DELAY_AVG_PKT_BYTES     = 1000         # M/M/1 排队近似用的平均包长
DELAY_STALE_AFTER_S     = 15.0         # 超过这个时间没刷新就标 stale
DELAY_REF_MS            = 1.0          # weight 归一化基准，S3 可调
DELAY_MIN_INTERVAL_S    = 2.0          # 计数差分的最小 Δt，过短直接丢弃本轮
DELAY_PORT_SPEED_TO_BPS = 1000         # ONOS portSpeed 单位换算系数（Kbps -> bps）
                                       # ⚠ 数量级依据见 samples/SCHEMA_NOTES.md §2；
                                       #    官方单位换算仍未实测，缺失时回落默认容量。
DELAY_MIN_DELAY_MS      = 0.01         # 时延下限，保证 Dijkstra 权重非负且不全 0
DELAY_UTIL_CAP          = 0.98         # 利用率裁剪上限，避免 1/(1-u) 发散

WEIGHT_MODE             = "hop"        # "hop" = 一阶段按跳数；"delay" = S2 按时延注入
