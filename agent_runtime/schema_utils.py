"""工具 Schema 自动生成：从 dataclass type hints 推导 schema 和参数校验。

使得新增工具只需要定义 dataclass + 执行函数，schema 和校验自动生成。
"""

from dataclasses import MISSING, fields
from typing import get_args, get_origin, get_type_hints


def auto_schema(args_cls: type) -> dict:
    """从 dataclass 的 type hints 自动推导工具参数 schema。

    Args:
        args_cls: 参数 dataclass 类型。

    Returns:
        dict，key 为参数名，value 为 "type" 或 "type=default" 格式。

    Example:
        >>> from dataclasses import dataclass
        >>> @dataclass
        ... class ReadArgs:
        ...     path: str
        ...     start: int = 1
        >>> auto_schema(ReadArgs)
        {'path': 'str', 'start': 'int=1'}
    """
    hints = get_type_hints(args_cls)
    schema = {}
    field_map = {f.name: f for f in fields(args_cls)}

    for name, hint in hints.items():
        type_str = _type_to_str(hint)
        f = field_map.get(name)
        if f is not None and f.default is not MISSING:
            schema[name] = f"{type_str}={f.default}"
        else:
            schema[name] = type_str
    return schema


def _type_to_str(hint) -> str:
    """将 Python type hint 转为 schema 字符串。

    Args:
        hint: 类型注解，如 str、int、Optional[str] 等。

    Returns:
        类型名字符串。
    """
    origin = get_origin(hint)
    if origin is not None:
        # 处理 Optional[str] → str, Union[int, None] → int
        args = get_args(hint)
        # 过滤 NoneType
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if non_none:
            return _type_to_str(non_none[0])
        return "str"
    # 基本类型
    mapping = {str: "str", int: "int", float: "float", bool: "bool"}
    return mapping.get(hint, "str")


def auto_validate(args_cls: type, args: dict) -> dict:
    """校验工具参数：检查类型 + 缺失必填字段 + 尝试类型转换。

    Args:
        args_cls: 参数 dataclass 类型。
        args: 模型传入的参数字典。

    Returns:
        校验并转换后的参数字典（可能包含类型转换后的值）。

    Raises:
        ValueError: 参数校验失败时抛出，包含具体错误描述。
    """
    hints = get_type_hints(args_cls)
    schema = auto_schema(args_cls)
    validated = {}

    for name, spec in schema.items():
        expected_base = spec.split("=")[0]  # "str", "int", ...
        has_default = "=" in spec

        if name not in args:
            if has_default:
                # 有默认值，跳过（模型未传时使用工具函数内部的默认值）
                continue
            else:
                raise ValueError(f"缺少必填参数: {name}（类型: {expected_base}）")
        else:
            value = args[name]
            hint = hints.get(name, str)

            # 类型转换
            origin = get_origin(hint)
            if origin is not None:
                # Optional → 取内部类型
                args_list = get_args(hint)
                hint = next((a for a in args_list if a is not type(None)), str)  # noqa: E721

            try:
                if hint is int and not isinstance(value, int):
                    value = int(value)
                elif hint is float and not isinstance(value, float):
                    value = float(value)
                elif hint is bool and not isinstance(value, bool):
                    value = bool(value)
            except (ValueError, TypeError):
                raise ValueError(
                    f"参数 {name} 类型错误: 期望 {expected_base}，实际 {type(value).__name__}"
                )
            validated[name] = value

    return validated
