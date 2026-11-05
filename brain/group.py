def tree_to_group(tree: dict, group_id: int) -> str:
    """预留组播树到 group 描述的转换接口；当前尚未实现。"""
    raise NotImplementedError


def install_group(bridge: str, group_str: str) -> bool:
    """预留在指定 bridge 安装 group 的接口；当前尚未执行任何操作。"""
    raise NotImplementedError


def install_multicast_flows(res_tree: dict, *args, **kwargs) -> list[str]:
    """预留组播流表安装接口；完整参数和实现尚待确定。"""
    raise NotImplementedError
