"""Example tool plugin: exact arithmetic for bots (no code execution: a small expression evaluator).

Copy this file to start your own tool. See docs/extending.md, "Add a tool".
"""
import ast
import math
import operator

TOOL = {
    "name": "calculator",
    "description": "Evaluate an arithmetic expression exactly, e.g. '12.5 * (3 + 4) / 2' or 'sqrt(2) ** 2'. "
                   "Supports + - * / // % **, parentheses, and the functions sqrt, log, sin, cos, round, abs, min, max.",
    "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    "scope": ["chat", "session"],
    "approval": "never",
    "label": {"en": "Calculated", "ja": "計算した"},
}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "sin": math.sin, "cos": math.cos, "round": round, "abs": abs,
          "min": min, "max": max}
_CONSTS = {"pi": math.pi, "e": math.e}


MAX_BITS = 4096          # results bigger than this would take seconds to compute and freeze SnapLab


def _check(v):
    if isinstance(v, int) and v.bit_length() > MAX_BITS:
        raise ValueError("the number is too large")
    if isinstance(v, float) and v != v:
        raise ValueError("not a number")
    return v


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000:
                raise ValueError("exponent too large")
            if isinstance(left, int) and isinstance(right, int) and right > 0 and abs(left) > 1 \
                    and left.bit_length() * right > MAX_BITS:          # check before computing
                raise ValueError("the number is too large")
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int) \
                and left.bit_length() + right.bit_length() > MAX_BITS:
            raise ValueError("the number is too large")
        return _check(_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("only numbers, + - * / // % **, parentheses and a few math functions are allowed")


def run(ctx, expression: str):
    expression = (expression or "").strip()
    if not expression or len(expression) > 300:
        raise ValueError("give an expression of up to 300 characters")
    value = _eval(ast.parse(expression, mode="eval"))
    return {"expression": expression, "value": value}
