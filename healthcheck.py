"""Container health probe: the server is healthy when it rejects an anonymous
request with 401.

A bare TCP check would pass even if BearerAuthMiddleware had failed to attach,
which is the failure most worth catching. A 200 here would mean the endpoint is
answering unauthenticated callers, so it is treated as unhealthy.
"""

import os
import sys
import urllib.error
import urllib.request

PORT = os.getenv("TMW_MCP_PORT", "8000")
URL = f"http://127.0.0.1:{PORT}/mcp"


def main() -> int:
    request = urllib.request.Request(URL, method="POST", data=b"{}")
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return 0
        print(f"unhealthy: expected 401, got {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is unhealthy
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("unhealthy: anonymous request was not rejected", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
