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
