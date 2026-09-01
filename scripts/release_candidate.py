#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    receipt = {
        "result": "release candidate executed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    Path("release-executed.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("RELEASE CANDIDATE EXECUTED")


if __name__ == "__main__":
    main()

