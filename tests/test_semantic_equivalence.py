"""AST 语义等价进主路径单测：drift 检测 + 跳过 verify + trace。"""

import pytest

from src.tools.ast_parser import (
    _extract_signatures,
    check_semantic_equivalence,
)


class TestCheckSemanticEquivalence:
    def test_identical_code_is_ok(self):
        code = "def add(a, b):\n    return a + b\n"
        result = check_semantic_equivalence(code, code)
        assert result["status"] == "semantic_ok"

    def test_modified_function_body_is_ok(self):
        """函数体内修改不算语义漂移。"""
        orig = "def add(a, b):\n    return a + b\n"
        patched = "def add(a, b):\n    return int(a) + int(b)\n"
        result = check_semantic_equivalence(orig, patched)
        assert result["status"] == "semantic_ok"

    def test_removed_function_is_drift(self):
        """删除函数定义 → drift。"""
        orig = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"
        patched = "def add(a, b):\n    return a + b\n"
        result = check_semantic_equivalence(orig, patched)
        assert result["status"] == "drift"
        assert "removed" in result["detail"]
        assert "sub" in result["detail"]

    def test_added_function_is_drift(self):
        """新增函数定义 → drift。"""
        orig = "def add(a, b):\n    return a + b\n"
        patched = "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        result = check_semantic_equivalence(orig, patched)
        assert result["status"] == "drift"
        assert "added" in result["detail"]
        assert "mul" in result["detail"]

    def test_syntax_error_is_drift(self):
        """语法错误 → drift。"""
        result = check_semantic_equivalence("def add(a, b):", "def add(a, b)")
        assert result["status"] == "drift"
        assert "syntax" in result["detail"]

    def test_empty_input_detected(self):
        """空代码 → drift。"""
        result = check_semantic_equivalence("", "def foo():\n    pass\n")
        assert result["status"] == "drift"
        assert "added" in result["detail"]


class TestExtractSignatures:
    def test_extracts_function_signatures(self):
        import ast

        tree = ast.parse("def add(a, b):\n    return a + b\n\ndef mul(x):\n    return x * 2\n")
        sigs = _extract_signatures(tree)
        assert "def add(a, b)" in sigs
        assert "def mul(x)" in sigs
        assert len(sigs) == 2

    def test_extracts_class_names(self):
        import ast

        tree = ast.parse("class Calculator:\n    def calc(self):\n        pass\n")
        sigs = _extract_signatures(tree)
        assert "class Calculator" in sigs
        assert "def calc(self)" in sigs
