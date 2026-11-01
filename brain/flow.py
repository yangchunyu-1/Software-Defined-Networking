"""乙负责的流表构造与生命周期管理模块。

函数契约见 docs/02_接口契约与数据结构.md。当前仅搭建结构，具体逻辑待补充。
"""


def path_to_flows(res: dict, src_host: dict, dst_host: dict) -> list[tuple[str, dict]]:
    """将路径结果映射为按路径顺序排列的交换机流表。

    TODO: 构造首跳、中间跳和末跳的 IN_PORT -> OUTPUT 规则。
    """
    raise NotImplementedError


def install_path(key: tuple[str, str], res: dict, src_host: dict, dst_host: dict, onos=None) -> dict:
    """安装一对主机的路径流表并维护 deployed 记录。

    TODO: 调用 ONOS 下发并回读校验规则。
    TODO: 处理部分失败回滚，并在成功后记录 flow ID。
    """
    raise NotImplementedError


def uninstall(key: tuple[str, str], onos=None) -> bool:
    """按记录中的 flow ID 精确删除指定通信对的规则。

    TODO: 删除成功后同步更新 deployed 状态。
    """
    raise NotImplementedError


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
