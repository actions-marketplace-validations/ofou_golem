"""Runs inside the sandbox container. Reads JSON arguments on stdin, calls tool.run, prints one result line.

This file is mounted read-only at /golem/runner.py. It is kernel code, not generated code.
"""

import json
import sys

MARK = "<<<GOLEM_RESULT>>>"


def main() -> None:
    sys.path.insert(0, "/tool")
    try:
        args = json.loads(sys.stdin.read() or "{}")
        import tool  # noqa: E402  (the generated module)

        result = tool.run(args)
        print(MARK + json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
    except BaseException as exc:  # report every failure as data
        print(MARK + json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}))


if __name__ == "__main__":
    main()
