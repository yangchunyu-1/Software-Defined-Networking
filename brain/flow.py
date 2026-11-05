import time


# 以 (源 IP, 目的 IP) 为键，保存路径和已记录的 (设备 ID, flow ID)。
deployed = {}


def path_to_flows(res: dict, src_host: dict, dst_host: dict) -> list[tuple[str, dict]]:
    """将路径结果转换为每台交换机的流表请求，不执行网络操作。

    res 包含 path 和 out_ports；src_host、dst_host 提供主机接入端口。
    返回按路径顺序排列的 (device_id, flow) 列表；路径结构错误时抛出 ValueError。
    当前中间跳入端口直接使用上一跳出端口；链路两端端口不同会导致错误，需对齐契约后修正。
    """
    path = res["path"]
    out_ports = res["out_ports"]
    if not path:
        raise ValueError("path must contain at least one switch")
    if len(out_ports) != len(path) - 1:
        raise ValueError("out_ports length must equal len(path) - 1")

    # 首跳从源主机进入，末跳发往目的主机；单交换机路径同时满足两种情况。
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

        # priority 暂用契约示例值，后续应从甲维护的配置中读取。
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
    """通过 onos 安装路径规则，并在全部返回 ID 后记录 deployed。

    key 是 (源 IP, 目的 IP)；res 和两个 host 参数传给 path_to_flows。
    onos 须提供 post_flow 和 delete_flow；返回成功记录或失败字典。
    此函数会请求修改交换机流表及模块状态；尚未实现回读、异常回滚和安全替换旧规则。
    """
    if onos is None:
        raise NotImplementedError("ONOS client is not available yet")

    # 暂存已取得 ID 的规则；只有全部取得 ID 后才写入 deployed。
    flow_ids = []
    for device_id, flow in path_to_flows(res, src_host, dst_host):
        flow_id = onos.post_flow(device_id, flow)
        if flow_id is None:
            # 当前仅尝试删除先前取得 ID 的规则，未检查删除失败或处理异常。
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
    """按 key 的 flow ID 删除规则；onos 须提供 delete_flow。

    全部删除成功返回 True；记录不存在或存在删除失败时返回 False。
    会请求修改交换机流表和 deployed；删除失败的 ID 保留在记录中。
    """
    if key not in deployed:
        return False
    if onos is None:
        raise NotImplementedError("ONOS client is not available yet")

    # 保留返回失败的 ID，避免把仍需处理的规则从记录中移除。
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
    """预留受影响通信对筛选接口；输入为拓扑差异和部署记录。

    尚未实现，调用会抛出 NotImplementedError。
    """
    raise NotImplementedError


def recompute(affected_keys: list[tuple[str, str]], graph: dict, host_table: dict) -> list[dict]:
    """预留路径重算接口；输入为受影响的通信对、拓扑图和主机表。

    尚未实现，调用会抛出 NotImplementedError，也不会修改流表。
    """
    raise NotImplementedError
