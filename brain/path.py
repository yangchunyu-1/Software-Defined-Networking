import heapq


def dijkstra(graph: dict, src: str, dst: str) -> dict | None:
    # 根据拓扑计算最短路径和各跳出端口
    if src == dst:
        return {"path": [src], "cost": 0, "out_ports": []}

    if src not in graph or dst not in graph:
        return None

    # 记录最短距离和路径前驱
    distances = {src: 0}
    parents = {}
    queue = [(0, src)]

    # 按距离顺序遍历交换机
    while queue:
        current_cost, current = heapq.heappop(queue)
        if current_cost != distances.get(current):
            continue
        if current == dst:
            break

        # 更新邻居的最短距离和前驱
        for neighbor, edge in graph.get(current, {}).items():
            weight = edge.get("weight", 1)
            if weight < 0:
                raise ValueError("Dijkstra requires non-negative edge weights")

            next_cost = current_cost + weight
            previous_cost = distances.get(neighbor, float("inf"))
            if next_cost < previous_cost:
                distances[neighbor] = next_cost
                parents[neighbor] = current
                next_entry = (next_cost, neighbor)
                heapq.heappush(queue, next_entry)

    if dst not in distances:
        return None

    # 回溯生成完整路径
    path = [dst]
    while path[-1] != src:
        path.append(parents[path[-1]])
    path.reverse()

    # 提取路径各跳的出端口
    out_ports = []
    for index in range(len(path) - 1):
        current = path[index]
        neighbor = path[index + 1]
        edge = graph[current][neighbor]
        out_ports.append(edge["sport"])

    return {"path": path, "cost": distances[dst], "out_ports": out_ports}


def shortest_path_tree(graph: dict, src: str) -> dict:
    # 预留最短路径树计算
    raise NotImplementedError


def multicast_tree(graph: dict, src: str, dsts: list[str]) -> dict:
    # 预留组播路径计算
    raise NotImplementedError
