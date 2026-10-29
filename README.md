# SDN 网络大脑 · Software-Defined-Networking

跑在 **ONOS 2.7.0** 之上的应用平面程序：周期性通过北向 REST 拉取拓扑 → 建成字典邻接表 → 两轮比对出变化
→ Dijkstra 算最短路 → 把 OpenFlow 流表 POST 回数据平面 → 拓扑变了自动重算受影响路径。

> **状态：开发中。** 进度看闸门表 [`docs/03`](docs/03_三人分工与开发计划.md)。
> 本 README 里标 ⏳ 的命令要等 **N4 骨架贯通** 之后才真正可用。

---

## 目录结构

```
Software-Defined-Networking/
├── README.md            ← 本文件
├── docs/                ← 设计文档（三人共用）
│   ├── README.md            文档索引
│   ├── 01_技术选型报告.md
│   ├── 02_接口契约与数据结构.md     ★ 冻结件，改接口先改它
│   ├── 03_三人分工与开发计划.md     ★ N0–N7 闸门 + 十条硬口径
│   └── 04_范围与需求冻结.md         ★ 做什么 / 不做什么 / 怎么算做完
└── brain/               ← 代码根，所有 python 都在这
    ├── config.py            常量（唯一一处）
    ├── onos.py              同步 REST（requests）
    ├── onos_async.py        异步 REST（aiohttp，主链路）
    ├── topo.py              建图 / 主机表 / 快照 / diff
    ├── path.py              Dijkstra / 最短路径树 / 组播树
    ├── flow.py              流表构造 / 下发 / 删除 / 重算
    ├── group.py             组表（ovs-ofctl）
    ├── main.py              Sanic 服务 / 路由 / 轮询挂载
    ├── static/index.html    拓扑可视化（零 CDN、零框架）
    ├── samples/             真实 ONOS 响应（乙丙的测试数据）
    ├── tests/
    └── run.sh               一键启动
```

## 先读哪个文档

| 你要干什么 | 读哪个 |
|---|---|
| 写代码，要知道函数怎么调 | [`docs/02`](docs/02_接口契约与数据结构.md) |
| 要知道做到什么程度算完、什么不做 | [`docs/04`](docs/04_范围与需求冻结.md) |
| 要知道自己负责哪块、现在该干什么 | [`docs/03`](docs/03_三人分工与开发计划.md) |
| 被问「为什么用它不用 XX」 | [`docs/01`](docs/01_技术选型报告.md) |

---

## 环境前提

三个人用同一套版本，跑出来的结果才能互相验证。**版本号是要求，不是某台机器的记录。**

| 项 | 要求 |
|---|---|
| WSL2 | Ubuntu 22.04，systemd 可用（Mininet / OVS 需要） |
| Python | 3.10；`requests` / `sanic` / `aiohttp` 装在用户目录（`pip install --user`） |
| Mininet | 2.3.0 |
| Open vSwitch | 2.17.x，datapath 用 `system` |
| Docker | 用于跑 ONOS 容器；ONOS 2.7.0 镜像需提前拉到本地（离线可用） |
| ONOS | 2.7.0，容器名 `onos`，建议 `--restart=always` |

**端口与账号**（两套账号别搞混）

| 入口 | 端口 | 账号 / 口令 |
|---|---|---|
| OpenFlow（Mininet 连这个） | 6653 | — |
| REST API | 8181 | **`karaf` / `karaf`** |
| SSH CLI | 8101 | **`karaf` / `karaf`** |
| Web GUI | 8181 | `onos` / `rocks` |

---

## 快速开始

代码在仓库 `brain/` 下，WSL 里通过 `/mnt/d` 访问。先做一条软链接省得敲长路径：

```bash
REPO=/mnt/d/repos/Software-Defined-Networking      # 换成你自己的仓库路径
ln -sfn "$REPO/brain" ~/brain
```

**终端 A · ONOS**

```bash
docker ps --filter name=onos
# 没起就：
docker start onos
```

**终端 B · Mininet**（必须在 ONOS 起来之后）

```bash
sudo mn -c
sudo mn --topo=tree,2,2 --controller=remote,ip=127.0.0.1,port=6653 --mac --switch=ovs,protocols=OpenFlow13
```
进去后先 `pingall` —— **不做这步主机的 `ipAddresses` 是空的**，主机表建不出来。

**关掉默认转发应用 `fwd`**（每次 ONOS 容器重启后都要重做）

```bash
ssh -p 8101 karaf@localhost -o HostKeyAlgorithms=+ssh-rsa     # 密码 karaf
app deactivate org.onosproject.fwd
app list | grep fwd
```
判据：关掉后网络应该**立刻不通**。还通 = 没关干净，后面所有结果都不可信。

**终端 C · 大脑** ⏳ 待 N4

```bash
cd ~/brain && python3 main.py
```

---

## 自检（看这个，不看「启动成功」日志）

⏳ 待 N4 可用。

```bash
curl -s localhost:8000/api/state | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d['graph']),'switches',len(d['hosts']),'hosts')"
```

判据：`3 switches 4 hosts`（`tree,2,2` 拓扑）。
**框架打印「服务已启动」不代表连上了 ONOS**——只认这个数字。

排查顺序：`docker ps` → `curl 8181/onos/v1/devices` → `pingall` → `curl /api/state.onos_errors`。

---

## 常见问题（本项目已踩过的坑）

| 现象 | 原因 | 处理 |
|---|---|---|
| REST 返回 **401** | 用了 GUI 的 `onos/rocks` | REST 是 `karaf/karaf` + HTTPBasicAuth |
| REST 返回 **404** | 先别怀疑接口不存在 | 依次查：容器在跑 → 端口映射在 → 拓扑已连 → endpoint 拼写 |
| 关了 `fwd` 还能 ping 通 | 没关干净，或有别的 app 在转发 | `app list` 核对，必要时重启容器再来 |
| `/hosts` 里 `ipAddresses` 是 `[]` | 主机还没发过包 | 先 `pingall` |
| 链路数量比物理链路多一倍 | `/links` 双向各报一次 | 建图按有向存，做 diff 用 `frozenset` 归一化 |
| 下发流表 400 | `appId` 写进 body 了 | `appId` 是 **query 参数**：`POST /flows/{id}?appId=...` |
| 删流表删掉了别人的 | 用了 `DELETE /flows/application/{appId}` | 本项目禁用，只能按 `flowId` 精确删 |
| WSL 里 `检测到 localhost 代理配置但未镜像` | 正常现象 | 忽略 |

---

## 协作约定

1. **一个文件一个作者**，不许两人并行改同一模块再合并。
2. **改接口先改 [`docs/02`](docs/02_接口契约与数据结构.md)**，群里知会后再动代码。N3 冻结之后只增不改。
3. **跑通了才算做完**：`import` 不报错不算，必须有一条真实命令 + 真实输出。
4. **没实测的东西标 `❓`**，不许在报告或演示里当结论讲。
5. 提交前自查：`cd brain && python3 -m compileall -q .`

## 技术路线一句话

大脑说 **HTTP/JSON**（北向），ONOS 说 **OpenFlow 1.3**（南向）。大脑不直接跟交换机打交道——
这正是控制平面与数据平面分离、北向接口可编程的实证。

---

## 协作者打卡

- Aletheia06：Hello this is Aletheia06
