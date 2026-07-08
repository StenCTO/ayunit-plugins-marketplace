#!/usr/bin/env python
"""CLI wrapper: fetch ANBIMA historical series by code and print JSON to stdout."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))

from _anbima_client import get_historical  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch ANBIMA historical series by code.")
    ap.add_argument("--codigo", required=True, help="ANBIMA code (e.g. 258.363)")
    ap.add_argument("--size", type=int, default=None, help="Number of records")
    ap.add_argument("--data-inicio", dest="data_inicio", default=None, help="Start date YYYY-MM-DD")
    ap.add_argument("--source", default="agnes", choices=["agnes", "anbima"])
    args = ap.parse_args()

    try:
        data = get_historical(
            args.codigo,
            size=args.size,
            data_inicio=args.data_inicio,
            source=args.source,
        )
    except Exception as e:
        print(f"ERROR ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
