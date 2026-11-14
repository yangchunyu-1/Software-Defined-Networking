# S2 链路时延 · REST 响应字段契约（阶段 B 侦察归纳）

> 观测对象：ONOS 2.7.0 + Mininet `tree,2,2`，2026-11-14 落盘的三份真实响应。
> 本文件只记录**响应结构与字段契约**、精度口径、待补的文档条目；不含环境值、账号、本机路径与执行进度。
> 精度口径（docs/03 §11.4）：本模块给出的是**近似值，非插件级测量**。

---

## 1. D1 · `GET /statistics/ports` 字段清单

顶层结构：`{"statistics": [{"device": <dpid>, "ports": [...]}, ...]}`。

每个端口记录固定 10 个键：

```
port, packetsReceived, packetsSent, bytesReceived, bytesSent,
packetsRxDropped, packetsTxDropped, packetsRxErrors, packetsTxErrors, durationSec
```

- `port`、`durationSec` 为整数；`device` 为字符串 dpid。
- 4 个 dropped/errors 键**全部在场且值为 0，不省略**。因此解析可以放心用 `.get(key, 0)`，
  但不能假设「键缺失就等于 0」是 ONOS 的通用行为——这只在本次样本里成立。
- 本次样本为 3 devices / 8 port records。

证据片段（原文，节选一条端口记录）：

```json
{"port":1,"packetsReceived":294,"packetsSent":292,"bytesReceived":40134,"bytesSent":39856,
 "packetsRxDropped":0,"packetsTxDropped":0,"packetsRxErrors":0,"packetsTxErrors":0,"durationSec":431}
```

计数是**累计值**，不是速率：链路负载必须两轮采样做差分，
`load_bps = 8 * Δ(bytesReceived + bytesSent) / Δt`。计数回绕或 Δt 过短的轮次直接丢弃，不用负速率。

---

## 2. D2 · `GET /devices/{id}/ports` 结构与数量级

顶层是**单个 device 对象**（含 `id` / `available` / `annotations` / `ports`），
不是 `{"devices": [...]}`，也不是端口数组。解析时取 `payload["ports"]`。

`ports[]` 内字段：`element` / `port` / `isEnabled` / `type` / `portSpeed` / `annotations`。
- `port` 是**字符串**（`"1"`、`"2"`），与 `/statistics/ports` 里的整数 `port` 需要归一化后对齐；
  `"local"` 这类非数字端口不参与链路建模。
- `portSpeed` 实测为 `local: 0`、`"1": 10000`、`"2": 10000`。

证据片段（原文，节选）：

```json
{"element":"of:0000000000000001","port":"1","isEnabled":true,"type":"copper",
 "portSpeed":10000,"annotations":{"adminState":"enabled","portName":"s1-eth1"}}
```

**数量级说明（不作单位结论）**：`config.DELAY_PORT_SPEED_TO_BPS` 暂取 1000，
使 10000 映射到 10 Mbps 量级，与 `DELAY_DEFAULT_CAP_BPS` 一致。
ONOS `portSpeed` 的官方单位换算仍需真机确认；在确认前，`portSpeed` 缺失或为 0 一律回落到默认容量，
不把换算结果当成已验证的带宽真值。

---

## 3. D3 · 设备覆盖与 stale 分支

`/statistics/ports` 覆盖 3 devices / 8 port records，`/devices` 也是 3 devices，
**无缺失设备**。因此本次数据下不需要「整台设备统计缺失」的兜底分支。

但实现仍保留两类失效标记，供断链演练使用：
- 链路在本轮图里消失（`/links` 不再有该有向边）→ 该链路标 `stale`；
- 链路在图里、但两轮计数里没有对应端口 → `source` 降为 `P3`（兜底常数）并标 `stale`。

---

## 4. 两份 statistics 样本的差异

`cmp` 结果为 **DIFFER**（不是逐字节相同），sha256 不同；同为 1665 字节但内容不同。
计数确有增长，例如 dev3 port1 `packetsSent` 293→309、`durationSec` 431→456。
因此「采样无效、需重采」的预设情形成立条件不满足。

注意：`raw_stats_ports_idle.json` 的 `durationSec` 更晚、计数更大，
命名与「idle」的字面方向相反；使用时只按文件内容，不按文件名推断负载高低。

---

## 5. 与既有字段清单的核对

- `links.json` 全文：4 条有向边（2 条物理链路 × 双向），`type` 全 `DIRECT`、`state` 全 `ACTIVE`，
  `src.port` / `dst.port` 为字符串；**无 `latency`、无 `annotations`**。
  → ONOS 北向 REST 不提供真实链路传播时延，P1（直接读取）不可得；
    S2 只能走 P2（计数差分 + M/M/1 排队近似）→ P3（兜底常数）。
- 端口 `sport` / `dport` 在 `topo.build_graph(links)` 里已被转成 `int`（topo.py:42、:79），
  与 `/statistics/ports` 的整数 `port` 直接对齐；与 `/devices/{id}/ports` 的字符串 `port` 需归一化。
- `docs/02` §2 端点表当前共 8 行，第 8 行已有 `GET /devices/{id}/ports`（状态 ❓），
  **表内无 `/statistics/ports`**。

---

## 6. 待补的契约条目（随 PR 生效）

| 位置 | 只加不改的动作 |
|---|---|
| `docs/02` §2 端点表 | 追加第 9 行 `GET /statistics/ports`；第 8 行的 ❓ 状态位本次**不改动** |
| `docs/02` §3.2 | 追加 `fetch_port_statistics()` / `fetch_device_ports(device_id)` 两个签名 |
| `docs/05` §3 fixture 规则 | 计数类字段（`bytesReceived` 等 10 键）随上述 PR 一并成为契约字段 |

`docs/02` §0 冻结范围含「全部函数签名」，流程为「先提 PR 改本文档 → 48 小时无异议 → 再动代码」。
本阶段的离线测试把计数样本**内联在测试代码中**，不新增 `tests/fixtures/` 文件，
避免在数据契约生效前新增未定义字段的 fixture。

---

## 7. 本阶段未验证项（标 ❓）

- `set_port_state` 的断链写操作（`docs/02` §3.2 标注「尚未实测，N7 演示时验证」）；
- 真 ONOS 上连续两轮 `/statistics/ports` 差分得到的 `delay_ms`，以及打流/断链后的变化；
- `portSpeed` 的官方单位换算；
- fwd 激活后 `/flows` 中转发规则的实际下发条数（需主机间真实流量）。
