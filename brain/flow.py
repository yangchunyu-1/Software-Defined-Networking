import time


# 按通信对保存已部署的路径和流表
deployed = {}


def path_to_flows(res: dict, src_host: dict, dst_host: dict) -> list[tuple[str, dict]]:
    # 将路径转换为交换机流表
    # 待修正链路两端端口号可能不同的问题
    path = res["path"]
    out_ports = res["out_ports"]
    if not path:
        raise ValueError("path must contain at least one switch")
    if len(out_ports) != len(path) - 1:
        raise ValueError("out_ports length must equal len(path) - 1")

    # 为路径上的每台交换机构造转发规则
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

        # 待从配置读取流表优先级
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
    # 下发路径流表并记录部署状态
    # 待完善回读校验异常回滚和旧规则替换
    if onos is None:
        raise NotImplementedError("ONOS client is not available yet")

    # 记录本次下发取得的流表 ID
    flow_ids = []
    for device_id, flow in path_to_flows(res, src_host, dst_host):
        flow_id = onos.post_flow(device_id, flow)
        if flow_id is None:
            # 尝试清理本次已下发的流表
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
    # 精确删除通信对对应的流表
    if key not in deployed:
        return False
    if onos is None:
        raise NotImplementedError("ONOS client is not available yet")

    # 保留删除失败的流表记录
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
    # 预留受影响通信对的筛选
    raise NotImplementedError


def recompute(affected_keys: list[tuple[str, str]], graph: dict, host_table: dict) -> list[dict]:
    # 预留拓扑变化后的路径重算
    raise NotImplementedError
