"""乙负责的流表构造与生命周期管理模块。

函数契约见 docs/02_接口契约与数据结构.md。当前仅搭建结构，具体逻辑待补充。
"""

import time


deployed = {}

def path_to_flows(res: dict, src_host: dict, dst_host: dict) -> list[tuple[str, dict]]:
    """将路径结果映射为按路径顺序排列的交换机流表。

    res: dijkstra 返回的路径结果。
    src_host/dst_host: host_table 中对应 IP 的主机位置。
    返回 (device_id, flow_json) 列表，不执行下发。

    TODO: 甲提供 config.py 后，按其实际优先级常量名接入，避免本模块硬编码。
    """
    path = res["path"]
    out_ports = res["out_ports"]
    if not path:
        raise ValueError("path must contain at least one switch")
    if len(out_ports) != len(path) - 1:
        raise ValueError("out_ports length must equal len(path) - 1")

    flows = []
    for index, device_id in enumerate(path):
        if index == 0:
            in_port = src_host["port"]
        else:
            in_port = out_ports[index - 1]

        if index == len(path) - 1:
            output_port = dst_host["port"]
        else:
            output_port = out_ports[index]

        flow = {
            "priority": 40000,
            "timeout": 0,
            "isPermanent": True,
            "deviceId": device_id,
            "selector": {
                "criteria": [{"type": "IN_PORT", "port": str(in_port)}]
            },
            "treatment": {
                "instructions": [{"type": "OUTPUT", "port": str(output_port)}]
            },
        }
        flows.append((device_id, flow))

    return flows


def install_path(key: tuple[str, str], res: dict, src_host: dict, dst_host: dict, onos=None) -> dict:
    """安装一对主机的路径流表并维护 deployed 记录。

    TODO: 接入甲提供的 onos 客户端，逐交换机下发并回读校验。
    TODO: 下发中途失败时按已取得的 flow ID 回滚。
    """
    if onos is None:
        raise NotImplementedError("ONOS client is not available yet")

    flow_ids = []
    for device_id, flow in path_to_flows(res, src_host, dst_host):
        flow_id = onos.post_flow(device_id, flow)
        if flow_id is None:
            for installed_device, installed_id in reversed(flow_ids):
                onos.delete_flow(installed_device, installed_id)
            return {"ok": False, "error": "flow_id_unavailable", "flow_ids": []}
        flow_ids.append((device_id, str(flow_id)))

    deployed[key] = {
        "path": list(res["path"]),
        "out_ports": list(res["out_ports"]),
        "flow_ids": list(flow_ids),
        "installed_at": time.time(),
    }
    return {"ok": True, **deployed[key]}


def uninstall(key: tuple[str, str], onos=None) -> bool:
    """按记录中的 flow ID 精确删除指定通信对的规则。

    TODO: 将失败删除留在部署记录中，确保状态不掩盖残留规则。
    """
    if key not in deployed:
        return False
    if onos is None:
        raise NotImplementedError("ONOS client is not available yet")

    record = deployed[key]
    all_deleted = True
    remaining = []
    for device_id, flow_id in record["flow_ids"]:
        if not onos.delete_flow(device_id, flow_id):
            all_deleted = False
            remaining.append((device_id, flow_id))

    if remaining:
        record["flow_ids"] = remaining
    else:
        del deployed[key]
    return all_deleted


def affected(diff: dict, deployed: dict) -> list[tuple[str, str]]:
    """筛选拓扑差异影响到的已部署通信对。

    TODO: 根据路径、变化的节点/链路和主机位置判断受影响项。
    """
    raise NotImplementedError


def recompute(affected_keys: list[tuple[str, str]], graph: dict, host_table: dict) -> list[dict]:
    """重算受影响通信对的路径并更新流表。

    TODO: 按项目规则处理新旧流替换、失败回滚和结果记录。
    """
    raise NotImplementedError
