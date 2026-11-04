"""Decode object arguments, including Dify's legacy dict-to-string transport."""
import ast
import json
import math


# These bounds apply only to the compatibility format, not ordinary JSON inputs.
MAX_LITERAL_CHARS = 262144
MAX_LITERAL_NODES = 16384
MAX_LITERAL_DEPTH = 32


def parse_tool_arguments(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    error = '工具参数必须是对象或有效的JSON对象字符串'
    if len(value) > MAX_LITERAL_CHARS:
        raise ValueError(error)
    try:
        tree = ast.parse(value.strip(), mode='eval')
        # Bound traversal before literal_eval; never evaluate executable syntax.
        pending = [(tree, 0)]
        nodes = 0
        while pending:
            node, depth = pending.pop()
            nodes += 1
            if nodes > MAX_LITERAL_NODES or depth > MAX_LITERAL_DEPTH:
                raise ValueError(error)
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        result = ast.literal_eval(tree)
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise ValueError(error) from None

    # Accept only the JSON data model: no tuples, sets, bytes or non-string keys.
    if not isinstance(result, dict):
        raise ValueError(error)
    pending = [result]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError(error)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError(error)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(error)
    return result
