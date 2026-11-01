"""乙负责的可选组播组表模块。

当前只声明接口；必须功能验收通过后再实现组播细节。
"""


def tree_to_group(tree: dict, group_id: int) -> str:
    """将组播树转换为 ovs-ofctl group 描述。

    TODO: 按接口契约生成 group 字符串。
    """
    raise NotImplementedError


def install_group(bridge: str, group_str: str) -> bool:
    """在指定 OVS bridge 上安装 group。

    TODO: 调用 ovs-ofctl 并处理命令失败。
    """
    raise NotImplementedError


def install_multicast_flows(res_tree: dict, *args, **kwargs) -> list[str]:
    """安装组播路径对应的流表。

    TODO: 完整参数契约待组播实现阶段依据 02 补齐。
    """
    raise NotImplementedError
