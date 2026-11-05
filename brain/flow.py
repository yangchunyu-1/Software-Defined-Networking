import ipaddress
import time

try:
    from .path import dijkstra
except ImportError:
    from path import dijkstra


def path_to_flows(key, res, graph, src_host, dst_host, priority):
    # 将通信对和路径转换为交换机规则
    src_ip = str(ipaddress.IPv4Address(key[0])) + "/32"
    dst_ip = str(ipaddress.IPv4Address(key[1])) + "/32"
    path = res["path"]
    out_ports = res["out_ports"]
    if not path or len(out_ports) != len(path) - 1:
        raise ValueError("invalid path or out_ports")
    if path[0] != src_host["dpid"] or path[-1] != dst_host["dpid"]:
        raise ValueError("host locations do not match path")

    flows = []
    for index, device_id in enumerate(path):
        if index == 0:
            in_port = src_host["port"]
        else:
            previous = path[index - 1]
            in_port = graph[previous][device_id]["dport"]

        if index == len(path) - 1:
            output_port = dst_host["port"]
        else:
            neighbor = path[index + 1]
            output_port = graph[device_id][neighbor]["sport"]
            if output_port != out_ports[index]:
                raise ValueError("path ports do not match graph")

        criteria = [
            {"type": "IN_PORT", "port": str(in_port)},
            {"type": "ETH_TYPE", "ethType": "0x0800"},
            {"type": "IPV4_SRC", "ip": src_ip},
            {"type": "IPV4_DST", "ip": dst_ip},
        ]
        flow = {
            "priority": priority,
            "timeout": 0,
            "isPermanent": True,
            "deviceId": device_id,
            "selector": {"criteria": criteria},
            "treatment": {"instructions": [{"type": "OUTPUT", "port": str(output_port)}]},
        }
        flows.append((device_id, flow))
    return flows


async def _delete_rules(flow_ids, onos):
    # 保留无法确认删除的规则
    remaining = []
    for device_id, flow_id in flow_ids:
        try:
            deleted = await onos.delete_flow(device_id, flow_id)
        except Exception:
            deleted = False
        if not deleted:
            remaining.append((device_id, flow_id))
    return remaining


async def install_path(key, res, graph, src_host, dst_host, deployed, onos, priority):
    # 安装并回读验证一条新路径
    if key in deployed:
        return {"ok": False, "error": "already_exists", "flow_ids": []}
    flows = path_to_flows(key, res, graph, src_host, dst_host, priority)
    flow_ids = []
    unknown = []
    error = None
    for device_id, flow in flows:
        try:
            flow_id = await onos.post_flow(device_id, flow)
        except Exception:
            unknown.append((device_id, flow))
            error = "post_result_unknown"
            break
        if flow_id is None:
            unknown.append((device_id, flow))
            error = "flow_id_unknown"
            break
        flow_id = str(flow_id)
        flow_ids.append((device_id, flow_id))
        try:
            installed = await onos.fetch_flows(device_id)
            ids = [str(item.get("id")) for item in installed]
            if flow_id not in ids:
                error = "flow_not_confirmed"
                break
        except Exception:
            error = "flow_read_failed"
            break

    if error is not None:
        remaining = await _delete_rules(flow_ids, onos)
        if remaining or unknown:
            deployed[key] = {"path": list(res["path"]), "out_ports": list(res["out_ports"]),
                             "flow_ids": remaining, "unknown_flows": unknown,
                             "status": "cleanup_required", "installed_at": time.time()}
        return {"ok": False, "error": error, "flow_ids": remaining}

    deployed[key] = {"path": list(res["path"]), "out_ports": list(res["out_ports"]),
                     "flow_ids": flow_ids, "installed_at": time.time(), "status": "active"}
    return {"ok": True, "error": None, "flow_ids": flow_ids}


async def uninstall(key, deployed, onos):
    # 删除已知规则并保留未确认的状态
    if key not in deployed:
        return {"ok": False, "error": "not_found", "remaining": []}
    record = deployed[key]
    remaining = await _delete_rules(record.get("flow_ids", []), onos)
    record["flow_ids"] = remaining
    if remaining or record.get("unknown_flows"):
        record["status"] = "cleanup_required"
        return {"ok": False, "error": "cleanup_required", "remaining": remaining}
    del deployed[key]
    return {"ok": True, "error": None, "remaining": []}


def affected(diff, deployed):
    # 筛选拓扑变化可能影响的通信对
    changed_hosts = set(diff.get("hosts_removed", []))
    changed_hosts.update(diff.get("hosts_moved", {}))
    removed_nodes = set(diff.get("nodes_removed", []))
    removed_edges = diff.get("edges_removed", [])
    added_edges = diff.get("edges_added", [])
    affected_keys = []
    for key, record in deployed.items():
        path = record["path"]
        if added_edges or key[0] in changed_hosts or key[1] in changed_hosts:
            affected_keys.append(key)
            continue
        if any(node in removed_nodes for node in path):
            affected_keys.append(key)
            continue
        for index in range(len(path) - 1):
            current = path[index]
            neighbor = path[index + 1]
            if any(current in edge and neighbor in edge for edge in removed_edges):
                affected_keys.append(key)
                break
    return affected_keys


async def recompute(affected_keys, graph, host_table, deployed, onos, priority):
    # 检查受影响路径并拒绝未经验证的规则切换
    results = []
    for key in affected_keys:
        record = deployed.get(key)
        result = {"key": key, "old_path": None, "new_path": None, "ok": False, "error": None}
        if record is None:
            result["error"] = "not_found"
        elif record.get("status", "active") != "active":
            result["old_path"] = record["path"]
            result["error"] = "cleanup_required"
        else:
            result["old_path"] = record["path"]
            src_host = host_table.get(key[0])
            dst_host = host_table.get(key[1])
            if src_host is None or dst_host is None:
                result["error"] = "host_not_found"
            else:
                new_path = dijkstra(graph, src_host["dpid"], dst_host["dpid"])
                if new_path is None:
                    result["error"] = "unreachable"
                else:
                    result["new_path"] = new_path["path"]
                    result["error"] = "replacement_policy_unverified"
        results.append(result)
    return results
