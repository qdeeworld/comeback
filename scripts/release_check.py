#!/usr/bin/env python3
from __future__ import annotations

import subprocess


def main() -> None:
    tests = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "-q"], check=False
    )
    if tests.returncode:
        raise SystemExit(tests.returncode)
    print("RELEASE CHECK PASSED")


if __name__ == "__main__":
    main()

