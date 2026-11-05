import heapq


def dijkstra(graph: dict, src: str, dst: str) -> dict | None:
    """在有向拓扑中计算从 src 到 dst 的最短路径。

    graph 是交换机邻接表，边包含 sport 和非负 weight；src、dst 是交换机 ID。
    返回包含 path、cost、out_ports 的字典；节点不存在或不可达时返回 None。
    src 和 dst 均在图中且相同时，返回代价为零的单节点路径。
    """
    if src not in graph or dst not in graph:
        return None

    if src == dst:
        return {"path": [src], "cost": 0, "out_ports": []}

    # distances 保存目前发现的最小代价；parents 用于最终从目的节点回溯。
    distances = {src: 0}
    parents = {}
    queue = [(0, src)]

    # 最小堆按代价取节点；旧条目可能仍留在堆中，取出时需要跳过。
    while queue:
        current_cost, current = heapq.heappop(queue)
        if current_cost != distances.get(current):
            continue
        if current == dst:
            break

        # Dijkstra 依赖非负边权，只有更短的路径才更新距离与前驱。
        for neighbor, edge in graph.get(current, {}).items():
            weight = edge.get("weight", 1)
            if weight < 0:
                raise ValueError("Dijkstra requires non-negative edge weights")
            next_cost = current_cost + weight
            if next_cost < distances.get(neighbor, float("inf")):
                distances[neighbor] = next_cost
                parents[neighbor] = current
                heapq.heappush(queue, (next_cost, neighbor))

    if dst not in distances:
        return None

    # 沿前驱从目的节点回溯，再反转得到源到目的的交换机序列。
    path = [dst]
    while path[-1] != src:
        path.append(parents[path[-1]])
    path.reverse()

    # sport 属于当前交换机；对每条路径边取其本地出端口。
    out_ports = []
    for index in range(len(path) - 1):
        edge = graph[path[index]][path[index + 1]]
        out_ports.append(edge["sport"])

    return {"path": path, "cost": distances[dst], "out_ports": out_ports}


def shortest_path_tree(graph: dict, src: str) -> dict:
    """预留最短路径树接口；graph 是拓扑图，src 是源交换机。

    尚未实现，调用会抛出 NotImplementedError。
    """
    raise NotImplementedError


def multicast_tree(graph: dict, src: str, dsts: list[str]) -> dict:
    """预留组播树接口；dsts 是目的交换机 ID 列表。

    尚未实现，调用会抛出 NotImplementedError。
    """
    raise NotImplementedError
