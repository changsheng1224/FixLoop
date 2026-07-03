"""importer demo 测试。test_main_greet 会触发 ImportError。"""


def test_main_greet():
    """通过 app.main 调用，暴露错误 import 路径。"""
    from app import main

    assert main() == "hello, world"


def test_helpers_import_directly():
    """utils.helpers 模块本身可正常导入。"""
    from utils.helpers import greet

    assert greet("FixLoop") == "hello, FixLoop"


def test_greet_default():
    from utils.helpers import greet

    assert greet() == "hello, world"
