"""来源归一化用例的隔离 runner：每个 test_ 函数在独立子进程运行，
避免内存仓库与 data/ 产物在不同用例之间串扰。
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from tests import test_region_normalize as mod  # noqa: E402

TESTS = [
    (name, fn)
    for name, fn in inspect.getmembers(mod, inspect.isfunction)
    if name.startswith("test_")
]


def main() -> int:
    failed: list[str] = []
    for name, _ in TESTS:
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(BACKEND_DIR)!r}); "
            f"from tests.test_region_normalize import {name}; "
            f"{name}()"
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print(f"PASS: {name}")
        else:
            failed.append(name)
            print(f"FAIL: {name}")
            print(proc.stdout)
            print(proc.stderr)
    print(f"\n{len(TESTS) - len(failed)}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
