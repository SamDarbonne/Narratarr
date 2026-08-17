"""The Docker HEALTHCHECK command. Read APP-CONTRACT.md section 13.1.

This script calls `GET /api/v1/system/health`, the one route that needs
no `X-Api-Key` header. It uses the standard library only, so the image
needs no extra package such as `curl` just for this check.
"""

from __future__ import annotations

import os
import sys
import urllib.request

PORT = os.environ.get("NARRATARR_PORT", "8000")
URL = f"http://127.0.0.1:{PORT}/api/v1/system/health"


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=5) as response:
            if response.status != 200:
                print(f"unhealthy: status {response.status}", file=sys.stderr)
                return 1
    except Exception as exc:  # noqa: BLE001 - any fault here means unhealthy
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
