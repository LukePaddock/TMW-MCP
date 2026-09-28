"""Smoke-test a running TMW MCP server over HTTP.

Works against a local bind or the public URL once it is behind a proxy:

    python smoke_test.py http://127.0.0.1:8000/mcp <api-key>
    python smoke_test.py https://tmw-mcp.example.com/mcp <api-key>

Checks auth rejection, the MCP handshake, real tool calls, and concurrent calls
(which is what shook out the shared-connection bug). Exits non-zero on failure.
"""

import asyncio
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f'  - {detail}' if detail else ''}")


async def expect_401(url: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers |= {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "1"}},
    }
    async with httpx2.AsyncClient(timeout=30) as http:
        r = await http.post(url, json=body, headers=headers)
    label = "wrong key rejected" if token else "missing key rejected"
    check(r.status_code == 401, label, f"got HTTP {r.status_code}")


async def session_checks(url: str, key: str) -> None:
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx2.AsyncClient(headers=auth, timeout=60) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                check(True, "valid key accepted, handshake completed")

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                check(len(names) >= 10, "tools advertised", f"{len(names)} tools")

                res = await session.call_tool(
                    "find_city_codes", {"name": "Chicago", "state": "IL"}
                )
                check(not res.is_error and "69087" in res.content[0].text,
                      "find_city_codes returns data")

                res = await session.call_tool("summarize_orders", {
                    "group_by": "revtype1",
                    "started_after": "2025-01-01", "started_before": "2026-01-01",
                })
                check(not res.is_error and "order_count" in res.content[0].text,
                      "summarize_orders aggregates")

                # Concurrency: interleaved cursors on a shared DB connection
                # return WRONG ROWS rather than raising, so compare values.
                one = await session.call_tool("summarize_orders", {
                    "group_by": "revtype1", "revtype1": ["EPT"],
                    "started_after": "2025-01-01", "started_before": "2026-01-01"})
                baseline = one.content[0].text

                calls = [
                    session.call_tool("summarize_orders", {
                        "group_by": "revtype1", "revtype1": ["EPT"],
                        "started_after": "2025-01-01", "started_before": "2026-01-01"})
                    for _ in range(10)
                ]
                out = await asyncio.gather(*calls, return_exceptions=True)
                failed = [o for o in out if isinstance(o, Exception) or o.is_error]
                differing = [
                    o for o in out
                    if not isinstance(o, Exception) and o.content[0].text != baseline
                ]
                check(not failed and not differing,
                      "10 concurrent calls agree",
                      f"{len(failed)} errored, {len(differing)} returned different data")

                res = await session.call_tool("search_orders", {})
                check(res.is_error, "unfiltered search refused")


async def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    url, key = sys.argv[1], sys.argv[2]
    print(f"Testing {url}\n")
    try:
        await expect_401(url, None)
        await expect_401(url, "definitely-not-a-valid-key")
        await session_checks(url, key)
    except Exception as exc:  # noqa: BLE001 - report, don't traceback
        check(False, "unexpected error", f"{type(exc).__name__}: {exc}")

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
