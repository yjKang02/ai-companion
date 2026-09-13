import ast
from pathlib import Path


def test_core_imports_only_stdlib_and_inner_modules():
    root = Path(__file__).parents[1] / "src" / "ai_companion"
    paths = [root / "domain.py", *sorted((root / "application").glob("*.py"))]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert module.split(".")[0] not in {"discord", "httpx", "openai", "dotenv"}
                assert not module.startswith("ai_companion.adapters"), path
