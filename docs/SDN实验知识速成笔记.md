# SDN 实验知识速成笔记

> 根据 labs/ 下 8 份实验指导书整理，按"总览 → 分实验精讲 → 命令速查 → 易混考点"组织，可按顺序快速通读。

---

## 一、总览：SDN 是什么（先建立大图景）

传统网络中，每台交换机/路由器**自己**运行协议（MAC 自学习、OSPF、STP……），控制逻辑分布式地固化在每台设备里。

SDN（软件定义网络）把这件事拆开了：

| 平面 | 位置 | 职责 | 本课程对应工具 |
|---|---|---|---|
| **应用平面** | 上层程序 | 表达网络意图、做路径规划等决策 | Python + requests + Sanic |
| **控制平面** | SDN 控制器 | 集中维护全局拓扑视图，下发转发规则 | **ONOS** |
| **数据平面** | 交换机 | 只按规则转发，不做决策（"傻"转发） | **Open vSwitch (OVS)**，用 **Mininet** 仿真 |

三个接口方向：
- **南向接口**：控制器 ↔ 交换机，标准协议是 **OpenFlow**（下方向）+ Packet-In 等上报（上方向）
- **北向接口**：控制器 → 应用，主流是 **RESTful API**（ONOS 在 8181 端口）
- 东西向：多控制器实例间同步（ONOS 分布式集群）

一句话记忆：**控制器是大脑，交换机是手脚，OpenFlow 是神经，北向 REST API 是大脑对外的编程接口。**

8 个实验的学习路线（编号即依赖顺序）：

```
实验1 数据平面基础(OVS/Mininet) → 实验2 流表/组表(OpenFlow) → 实验3 控制器(ONOS部署)
→ 实验4 OpenFlow协议抓包分析 → 实验5 传统二层 vs SDN二层对比
→ 实验6 ONOS北向REST API → 实验7 应用平面编程(requests/Sanic)
→ 进阶 应用平面路径规划系统(综合大实验)
```

---

## 二、实验 1：OVS 与 Mininet 数据平面仿真基础

### 1. SDN 数据平面的核心：流表（Flow Table）
- 传统设备查"路由表/MAC 表"，SDN 交换机查**流表**：`匹配字段(match) → 指令(instruction)`
- 流表可匹配**多层头部**（端口、MAC、IP、传输层端口号），比传统设备灵活得多
- 数据平面"通用可编程、协议无关"——转发逻辑由控制器随时改写
- 延伸：**P4 语言 + PISA 芯片** 是更彻底的协议无关转发方案；P4 管交换机行为编程，OpenFlow 管控制器远程控制，二者可共存

### 2. Open vSwitch（OVS）关键概念
- 开源**虚拟**交换机，原生支持 OpenFlow
- 组件：`ovs-vswitchd`（守护进程）、`ovsdb-server`（配置数据库）、`ovs-vsctl`（配置工具）、`ovs-ofctl`（OpenFlow 流表工具）、`ovs-dpctl`（内核 datapath 工具）
- 概念层级：**Bridge = 一台交换机 → Port = 交换机上的端口 → Interface = 一块网卡**（虚拟或物理）
- Controller：OVS 可被一个或多个 OpenFlow 控制器管理

### 3. Linux 网络命名空间（network namespace）
- 隔离的全局网络资源：每个 ns 有独立的网卡、IP、路由表、防火墙
- 新建的 ns 只有 loopback；物理/虚拟网卡可以在 ns 之间移动
- 常用命令：`ip netns add/del/exec/list`；`nsenter -n -t <PID>` 进入某进程的网络 ns

### 4. veth 对
- 虚拟以太网**成对**设备，一端发出的包立刻从另一端收到，充当 ns 之间的"网线隧道"
- 组网套路：`ip netns add` 建主机 → `ovs-vsctl add-br` 建交换机 → `ip link add ... type veth peer name ...` 建线 → `ovs-vsctl add-port` 把交换机侧挂上桥 → `ip link set <veth> netns <ns>` 把主机侧移进 ns → 配 IP、up 接口

### 5. Mininet
- 把上述过程工程化封装：主机 = 网络命名空间，链路 = veth + tc 流量控制，交换机 = OVS
- 常用启动参数：`--topo single/linear/tree,minimal`、`--controller=remote,ip=...,port=6653`、`--mac`（MAC 规律排列 00:00:00:00:00:01...）、`--custom xxx.py --topo mytopo`、`--link=tc`（带宽/时延约束生效）、`-c` 清理
- CLI 常用：`nodes / net / links / dump / pingall / xterm h1 h2 / py <python表达式> / iperf h1 h2`
- 自定义拓扑模板（Python）：

```python
from mininet.topo import Topo
class MyTopo(Topo):
    def __init__(self):
        Topo.__init__(self)
        h1 = self.addHost("h1")
        s1 = self.addSwitch("s1")
        self.addLink(h1, s1, bw=10, delay='5ms', loss=10)  # 需 --link=tc
topos = {"mytopo": (lambda: MyTopo())}
```

---

## 三、实验 2：OpenFlow 流表、组表与 Pipeline

### 1. OpenFlow 交换机组成
= **流表/组表**（怎么处理包）+ **安全通道**（与控制器通信，TCP 或 TLS）。主流版本 **OpenFlow 1.3**（最新 1.5）。

### 2. 端口类型
- 物理端口 / 逻辑端口（聚合、隧道）
- 保留端口：`ALL`（除入端口全发）、`CONTROLLER`（发给控制器）、`TABLE`、`LOCAL`（本机网络栈）、`IN_PORT`、`NORMAL`（按传统交换机方式转发）、`FLOOD`
- OVS 支持全部保留端口；很多物理 OpenFlow 交换机不支持 NORMAL/FLOOD

### 3. 流表（Flow Entry）字段
`match fields`（匹配）+ `priority`（优先级，数字大者优先）+ `counters`（计数）+ `instructions`（指令）+ `timeouts`（idle/hard 超时）+ `cookie`（控制器标记）
- 无匹配项 → 触发 **table-miss** → 按配置丢弃/上交控制器/进下一张表
- 没有明确 action 的默认**丢弃**

### 4. Pipeline（流水线）机制
交换机内有多张流表（从 0 编号）。包从 table 0 开始匹配 → 选优先级最高的表项 → 执行指令集 → 若有 **Goto** 指令则带着修改后的匹配数据进下一张表；没有 Goto 则执行最终 action set。

### 5. 组表（Group Table）——复杂报文处理
字段：`group_id` + `type` + `action buckets`（动作桶列表）。四种类型：
- **all**：执行所有桶 → **组播/广播复制**（实验用它把一路 UDP 视频复制分发到多台主机）
- **select**：选一个桶执行 → 负载均衡
- **indirect**：只有一个桶
- **fast failover**：执行第一个"存活"桶 → 链路故障快速切换

注意：组表本身**没有匹配字段**，需要一条流表用 `action=group:<id>` 把流量引过去；转发前通常要 `mod_dl_dst / mod_nw_dst` 改写目的 MAC/IP，否则收端会丢弃。

### 6. 计量表（Meter Table）
限速/QoS：`meter bands` 里 `drop`（限速丢弃）、`dscp remark`（降丢弃优先级，DiffServ）。

### 7. ovs-ofctl 语法速记
```
ovs-ofctl add-flow s1 "priority=10, in_port=1, dl_type=0x0800, nw_proto=1, actions=output:2"
```
- 匹配层次：`in_port` → `dl_type/dl_src/dl_dst`（二层）→ `nw_proto/nw_src/nw_dst/nw_ttl`（三层）→ `tcp_src/tcp_dst`（四层）
- 常用协议号：IPv4=0x0800、ARP=0x0806、LLDP=0x88cc、BDDP=0x8942；nw_proto：ICMP=1、TCP=6、UDP=17
- 动作：`output:N / flood / all / normal / drop / controller / mod_dl_src... / mod_nw_dst... / group:N`
- 其它命令：`dump-flows`、`dump-groups`、`add-group`、`mod-flows`、`del-flows`

---

## 四、实验 3：ONOS 控制器部署与管理

### 1. 控制平面应具备的能力
最短路径转发、通告管理、安全隔离、拓扑管理、统计管理、设备管理。控制器 = **网络操作系统（NOS）**：对上提供统一 API 和抽象，屏蔽底层设备的异构性。

### 2. ONOS 四大架构特征（重点概念）
1. **分布式核心**：多实例对称部署，对外表现为一个逻辑实体；实例间 Pub/Sub 消息同步 + 领导选举（每台交换机有且只有一个主实例）→ 高可用、可无缝扩容、零宕机升级
2. **北向抽象层**：①意图框架（Intent：应用只声明"要什么"，如"主机A连通主机B"，控制器负责编译成流表并解决冲突）②全局网络视图（主机/交换机/链路/利用率的图模型，应用基于它编程）
3. **南向抽象层**：插件式协议适配（OpenFlow、NetConf...），屏蔽设备差异，支持新旧设备混布
4. **软件模块化**：应用/分布式核心/适配层解耦，无循环依赖，模块独立升级

### 3. 部署与使用（实操要点）
```bash
# Docker 部署 ONOS 2.7.0，三个关键端口：
# 6653 OpenFlow 通信 | 8101 CLI(SSH) | 8181 REST API + Web UI
sudo docker run -d -p 6653:6653 -p 8101:8101 -p 8181:8181 --restart=always --name onos onosproject/onos:2.7.0

# CLI 登录（注意必须指定 ssh-rsa 加密套件，用户名/密码均 karaf）
ssh -p 8101 karaf@<onos-host> -o HostKeyAlgorithms=+ssh-rsa
```
- 必激活的三个 Application：
  - `org.onosproject.openflow`：OpenFlow 基础 + ARP 主机定位 + LLDP 链路发现
  - `org.onosproject.openflow-message`：控制消息统计
  - `org.onosproject.fwd`（Reactive Forwarding）：**响应式转发**——收到 Packet-In 后按 IP 算路径下发流表，是"SDN 二层/三层交换"的实现者
- Mininet 连接：`sudo mn --controller=remote,ip=<onos>,port=6653 --mac --switch=ovs,protocols=OpenFlow13`
- ONOS CLI 常用：`onos:devices / onos:hosts / onos:links / onos:flows`（与 ovs-ofctl dump-flows 数据应一致）
- Web UI：`http://<onos>:8181/onos/ui/`，拓扑页 topo2，按 H 显示主机

---

## 五、实验 4：Wireshark 抓包分析 OpenFlow 协议

### 1. OpenFlow 消息三大类（必背）
| 类型 | 发起方 | 消息 | 作用 |
|---|---|---|---|
| **Controller-to-Switch** | 控制器 | Features / Configuration / Modify-state / Read-state / **Packet-out** / Barrier | 查能力、下发流表、发数据包、屏障同步 |
| **Asynchronous** | 交换机 | **Packet-in** / Flow-removed / Port-status | 上报数据包、流表过期、端口状态变化 |
| **Symmetric** | 双方 | **Hello** / Echo / Error | 建立连接协商版本 / 保活测延迟 / 报错 |

### 2. 连接建立流程（重点）
```
TCP(6653) 建立 → 双方互发 OFPT_HELLO（带版本位图，协商出共同最高版本，如 1.3=0x04）
→ 控制器发 FEATURES_REQUEST → 交换机回 FEATURES_REPLY（datapath_id、n_buffer、n_tables、能力集）
→ 控制器下发 Table-Miss 流表项（兜底：不匹配的包 → Packet-In）
→ 日常：Packet-In ↑ / Packet-Out ↓ / FLOW_MOD 下发流表；Echo 保活
```

### 3. 实操
- 抓包位置：Mininet 与 ONOS 同机抓 `lo`，不同机抓 `eth0`
- 过滤器：`openflow_v4`（即 OpenFlow 1.3）
- 典型报文序列（pingall 时）：ARP 包匹配 table-miss → OFPT_PACKET_IN 上交 → 控制器回 OFPT_PACKET_OUT（OFPAT_FLOOD 泛洪）→ 学到主机后 FLOW_MOD 下发双向流表
- 每条消息都有 OpenFlow 头：version / type / length / xid（事务 ID，响应与请求一致）

---

## 六、实验 5：传统二层交换 vs SDN 二层交换（本课程"思想课"）

### 1. 传统二层交换 = MAC 自学习
- 交换机收到帧 → 记录 **源MAC ↔ 入端口** → 表里没有目的 MAC 就**泛洪**（除入端口外全发）→ 回包到达时反向学习
- 问题：**环路会致命**（帧无限复制泛洪，瞬间瘫痪网络）

### 2. STP（生成树协议）及其缺陷
- 通过交换 BPDU 选举根交换机，阻塞冗余端口，把有环拓扑剪成无环树
- 缺陷：**收敛慢、链路利用率低（被阻塞的链路浪费）、规模受限、故障难定位** —— 数据中心 IO 密集 + 要求自动化扩展，STP 成最大短板 → 业界转向"大二层"，SDN 成为数据中心主流方案（Nicira 被 VMware 12.6 亿美元收购即标志）

### 3. SDN 二层交换（ONOS fwd 模块流程，重点）
```
交换机连上控制器 → 控制器下发初始流表（ARP/LLDP/BDDP/IPv4 → Packet-In）
→ 控制器用 LLDP/BDDP 探测发现链路、构建全局拓扑
→ h1 发 ARP：s1 按 table-miss 上交 Packet-In → 控制器解析学习 → Packet-Out 广播
→ h2 回 ARP：再上交 → 控制器学习并指导转发 → 主机发现完成
→ h1 发 ICMP：上交 → 控制器算路径 → 先 Packet-Out 转发首包，同时下发双向流表
→ 后续包由流表直接转发（不再经过控制器）
```
关键理解：**SDN 不是"每包问控制器"，而是"首个包触发学习，控制器一次性下发双向流表"**（响应式/reactive）。

### 4. OVS 的四种二层工作模式
1. 纯 OpenFlow 流表模式（有控制器）
2. 流表 + MAC 自学习混合（`action=normal` 让包脱离流表交给 OVS 传统转发）
3. 纯 MAC 自学习（类似 Linux Bridge）
4. 自学习 + 手动流表

### 5. 数据中心背景知识
- **3Tier 架构**：接入层（二层交换机，接服务器/VLAN）→ 汇聚层（三层交换机，POD 内路由/ACL）→ 核心层（业务路由器，跨 POD 路由/Internet/VPN）；接入-汇聚间是二层网络跑 STP
- 多租户挑战：虚拟网络要秒级创建；VLAN 只有 4096 个不够用 → SDN + Overlay
- SDN 也进入传送网：ONOS 就是**面向运营商**的控制器

### 6. 链路/主机发现原理（易考）
- **LLDP 发现交换机间链路**：控制器通过 Packet-Out 给每台交换机每端口下发 LLDP/BDDP 包（Chassis Id 标记交换机），邻居交换机泛洪回来后控制器收到 Packet-In，比对标记即可算出"谁和谁相连"
- **ARP 发现主机**：LLDP/BDDP 包主机不处理，主机靠 ARP 请求/响应被控制器学习到（MAC、IP、接入位置）

---

## 七、实验 6：ONOS 北向接口（REST API）

### 1. REST/RESTful 六大约束
Client-Server、**Stateless**（请求自带全部信息）、Cache、**Uniform Interface**（资源标识/表示操作资源/自描述消息/超媒体）、Layered System、Code-On-Demand（可选）。

### 2. RESTful 请求四要素
URI（资源定位）+ HTTP Method（GET 查 / POST 增改 / PUT 替换 / DELETE 删）+ Header（Content-Type、Accept 等）+ Body（POST/PUT/PATCH 携带 JSON）；响应看状态码（2xx 成功）。

### 3. ONOS 常用 API（基址 `http://<onos>:8181/onos/v1`，Basic 认证 karaf/karaf）
```
GET    /devices                    所有交换机
GET    /devices/{id}/ports         指定交换机端口
POST   /devices/{id}/portstate/{port}   启/禁端口（body: {"enabled": false}）
GET    /hosts                      所有主机（MAC/IP/接入位置）
GET    /links                      交换机间链路（一条链路会输出两次，src/dst 相反）
GET    /flows/{deviceId}           指定交换机流表
POST   /flows/{deviceId}?appId=xx  下发流表（body 为 JSON）
DELETE /flows/{deviceId}/{flowId}  删单条流表
DELETE /flows/application/{appId}  按 appId 批量删除（如 org.onosproject.core / org.onosproject.fwd）
GET    /topology/clusters/{id}/devices|links   拓扑设备/链路
接口文档（Swagger）：http://<onos>:8181/onos/v1/docs/
```

### 4. ONOS 流表 JSON 结构（重点，实验 7/进阶都要用）
```json
{
  "priority": 40000, "timeout": 0, "isPermanent": true,
  "deviceId": "of:0000000000000001",
  "selector": { "criteria": [ {"type":"IN_PORT","port":2},
                              {"type":"ETH_TYPE","ethType":"0x0800"},
                              {"type":"ETH_DST","mac":"00:00:00:00:00:01"} ] },
  "treatment": { "instructions": [ {"type":"OUTPUT","port":1} ] }
}
```
- `selector.criteria` = 匹配条件数组（IN_PORT / ETH_TYPE / ETH_SRC / ETH_DST / IPV4_SRC / IPV4_DST / IP_PROTO ...）
- `treatment.instructions` = 动作数组（OUTPUT / NOACTION(丢弃) / MODIFICATION 改写字段 ...）
- appId 标识流表来源，可按 appId 分类统一管理/删除

---

## 八、实验 7：应用平面编程（requests + Sanic）

### 1. requests 调 ONOS API 模板
```python
import requests
from requests.auth import HTTPBasicAuth

auth = HTTPBasicAuth("karaf", "karaf")
headers = {"Accept": "application/json"}

# GET 设备
resp = requests.get("http://<onos>:8181/onos/v1/devices", headers=headers, auth=auth)
device_ids = [d["id"] for d in resp.json()["devices"]]

# POST 下发流表（json 参数直接传字典）
requests.post(f"http://<onos>:8181/onos/v1/flows/{device_id}",
              params={"appId": "test"}, json=flow_dict, auth=auth,
              headers={"Content-Type": "application/json", "Accept": "application/json"})

# DELETE 按 appId 清流表
requests.delete("http://<onos>:8181/onos/v1/flows/application/org.onosproject.core", auth=auth)
```

### 2. Sanic 封装成自己的 Web 服务
- Sanic：基于 async/await 的高性能异步 Web 框架
- 核心概念：App（`Sanic("name")`）、Handler（async 函数 + `@app.route/@app.get` 装饰器）、Request（`request.json` 取 POST 数据）、Response（`text()/json()`）、Route、Exceptions（`@app.exception(NotFound)` 自定义 404/500）
- 启动：`app.run(host="0.0.0.0", port=8000, single_process=True)`；调试用 `debug=True, dev=True`（生产关掉 debug 防泄露）
- 典型模式：**应用平面把多次北向调用封装成一个简化接口**。例如"每台交换机挂了哪些主机" = GET /devices + GET /hosts 两次调用整合成一个字典，再通过 `GET /v1/devices-hosts` 对外发布 → 上层使用者一次调用即可

---

## 九、进阶实验：基于跳数的端到端路径动态规划（综合）

### 1. 系统闭环
```
Mininet+OVS 数据平面 → 北向接口采集拓扑/链路/主机 → 构建图 → 最短路径规划
→ 生成双向流表 → POST 下发 → ping 验证 →（故障/带宽变化 → 重新规划）
```
前置操作：**关闭 fwd 模块**（`app deactivate org.onosproject.fwd`，否则它抢着下发流表干扰自研算法）+ 按 appId 清空历史流表（只留 org.onosproject.core 的初始流表）。

### 2. 图建模两种方式
- **交换机为顶点**：链路为边；规划出的是"交换机序列"，下发时要换算成端口
- **端口为顶点**：下发流表直接用端口，简单；但要维护"同一交换机的端口归组"
- 主机通信规划 = 规划其接入交换机（或接入端口）之间的路径

### 3. 最短路径算法要点
- 单源最短路径：**Dijkstra**（贪心+优先队列，O(E+VlogV)，不能有负权边；OSPF/IS-IS 用的就是它）与 **Bellman-Ford**（逐边松弛 |V|-1 轮，O(VE)，能处理负权，RIP 的思想）
- 网络路径规划的特点：**有向图**（全双工/不对称带宽）、**多约束权重**（时延/带宽/丢包/跳数，不同业务不同权重策略）、**动态性**（拓扑和负载持续变化，算法要高效且避免震荡）
- 本实验权重 = 跳数（每条边权 1，BFS 即可，但用 Dijkstra 通用）

### 4. 流表下发
对路径上每台交换机下发**双向**流表：入端口 X → output 出端口 Y，以及反向。例如按源/目的 MAC 匹配 + OUTPUT。

### 5. 实时感知网络变化的四种技术（对比记忆）
| 技术 | 模式 | 特点 |
|---|---|---|
| **轮询 Polling** | 客户端定期 GET | 简单兼容好，浪费资源、实时性差 |
| **WebSocket** | 双工长连接 | 实时双向；有状态、难负载均衡、可能被代理挡 |
| **SSE** | 服务器单向推 | 基于 GET + text/event-stream，只能推文本 |
| **WebTransport** | 基于 HTTP/3 | 可靠流 + 不可靠数据报，性能最好，尚在草案 |

### 6. 进阶方向（实验要求）
- 故障处理：轮询比对拓扑变化（`ovs-vsctl del-port` 模拟 Port Down、`del-br` 模拟交换机故障、py 命令动态加设备）→ 触发重规划
- 带宽约束：Mininet `addLink(bw=...)`，定期读端口计数算已用带宽，以剩余带宽为权重规划
- 背景流量、组播（组表 + 多单播路径合并，注意分叉点放组表）、了解 ONOS **Intent 框架**（指定源/目的 IP 即自动编译为流表——本实验手写的就是一个简化版意图框架）

---

## 十、命令速查表

```bash
# ---- Mininet ----
sudo mn --topo=single,3 --mac --controller=remote,ip=<onos>,port=6653 --switch=ovs,protocols=OpenFlow13
sudo mn --custom topo.py --topo mytopo --link=tc     # 自定义拓扑+链路约束
sudo mn -c                                            # 清理残留
mininet> pingall | nodes | net | links | dump | ports | xterm h1 h2 | py ...
mininet> iperf h1 h2                                  # TCP 吞吐测试

# ---- OVS ----
ovs-vsctl add-br/del-br/list-br/show
ovs-vsctl add-port/del-port <br> <port>
ovs-vsctl set-controller <br> tcp:<ip>:6653 | del-controller
ovs-ofctl dump-flows s1 -O OpenFlow13
ovs-ofctl add-flow s1 "priority=10,in_port=1,actions=output:2"
ovs-ofctl add-flow s1 "dl_type=0x0800,nw_proto=6,actions=normal"   # 放行 TCP
ovs-ofctl add-group s1 "group_id=3,type=all,bucket=mod_nw_dst:10.0.0.2,output:2,..."
ovs-ofctl dump-groups s1

# ---- 网络命名空间 ----
ip netns add/del/list;  ip netns exec h1 <cmd>
ip link add a type veth peer name b;  ip link set a netns h1
nsenter -n -t <PID>

# ---- ONOS ----
sudo docker run -d -p 6653:6653 -p 8101:8101 -p 8181:8181 --restart=always --name onos onosproject/onos:2.7.0
ssh -p 8101 karaf@<onos> -o HostKeyAlgorithms=+ssh-rsa
app activate org.onosproject.openflow / openflow-message / fwd
app deactivate org.onosproject.fwd
onos:devices | onos:hosts | onos:links | onos:flows
# REST 基址 http://<onos>:8181/onos/v1 ，文档 /docs ，认证 karaf/karaf

# ---- OpenFlow 常用协议号 ----
IPv4=0x0800  ARP=0x0806  LLDP=0x88cc  BDDP=0x8942
ICMP=1  TCP=6  UDP=17
```

---

## 十一、最容易被问/最容易混的点

1. **Packet-In vs Packet-Out**：In 是交换机→控制器（上交包），Out 是控制器→交换机（发还/注入包）。
2. **FLOW_MOD 就是"下发流表"这个动作的协议消息名**；实验 6 的 POST /flows 是北向 REST 层面的等价操作。
3. **table-miss 流表项**（priority=0）：没匹配上时怎么办——丢弃/上交控制器/去下一张表。ONOS 初始下发的 priority=40000 的 ARP/LLDP/BDDP 和 priority=5 的 ip 规则本质就是"把这些包 Packet-In 给我"。
4. **响应式转发 vs 主动式**：fwd 是响应式（包来了才算路径下发流表）；Intent 框架偏主动声明。
5. **priority 大者优先**；idle_timeout（软超时，无流量则删）vs hard_timeout（硬超时，到点必删）。
6. **组表没有匹配字段**，必须由流表 `group:N` 引流；复制到多端口时记得改目的 MAC/IP。
7. **SDN 解决了 STP 什么问题**：不再需要阻塞链路保无环——控制器全局视图算最短路径下发流表，全链路可用、收敛快、可编程。
8. **onflows/REST 看到的流表与 ovs-ofctl dump-flows 一致**——控制器是流表的唯一权威来源。
9. Mininet 主机 = 网络命名空间，所以 xterm/iperf/Wireshark 都是"进某个 ns 里跑"；Wireshark 只能看到本 ns 的网卡。
10. ONOS 三个端口别混：**6653=OpenFlow 南向，8101=CLI(SSH)，8181=REST/Web 北向**。
