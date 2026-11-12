def tree_to_group(tree: dict, group_id: int) -> str:
    # 预留组播树到组表的转换
    raise NotImplementedError


def install_group(bridge: str, group_str: str) -> bool:
    # 预留交换机组表的安装
    raise NotImplementedError


def install_multicast_flows(res_tree: dict, *args, **kwargs) -> list[str]:
    # 预留组播流表的安装
    raise NotImplementedError
