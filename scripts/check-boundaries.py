"""Fail closed when serving or pure engine code gains external capabilities."""

import ast
from pathlib import Path

root = Path("src/earnings_watch")
violations = []
for path in root.rglob("*.py"):
    relative = path.relative_to(root)
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        modules = (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        for module in modules:
            if module.split(".")[0] == "sqlite3" and relative.parts[0] != "store":
                violations.append(f"{relative}: sqlite3 outside store")
            if module.split(".")[0] == "httpx" and relative.as_posix() != "transport.py":
                violations.append(f"{relative}: HTTP outside transport")
            if module.split(".")[0] == "subprocess" and relative.parts[0] != "llm":
                violations.append(f"{relative}: subprocess outside model runner")
            if relative.parts[0] == "engine" and module.split(".")[0] in {
                "httpx",
                "socket",
                "subprocess",
                "sqlite3",
                "os",
                "pathlib",
            }:
                violations.append(f"{relative}: impure engine import {module}")
            if relative.as_posix() == "service.py" and any(
                word in module.split(".")
                for word in ["llm", "transport", "providers", "maintenance"]
            ):
                violations.append(f"{relative}: serving gained maintenance dependency")
if violations:
    raise SystemExit("\n".join(violations))
print("Dependency boundaries passed")
