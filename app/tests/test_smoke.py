"""骨架冒烟测试。"""


def test_skeleton_imports():
    from app.api import app

    assert app is not None


def test_registry_constructs():
    from app.registry import Registry

    assert Registry() is not None
