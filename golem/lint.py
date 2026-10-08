"""Static lint for candidate tools. It names a privilege request early and in words.

This is not the boundary. The sandbox is: no network, no environment, read-only
filesystem, non-root, no capabilities. The lint exists so a request for more
authority is refused with a readable reason instead of failing somewhere inside
the container.

Forbidden calls are patterns over the dotted call name: "eval" matches the
builtin, "os.replace" matches only os.replace (not str.replace), and "*.unlink"
matches any method named unlink.
"""

from __future__ import annotations

import ast
import fnmatch

WRITE_CALLS = (
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.removedirs",
    "os.rename",
    "os.replace",
    "os.truncate",
    "shutil.rmtree",
    "shutil.move",
    "shutil.copy*",
    "pathlib.Path.unlink",
    "*.write_text",
    "*.write_bytes",
    "*.unlink",
    "*.rmdir",
)


def scan(
    source: str,
    forbidden_imports: list[str],
    forbidden_calls: list[str],
    for_tests: bool = False,
) -> list[str]:
    """Privilege requests in source. Tests may write fixture files: inside the sandbox
    only /tmp is writable, so filesystem writes are left to the sandbox for test files."""
    if for_tests:
        forbidden_calls = [
            pattern for pattern in forbidden_calls if pattern not in WRITE_CALLS
        ]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    banned_modules = set(forbidden_imports)
    found: list[str] = []
    aliases: dict[str, str] = {}

    def add(message: str) -> None:
        if message not in found:
            found.append(message)

    def module_banned(name: str) -> bool:
        return name in banned_modules or name.split(".")[0] in banned_modules

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
                if module_banned(alias.name):
                    add(f"new authority: imports {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            if module_banned(node.module):
                add(f"new authority: imports {node.module}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func, aliases)
            if dotted and any(
                fnmatch.fnmatchcase(dotted, pattern) for pattern in forbidden_calls
            ):
                add(f"new authority: calls {dotted}()")
            if (
                not for_tests
                and dotted in ("open", "io.open", "builtins.open")
                and _opens_for_writing(node)
            ):
                add("new authority: opens a file for writing")
        elif isinstance(node, ast.Attribute) and node.attr in {
            "environ",
            "getenv",
            "putenv",
            "environb",
        }:
            add(f"new authority: reads the environment ({node.attr})")
    return found


def _dotted(func: ast.AST, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(aliases.get(func.id, func.id))
    elif parts:
        parts.append("?")
    else:
        return None
    return ".".join(reversed(parts))


def _opens_for_writing(call: ast.Call) -> bool:
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    return isinstance(mode, str) and any(flag in mode for flag in "wax+")
