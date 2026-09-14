import ast
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest


def assert_core_imports(source, package):
    for node in ast.walk(ast.parse(source)):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            modules = [module]
            # 패키지에서 가져온 하위 모듈도 확인해 from .. import adapters 등을 막는다.
            if module in {"ai_companion", "ai_companion.application"}:
                modules.extend(f"{module}.{alias.name}" for alias in node.names)
        for module in modules:
            assert (
                module.split(".")[0] in sys.stdlib_module_names
                or module in {"ai_companion", "ai_companion.domain", "ai_companion.application"}
                or module.startswith("ai_companion.application.")
            ), f"{package}: 허용되지 않은 코어 의존성 {module}"


def test_core_imports_only_stdlib_and_inner_modules():
    root = Path(__file__).parents[1] / "src" / "ai_companion"
    paths = [
        root / "__init__.py",
        root / "domain.py",
        *sorted((root / "application").rglob("*.py")),
    ]
    for path in paths:
        package = ".".join(path.parent.relative_to(root.parent).parts)
        try:
            assert_core_imports(path.read_text(encoding="utf-8"), package)
        except AssertionError as error:
            raise AssertionError(f"{path}: {error}") from error


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio",
        "from collections.abc import AsyncIterator",
        "import ai_companion",
        "from ai_companion import domain",
        "from ai_companion.domain import ChatRequest",
        "from ai_companion.application import ports",
        "from .ports import ChatModel",
        "from . import ports",
        "from ..domain import ChatRequest",
        "from .. import domain",
    ],
)
def test_architecture_guard_accepts_stdlib_and_core_imports(source):
    assert_core_imports(source, "ai_companion.application")


@pytest.mark.parametrize(
    "source",
    [
        "import requests",
        "from requests import Session",
        "import discord",
        "import httpx",
        "import openai",
        "import dotenv",
        "import ai_companion.adapters.memory",
        "from ai_companion.adapters import memory",
        "from ai_companion import adapters",
        "from ..adapters.memory import InMemoryConversationStore",
        "from .. import adapters",
        "from ai_companion.bootstrap import run_bot",
        "from .. import bootstrap",
        "from ai_companion import config",
        "from ai_companion import *",
    ],
)
def test_architecture_guard_rejects_third_party_and_outer_imports(source):
    with pytest.raises(AssertionError, match="허용되지 않은 코어 의존성"):
        assert_core_imports(source, "ai_companion.application")
