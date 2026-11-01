"""乙负责的路径计算模块。

函数契约见 docs/02_接口契约与数据结构.md。当前仅搭建结构，具体逻辑待补充。
"""


def dijkstra(graph: dict, src: str, dst: str) -> dict | None:
    """计算两个交换机之间的最短路径。

    TODO: 实现路径、总代价和每跳出端口的计算。
    TODO: 覆盖 src == dst、节点不存在和目的不可达情况。
    """
    raise NotImplementedError


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
