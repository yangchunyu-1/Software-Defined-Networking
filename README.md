# SDN 网络大脑 · Software-Defined-Networking

跑在 **ONOS 2.7.0** 之上的应用平面程序：周期性通过北向 REST 拉取拓扑 → 建成字典邻接表 → 两轮比对出变化
→ Dijkstra 算最短路 → 把 OpenFlow 流表 POST 回数据平面 → 拓扑变了自动重算受影响路径。

> **状态：开发中。** 进度看闸门表 [`docs/03`](docs/03_三人分工与开发计划.md)。
> 启动与自检命令已可用（`brain/run.sh`），各闸门是否达成以 `docs/03` 的勾选状态为准。

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
│   ├── 04_范围与需求冻结.md         ★ 做什么 / 不做什么 / 怎么算做完
│   └── 05_异步协作约定.md           ★ 节奏 / 分支与 PR / 契约测试
├── labs/                ← 实验指导书（docx 原件）
│   ├── 实验指导 - 传统网络二层交换与软件定义网络数据转发分析对比.docx
│   ├── 实验指导 - 基于OVS与Mininet的软件定义网络数据平面仿真基础.docx
│   ├── 实验指导 - 基于ONOS的软件定义网络数据平面配置管理.docx
│   ├── 实验指导 - 基于ONOS北向接口的软件定义网络数据平面管理.docx
│   ├── 实验指导 - 基于OpenFlow协议流表、组表的软件定义网络数据转发.docx
│   ├── 实验指导 - 基于Wireshark的SDN南向协议OpenFlow通信分析.docx
│   └── 实验指导 - 面向ONOS北向接口的应用平面服务编程与接口封装.docx
└── brain/               ← 代码根，所有 python 都在这
    ├── config.py            常量（唯一一处）
    ├── onos_async.py        唯一 REST 客户端（aiohttp）
    ├── topo.py              建图 / 主机表 / 快照 / diff
    ├── path.py              Dijkstra / 最短路径树 / 组播树
    ├── flow.py              流表构造 / 下发 / 删除 / 重算
    ├── delay.py             链路时延表（S2，默认不参与选路）
    ├── group.py             组表（ovs-ofctl）
    ├── main.py              Sanic 服务 / 路由 / 轮询挂载
    ├── static/index.html    拓扑可视化（零 CDN、零框架）
    ├── samples/             真实 ONOS 响应（只能是真的，不许手编）
    ├── tests/               契约测试（每人为自己的模块写一份）
    │   └── fixtures/        手写样本，结构照 docs/02 第 4 节
    ├── tools/
    │   ├── smoke.py         端到端自检（N4 判据的脚本化，只认 /api/state 的数字）
    │   └── mock_onos.py     假 ONOS（本地联调用，**不是控制器**，见下文「没有 ONOS 也能跑」）
    ├── requirements.txt     依赖固定版本
    ├── run.sh               一键：check / start / test / smoke / demo / stop
    └── .run/                运行期 pid 与日志（git 忽略）
```

## 先读哪个文档

| 你要干什么 | 读哪个 |
|---|---|
| 写代码，要知道函数怎么调 | [`docs/02`](docs/02_接口契约与数据结构.md) |
| 要知道做到什么程度算完、什么不做 | [`docs/04`](docs/04_范围与需求冻结.md) |
| 要知道自己负责哪块、现在该干什么 | [`docs/03`](docs/03_三人分工与开发计划.md) |
| 要知道怎么和别人异步配合（分支、PR、测试） | [`docs/05`](docs/05_异步协作约定.md) |
| 被问「为什么用它不用 XX」 | [`docs/01`](docs/01_技术选型报告.md) |

**协作节奏**：每周四晚碰头一次对齐闸门，其余时间全部异步。见 [`docs/05`](docs/05_异步协作约定.md)。

---

## 环境前提

三个人用同一套版本，跑出来的结果才能互相验证。**版本号是要求，不是某台机器的记录。**

| 项 | 要求 |
|---|---|
| WSL2 | Ubuntu 22.04，systemd 可用（Mininet / OVS 需要） |
| Python | 3.10；`sanic` / `aiohttp` 装在用户目录（`pip install --user`） |
| 测试依赖 | `pytest` 与 `sanic-testing`（`app.test_client` 由它提供），同样装在用户目录；版本要求见 [`docs/01` §3](docs/01_技术选型报告.md) |
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

**第 0 步 · 装依赖（每台机器一次）**

```bash
pip install --user -r ~/brain/requirements.txt
cd ~/brain && ./run.sh check
```

`check` 不做任何启动，只报四项：Python 与依赖、端口、ONOS 北向、代码文件。
**报红的每一行下面都跟着「下一步敲什么」**，照抄即可。没有 ONOS 时最后会提示
`./run.sh demo`——先用它确认代码是好的，再去折腾环境。

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

**终端 C · 大脑**

```bash
cd ~/brain && ./run.sh start          # 等价于 python3 main.py，但会先跑一遍预检
```

---

## 自检（看这个，不看「启动成功」日志）

```bash
cd ~/brain && ./run.sh smoke
```

正常输出（`tree,2,2` 拓扑、已 `pingall`、`fwd` 已关）：

```
  [PASS] 等 onos_ok      0.0s 内就绪
  [PASS] 页面 GET /       200, 21956B, 含 <svg>
  [PASS] 字段数 14        与 docs/02 §6.1 完全一致
  [PASS] onos_ok         True
  [PASS] 轮询已在跑       poll_count=2, poll_ms=3
  [PASS] 拓扑规模         3 switches / 4 hosts
  [SKIP] 下发/删除闭环    未加 --route SRC DST
  [SKIP] WS 断链推送      未加 --mock（真网络断链请按 README 手动做）
合计: 6 通过 / 0 失败 / 2 跳过
```

**判据是这行数字，不是启动日志。** `onos_ok` 为 False 时脚本会直接把 `last_error` 打出来；
「Sanic 打印启动成功」只能证明 HTTP 起来了，跟 ONOS 通不通没关系。

想把下发/撤销也验一遍（会真的往网络里装规则，验完自动删）：

```bash
./run.sh smoke --route 10.0.0.1 10.0.0.3
```

判据逐条对应文档：`POST` 返回 `path` 与 `cost` → `/api/state.paths` 出现该对 →
设备上规则条数 ≥ 路径跳数 → `DELETE` 后 `paths` 清空且设备上 0 条残留规则。

排查顺序：`./run.sh check` → `docker ps` → `curl 8181/onos/v1/devices` → `pingall` →
`curl /api/state` 看 `last_error`。

---

## 没有 ONOS 也能跑（`demo`）

给三种场合用：新机器还没装 Docker、改完代码想快速回归、给别人演示但没有环境。

```bash
cd ~/brain && ./run.sh demo          # 起假 ONOS + 大脑，跑完整自检，最后留下页面
# 看完停止：./run.sh stop
./run.sh demo --no-keep              # 跑完立刻收摊（CI / 快速回归用这个）
```

`demo` 会拉起的假 ONOS（`tools/mock_onos.py`）在 8181 上按 **docs/02 的响应格式**返回
4 交换机 / 5 链路（带环）/ 6 主机的死拓扑，并实现 `POST /flows`、`DELETE /flows`、
`portstate`。所以走的是**大脑的真实代码路径**：轮询 → 建图 → diff → Dijkstra →
下发 → 回读确认 → 撤销 → 精确删规则，一条都不少。它会验 17 项，包括：

- 下发后设备上出现 `N` 条规则，撤销后回到 0 条（证明按 flowId 精确删除，没留残规则）
- WS 连上后断一条链路，**3~4 秒内**收到 `edges_removed` 推送，恢复后收到 `edges_added`
  （这就是 FR-14「断链 10s 内变红」的服务端一半）

**它不能证明什么**（答辩时别说错）：

| 不能证明 | 为什么 |
|---|---|
| `ping` 通 | 假 ONOS 不转发包，没有数据平面。A2 的 0% loss 必须在 Mininet 上验 |
| 时延 / 组播 | 假 ONOS 没有 `/statistics/ports`，也没有真实转发 |
| ONOS 版本兼容性 | 响应格式是照 2026-11-14 的真实抓包复刻的，但不是真控制器 |

一句话：**`demo` 证明「代码逻辑对」，`smoke` 在真环境里证明「和 ONOS 也对得上」。**

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
| `run.sh check` 报 **端口 8000 已占用** | 上次的大脑还挂着 | `./run.sh stop`；还是占着就 `lsof -i:8000` 找 pid 杀掉 |
| `run.sh demo` 报 **端口 8181 已占用** | 真 ONOS 正在跑 | 真环境不需要 demo：`./run.sh start` + `./run.sh smoke` |
| `./run.sh: Permission denied` | 文件权限没带过来（Windows 盘常见） | `bash run.sh <命令>`，或 `chmod +x brain/run.sh` |
| 本机上 `curl 127.0.0.1` 超时，但浏览器能开 | 系统代理把本地请求也劫持了 | `curl --noproxy '*' ...`；`run.sh` / `smoke.py` 内部已绕开代理 |

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
