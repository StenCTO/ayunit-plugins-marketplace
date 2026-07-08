#!/usr/bin/env python
"""CLI wrapper: fetch ANBIMA cadastral data by code and print JSON to stdout."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))

from _anbima_client import get_cadastral  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch ANBIMA cadastral data by code.")
    ap.add_argument("--codigo", required=True, help="ANBIMA code (e.g. 258.363)")
    ap.add_argument("--source", default="agnes", choices=["agnes", "anbima"])
    args = ap.parse_args()

    try:
        data = get_cadastral(args.codigo, source=args.source)
    except Exception as e:
        print(f"ERROR ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
