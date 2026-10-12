"""
smoke_test.py — check the deployed MCP server end to end, WITHOUT the LLM.

It connects exactly like the agent does (MCP_URL + MCP_TOKEN), calls a few
tools against live USGS data, and prints PASS/FAIL for each. Run it after every
deployment or code change:

  set -a; source /etc/usgs-mcp.env; set +a     # or export MCP_URL / MCP_TOKEN yourself
  python smoke_test.py
"""
import asyncio
import json
import os
import sys

import agent   # reuses make_mcp_client(), so this tests the same connection path as the agent

SITE = "USGS-01463500"   # Delaware River at Trenton, NJ: a long-running, reliable gauge

CHECKS = [
    # (description, tool, arguments, text a good answer contains, should it be an error?)
    ("list the parameter codes", "parameter_codes", {}, "00060", False),
    ("latest discharge at Trenton", "get_latest", {"monitoring_location_id": [SITE], "parameter_code": "00060"}, SITE, False),
    ("what Trenton measures", "list_timeseries", {"monitoring_location_id": [SITE]}, "00060", False),
    ("30 days of daily discharge", "get_daily",
     {"monitoring_location_id": [SITE], "parameter_code": "00060", "time": "P30D"}, "series", False),
    ("site search in one county", "find_sites",
     {"state_name": "New Jersey", "county_name": "Mercer County"}, "USGS-", False),
    ("guardrail rejects a bad site ID", "get_latest", {"monitoring_location_id": ["Trenton"]}, "not a valid site ID", True),
]


async def main() -> int:
    if not os.getenv("MCP_URL"):
        print("MCP_URL is not set, so this would test a local stdio server, not the deployed one.")
        print("Load your settings first:  set -a; source /etc/usgs-mcp.env; set +a")
        return 1
    failures = 0
    async with agent.make_mcp_client() as mcp:
        tools = await agent.list_allowed_tools(mcp)
        print(f"Connected to {os.environ['MCP_URL']}: {len(tools)} tools\n")
        for desc, tool, args, expect, should_error in CHECKS:
            r = await mcp.call_tool(tool, args)
            text = "\n".join(getattr(c, "text", "") for c in r.content)
            ok = expect in text and bool(r.is_error) == should_error
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {desc}")
            if not ok or "--verbose" in sys.argv:
                print("      " + text[:400].replace("\n", "\n      "))
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
