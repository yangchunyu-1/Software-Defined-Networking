"""乙负责的路径计算模块。

函数契约见 docs/02_接口契约与数据结构.md。当前仅搭建结构，具体逻辑待补充。
"""

import heapq


def dijkstra(graph: dict, src: str, dst: str) -> dict | None:
    """计算两个交换机之间的最短路径。

    graph: 有向邻接表；边数据包含 sport 和非负 weight。
    返回路径交换机序列、总权重以及每一跳的本地出端口。
    """
    if src not in graph or dst not in graph:
        return None

    if src == dst:
        return {"path": [src], "cost": 0, "out_ports": []}

    distances = {src: 0}
    parents = {}
    queue = [(0, src)]

    while queue:
        current_cost, current = heapq.heappop(queue)
        if current_cost != distances.get(current):
            continue
        if current == dst:
            break

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

    path = [dst]
    while path[-1] != src:
        path.append(parents[path[-1]])
    path.reverse()

    out_ports = []
    for index in range(len(path) - 1):
        edge = graph[path[index]][path[index + 1]]
        out_ports.append(edge["sport"])

    return {"path": path, "cost": distances[dst], "out_ports": out_ports}


def shortest_path_tree(graph: dict, src: str) -> dict:
    """计算从源交换机出发的最短路径树。

    TODO: 按接口契约返回各节点的父节点、端口和距离。
    """
    raise NotImplementedError


def multicast_tree(graph: dict, src: str, dsts: list[str]) -> dict:
    """计算源交换机到多个目的交换机的组播树。

    TODO: 可选功能阶段再实现；当前保留接口声明。
    """
    raise NotImplementedError
