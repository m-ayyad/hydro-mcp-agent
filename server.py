"""
server.py — the MCP server: it exposes USGS water data as "tools" an LLM can call.

Built on the official MCP Python SDK v2 (`pip install "mcp[cli]"`).

BIG PICTURE
-----------
MCP (Model Context Protocol) is a standard way to give an LLM access to
outside systems. An MCP *server* (this file) publishes a list of tools. Each
tool has a name, a description, and typed arguments. An MCP *client* (agent.py,
Claude Desktop, etc.) reads that list, shows it to the LLM, and when the LLM
says "call find_sites with state_name='New Jersey'", the client sends that
request here. We run the Python function and send the result back.

The LLM never talks to USGS directly. It can only do what these functions allow,
which is why the input checks in guardrails.py matter.

DATA SOURCE
-----------
We use `dataretrieval.waterdata`, the Python client for the modernized USGS
Water Data APIs (api.waterdata.usgs.gov). The older `dataretrieval.nwis`
module talks to WaterServices, which USGS is retiring in Q1 2027.

HOW TO RUN
----------
  Local, for one program on the same machine:  python server.py --transport stdio
  Try the tools in a web UI (MCP Inspector):   mcp dev server.py
  On the EC2 instance, as a network service:   python server.py --transport http --port 8000

ENVIRONMENT VARIABLES (never hard-code secrets in the file)
  API_USGS_PAT   USGS API key; dataretrieval reads it automatically. Raises your rate limit.
  MCP_TOKEN      Password-like token clients must send when using --transport http.
"""
from __future__ import annotations

import argparse          # reads command-line options like --port 8000
import asyncio           # lets the server handle several requests at once
import hmac              # constant-time string comparison for the token check
import json
import logging
import os                # reads environment variables
import time
import warnings
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import BaseModel, Field           # attaches descriptions/limits to arguments
from mcp.server.mcpserver import MCPServer      # the SDK's high-level server (was "FastMCP" in v1)
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.types import CallToolResult, TextContent
from dataretrieval import waterdata as wd       # the USGS client library

import guardrails as g                          # our input checks (see guardrails.py)

warnings.filterwarnings("ignore", category=FutureWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("usgs-mcp")

# ---------------------------------------------------------------------------
# Output-size settings.
# Everything a tool returns is pasted into the LLM's context window (its
# short-term memory). That space is limited and costs money per token, so we
# return summaries and a limited number of rows instead of raw dumps.
# ---------------------------------------------------------------------------
MAX_ROWS_RETURNED = int(os.getenv("MAX_ROWS_RETURNED", "200"))    # rows sent to the LLM
MAX_ROWS_FETCHED = int(os.getenv("MAX_ROWS_FETCHED", "50000"))    # rows pulled from USGS
MAX_RESPONSE_BYTES = 200_000                                      # hard cap on one tool response
USGS_TIMEOUT_S = float(os.getenv("USGS_TIMEOUT_S", "60"))         # give up on slow USGS calls
USGS_RETRIES = 3                                                  # retry on rate limit / server errors
RATE_LIMIT_PER_S = 2.0                                            # sustained tool calls per second
RATE_LIMIT_BURST = 10                                             # short bursts allowed above that
CACHE_TTL_S = 300                                                 # reuse identical results for 5 min

COMMON_PARAMETER_CODES = {
    "00060": "Discharge, ft3/s",
    "00065": "Gage height, ft",
    "00010": "Water temperature, deg C",
    "00045": "Precipitation, in",
    "00095": "Specific conductance, uS/cm @25C",
    "00300": "Dissolved oxygen, mg/L",
    "00400": "pH",
    "63680": "Turbidity, FNU",
    "62614": "Lake/reservoir water surface elevation above NGVD 1929, ft",
    "72019": "Depth to water level below land surface, ft",
    "72279": "Tidal elevation, ft",
}

# These instructions are sent to the client when it connects. Many clients
# show them to the LLM, so they act like a short user manual for the tools.
INSTRUCTIONS = """Tools for searching and retrieving USGS water data.
Site IDs use the form 'USGS-01646500' (agency prefix + site number).
Time arguments must be ISO 8601: intervals ('2024-10-01/2025-09-30'),
from-a-date ('2025-01-01/..'), or durations ('P7D', 'P1M').
Typical workflow: find_sites -> list_timeseries -> get_latest / get_daily / get_continuous.
If a tool returns an error, read it: it explains how to fix the request."""


# ===========================================================================
# SERVER-WIDE GUARDRAILS (middleware)
# Middleware is code that runs around EVERY request, before and after the tool.
# Think of it as a security checkpoint all requests pass through.
#
# In the SDK, a middleware is any async function `(ctx, call_next) -> result`:
#   - ctx.method is the MCP method, e.g. "tools/call" or "tools/list"
#   - ctx.params holds the raw request, e.g. {"name": "get_daily", "arguments": {...}}
#   - `await call_next(ctx)` runs the rest of the chain (eventually the tool)
# Unlike fastmcp, the SDK doesn't ship ready-made logging / rate-limit / cache
# middleware, so we write small ones ourselves. Each only acts on tool calls.
# ===========================================================================
def _tool_call(ctx) -> tuple[str, dict] | None:
    """Return (tool name, arguments) if this request is a tool call, else None."""
    if ctx.method != "tools/call" or not ctx.params:
        return None
    return str(ctx.params.get("name")), dict(ctx.params.get("arguments") or {})


# Note: inside middleware, results travel in their "wire" form, a plain dict
# like {"content": [...], "isError": true}, exactly as JSON sent to the client.
def _is_error(result: Any) -> bool:
    """True if a tool result (wire dict or model) is marked as an error."""
    if isinstance(result, dict):
        return bool(result.get("isError"))
    return bool(getattr(result, "is_error", False))


def _error_result(message: str) -> CallToolResult:
    """A tool result marked as an error, which the LLM reads like any other result."""
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


async def audit_log(ctx, call_next):
    """1) Audit log: one line per tool call (name, arguments, duration, outcome)."""
    call = _tool_call(ctx)
    if call is None:
        return await call_next(ctx)
    name, args = call
    t0 = time.perf_counter()
    try:
        result = await call_next(ctx)
    except Exception as e:
        log.warning("tool=%s args=%s FAILED %s", name, json.dumps(args)[:500], type(e).__name__)
        raise
    failed = _is_error(result)
    log.info("tool=%s args=%s ok=%s %.2fs", name, json.dumps(args)[:500], not failed, time.perf_counter() - t0)
    return result


class RateLimiter:
    """2) Rate limit ("token bucket"). The bucket holds up to `burst` tokens and
    refills at `rate` tokens per second. Each tool call takes one token; with an
    empty bucket the call is refused. Protects USGS (and your API key) from a
    runaway agent loop."""

    def __init__(self, rate: float, burst: int):
        self.rate, self.burst = rate, burst
        self.tokens, self.last = float(burst), time.monotonic()

    async def __call__(self, ctx, call_next):
        if _tool_call(ctx) is None:
            return await call_next(ctx)
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            return _error_result("Rate limit reached on the USGS MCP server. Wait a few seconds, then retry.")
        self.tokens -= 1
        return await call_next(ctx)


class ResponseCache:
    """3) Cache: identical tool calls within `ttl` seconds reuse the previous
    answer instead of hitting USGS again. Only successful results are cached."""

    def __init__(self, ttl: float, max_items: int = 500):
        self.ttl, self.max_items = ttl, max_items
        self.store: dict[str, tuple[float, Any]] = {}

    async def __call__(self, ctx, call_next):
        call = _tool_call(ctx)
        if call is None:
            return await call_next(ctx)
        key = json.dumps(call, sort_keys=True, default=str)
        hit = self.store.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        result = await call_next(ctx)
        if not _is_error(result):
            if len(self.store) >= self.max_items:                      # drop the oldest entry
                self.store.pop(next(iter(self.store)))
            self.store[key] = (time.monotonic(), result)
        return result


async def limit_response_size(ctx, call_next):
    """4) Hard cap on response size, a last line of defence behind MAX_ROWS_RETURNED."""
    result = await call_next(ctx)
    if _tool_call(ctx) is not None and result is not None:
        size = len(json.dumps(result, default=str)) if isinstance(result, dict) else len(result.model_dump_json())
        if size > MAX_RESPONSE_BYTES:
            return _error_result(f"Result too large ({size:,} bytes). Narrow the query: fewer sites, "
                                 "a shorter time window, or smaller max_points.")
    return result


# ===========================================================================
# AUTHENTICATION (HTTP mode only)
# ===========================================================================
class StaticTokenVerifier:
    """Accepts exactly one shared secret, sent as 'Authorization: Bearer <token>'.

    The SDK calls verify_token() on every HTTP request. Returning an AccessToken
    means "let it in"; returning None makes the SDK answer 401 Unauthorized.
    hmac.compare_digest compares in constant time, so an attacker can't guess
    the token character by character from response timings.
    """

    def __init__(self, token: str):
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token.encode()):
            return AccessToken(token=token, client_id="usgs-agent", scopes=[])
        return None


# ===========================================================================
# HELPERS — ordinary Python functions, not visible to the LLM
# ===========================================================================
def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Make a DataFrame JSON-friendly: drop map geometry, turn dates into text, NaN into None."""
    df = pd.DataFrame(df).copy()
    if "geometry" in df.columns:
        df = df.drop(columns="geometry")
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].astype(str)
    return df.astype(object).where(pd.notna(df), None)


def _records(df: pd.DataFrame, keep: list[str] | None = None) -> dict[str, Any]:
    """Convert a table into a list of dicts (JSON rows), keeping only useful columns."""
    df = _clean(df)
    if keep:
        df = df[[c for c in keep if c in df.columns]]
    out: dict[str, Any] = {"n_rows": len(df), "rows": df.head(MAX_ROWS_RETURNED).to_dict("records")}
    if len(df) > MAX_ROWS_RETURNED:
        # Tell the LLM the list is incomplete, so it doesn't claim "there are only 200 sites".
        out["note"] = f"Truncated to {MAX_ROWS_RETURNED} of {len(df)} rows; narrow the query."
    return out


def _timeseries_summary(df: pd.DataFrame, max_points: int) -> dict[str, Any]:
    """Summarise a time series instead of returning every value.

    For each (site, parameter, statistic) we return: count, start/end, min, max,
    mean, when the max happened, approval status, and an evenly thinned-out copy
    of the series (every Nth point) so the LLM can still describe the shape.
    """
    df = pd.DataFrame(df)
    if df.empty:
        return {"n_rows": 0, "series": [],
                "note": "No data for this site/parameter/window. Check list_timeseries for what exists."}
    df = df.drop(columns="geometry", errors="ignore")
    df["value"] = pd.to_numeric(df.get("value"), errors="coerce")        # text -> numbers
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)   # text -> timestamps
    keys = [k for k in ("monitoring_location_id", "parameter_code", "statistic_id") if k in df.columns]

    series = []
    for key, grp in (df.groupby(keys) if keys else [((), df)]):
        grp = grp.sort_values("time")
        v = grp["value"]
        has_values = bool(v.notna().any())
        step = max(1, len(grp) // max_points)                            # keep every `step`-th row
        sample = grp.iloc[::step][["time", "value"]].assign(time=lambda x: x["time"].astype(str))
        key = key if isinstance(key, tuple) else (key,)
        series.append({
            **dict(zip(keys, key)),
            "unit": grp["unit_of_measure"].iloc[0] if "unit_of_measure" in grp else None,
            "n_values": int(v.notna().sum()),
            "start": str(grp["time"].iloc[0]),
            "end": str(grp["time"].iloc[-1]),
            "min": float(v.min()) if has_values else None,
            "max": float(v.max()) if has_values else None,
            "mean": round(float(v.mean()), 4) if has_values else None,
            "time_of_max": str(grp.loc[v.idxmax(), "time"]) if has_values else None,
            # 'Provisional' data can still be revised by USGS; 'Approved' is final.
            "approval_status": sorted(grp["approval_status"].dropna().unique().tolist())
                               if "approval_status" in grp else None,
            "sample_every_n": step,
            "values": sample.astype(object).where(pd.notna(sample), None).to_dict("records"),
        })
    return {"n_rows": len(df), "series": series}


async def _call(fn, **kwargs) -> pd.DataFrame:
    """Call a dataretrieval function safely.

    - dataretrieval is *synchronous* (it blocks while waiting for USGS). We run it
      in a background thread (asyncio.to_thread) so the server stays responsive.
    - Timeout: give up after USGS_TIMEOUT_S seconds instead of hanging forever.
    - Retries: on HTTP 429 (rate limited) or 5xx (USGS server trouble), wait and
      try again: 2s, then 4s ("exponential backoff").
    - Arguments that are None are dropped, so we only send filters that were given.
    """
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    last_err: Exception | None = None
    for attempt in range(USGS_RETRIES):
        try:
            df, _metadata = await asyncio.wait_for(asyncio.to_thread(fn, **kwargs), USGS_TIMEOUT_S)
            return df
        except asyncio.TimeoutError:
            raise g.GuardrailError(f"USGS did not answer within {USGS_TIMEOUT_S:.0f}s. "
                                   "Try a shorter time window or fewer sites.")
        except Exception as e:
            last_err = e
            msg = str(e)
            retryable = "429" in msg or any(code in msg for code in ("500", "502", "503", "504"))
            if not retryable or attempt == USGS_RETRIES - 1:
                break
            wait = 2 ** (attempt + 1)
            log.warning("USGS %s failed (%s); retrying in %ss", fn.__name__, msg[:100], wait)
            await asyncio.sleep(wait)
    # Turn the library's error into a short message the LLM can act on.
    raise g.GuardrailError(f"USGS API error in {fn.__name__}: {str(last_err)[:300]}")


# ---------------------------------------------------------------------------
# Reusable argument types. `Annotated[type, Field(description=...)]` is how we
# tell the LLM what each argument means; the MCP SDK turns these into the JSON
# schema the LLM sees. Pydantic also rejects wrong types automatically
# (e.g. text where a number is expected).
# ---------------------------------------------------------------------------
SiteIds = Annotated[list[str], Field(description="Site IDs like ['USGS-01463500']; max 25")]
Pcode = Annotated[str | None, Field(description="5-digit USGS parameter code, e.g. '00060' discharge")]
TimeArg = Annotated[str, Field(description="ISO 8601 interval or duration, e.g. 'P30D' or '2025-01-01/2025-03-31'")]


# ===========================================================================
# TOOLS — the functions the LLM can call.
# These are plain Python functions. build_server() below registers each one
# with server.add_tool(fn). The docstring becomes the tool description the
# LLM reads, and the type hints become its input schema, so write both for the LLM.
# ===========================================================================
def parameter_codes() -> dict[str, str]:
    """Common USGS parameter codes and their meaning."""
    return COMMON_PARAMETER_CODES


async def find_sites(
    state_name: Annotated[str | None, Field(description="Full state name, e.g. 'New Jersey'")] = None,
    county_name: Annotated[str | None, Field(description="e.g. 'Hudson County'")] = None,
    site_type: Annotated[str | None, Field(description="e.g. 'Stream', 'Estuary', 'Well', 'Lake, Reservoir, Impoundment'")] = None,
    hydrologic_unit_code: Annotated[str | None, Field(description="HUC code, e.g. '02030101'")] = None,
    bbox: Annotated[list[float] | None, Field(description="[min_lon, min_lat, max_lon, max_lat] in WGS84")] = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive text to match in the site name, e.g. 'Hudson'")] = None,
) -> dict[str, Any]:
    """Search USGS monitoring locations by state, county, site type, HUC, or bounding box."""
    # Guardrail: require a spatial filter, otherwise this would try to list every USGS site in the country.
    if not any([state_name, county_name, hydrologic_unit_code, bbox]):
        raise g.GuardrailError("Give at least one spatial filter: state_name, county_name, "
                               "hydrologic_unit_code, or bbox.")
    if hydrologic_unit_code and not hydrologic_unit_code.isdigit():
        raise g.GuardrailError("hydrologic_unit_code must be digits, e.g. '02030101'.")

    df = await _call(
        wd.get_monitoring_locations,
        state_name=g.clean_text(state_name, "state_name"),
        county_name=g.clean_text(county_name, "county_name"),
        site_type=g.clean_text(site_type, "site_type"),
        hydrologic_unit_code=hydrologic_unit_code,
        bbox=g.clean_bbox(bbox),
        skip_geometry=True,                  # we don't need map shapes, smaller download
        max_rows=MAX_ROWS_FETCHED,           # never page through more than this
    )
    name_contains = g.clean_text(name_contains, "name_contains")
    if name_contains and not df.empty:
        df = df[df["monitoring_location_name"].str.contains(name_contains, case=False, na=False, regex=False)]
    return _records(df, keep=[
        "monitoring_location_id", "monitoring_location_name", "site_type", "state_name",
        "county_name", "hydrologic_unit_code", "drainage_area", "altitude", "vertical_datum",
    ])


async def site_info(monitoring_location_id: SiteIds) -> dict[str, Any]:
    """Full metadata for specific monitoring locations (location, datum, drainage area, ...)."""
    ids = g.clean_site_ids(monitoring_location_id)
    return _records(await _call(wd.get_monitoring_locations, monitoring_location_id=ids))


async def list_timeseries(monitoring_location_id: SiteIds, parameter_code: Pcode = None) -> dict[str, Any]:
    """What is measured at a site: parameters, statistics, and period of record."""
    df = await _call(wd.get_time_series_metadata,
                     monitoring_location_id=g.clean_site_ids(monitoring_location_id),
                     parameter_code=g.clean_code(parameter_code),
                     skip_geometry=True)
    return _records(df, keep=[
        "monitoring_location_id", "parameter_code", "parameter_name", "statistic_id",
        "computation_period_identifier", "unit_of_measure", "begin", "end", "primary", "web_description",
    ])


async def get_latest(
    monitoring_location_id: SiteIds,
    parameter_code: Pcode = None,
    kind: Literal["continuous", "daily"] = "continuous",
) -> dict[str, Any]:
    """Most recent value for each site/parameter ('continuous' = real-time, 'daily' = latest daily value)."""
    fn = wd.get_latest_continuous if kind == "continuous" else wd.get_latest_daily
    df = await _call(fn, monitoring_location_id=g.clean_site_ids(monitoring_location_id),
                     parameter_code=g.clean_code(parameter_code))
    return _records(df, keep=["monitoring_location_id", "parameter_code", "statistic_id", "time",
                              "value", "unit_of_measure", "approval_status", "qualifier"])


async def get_daily(
    monitoring_location_id: SiteIds,
    parameter_code: Pcode,
    time: TimeArg,
    statistic_id: Annotated[str | None, Field(description="'00003' mean, '00001' max, '00002' min")] = "00003",
    max_points: Annotated[int, Field(ge=10, le=500, description="How many sampled points to return per series")] = 120,
) -> dict[str, Any]:
    """Daily values (default: daily mean) for up to 30 years. Returns summary stats plus a thinned series."""
    df = await _call(
        wd.get_daily,
        monitoring_location_id=g.clean_site_ids(monitoring_location_id),
        parameter_code=g.clean_code(parameter_code),
        statistic_id=g.clean_code(statistic_id, "statistic_id"),
        time=g.check_time_window(time, g.MAX_DAYS_DAILY),
        skip_geometry=True,
        max_rows=MAX_ROWS_FETCHED,
    )
    return _timeseries_summary(df, max_points)


async def get_continuous(
    monitoring_location_id: SiteIds,
    parameter_code: Pcode,
    time: TimeArg,
    max_points: Annotated[int, Field(ge=10, le=500, description="How many sampled points to return per series")] = 150,
) -> dict[str, Any]:
    """Instantaneous (typically 15-minute) values, max 31 days. Use get_daily for longer periods."""
    df = await _call(
        wd.get_continuous,
        monitoring_location_id=g.clean_site_ids(monitoring_location_id),
        parameter_code=g.clean_code(parameter_code),
        time=g.check_time_window(time, g.MAX_DAYS_CONTINUOUS),
        max_rows=MAX_ROWS_FETCHED,
    )
    return _timeseries_summary(df, max_points)


async def get_peaks(monitoring_location_id: SiteIds, parameter_code: Pcode = "00060") -> dict[str, Any]:
    """Annual peak streamflow ('00060') or peak gage height ('00065') records."""
    df = await _call(wd.get_peaks, monitoring_location_id=g.clean_site_ids(monitoring_location_id),
                     parameter_code=g.clean_code(parameter_code), skip_geometry=True,
                     max_rows=MAX_ROWS_FETCHED)
    return _records(df)


# ===========================================================================
# BUILDING THE SERVER
# ===========================================================================
TOOLS = [parameter_codes, find_sites, site_info, list_timeseries,
         get_latest, get_daily, get_continuous, get_peaks]


def build_server(token: str | None = None, public_url: str = "http://127.0.0.1:8000") -> MCPServer:
    """Create the MCP server, register the tools, and attach the guardrails.

    Why a function instead of one global object? In the SDK, authentication is
    configured when the server is created. stdio mode needs no token, HTTP mode
    does, so we build the server with or without auth depending on the mode.
    """
    auth_kwargs: dict[str, Any] = {}
    if token:
        auth_kwargs = {
            "token_verifier": StaticTokenVerifier(token),
            # AuthSettings is designed for full OAuth; with a static token these
            # URLs are only used in metadata the SDK advertises to clients.
            "auth": AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp"),
        }

    server = MCPServer(
        "usgs-water-data",
        instructions=INSTRUCTIONS,
        # Extra packages `mcp dev` / `mcp install` must add to the temporary
        # environment they create with `uv` (they only include `mcp` by default).
        dependencies=["dataretrieval>=1.3", "pandas>=2.2"],
        # Outermost first: log everything, then rate limit, then cache, then size check.
        middleware=[audit_log, RateLimiter(RATE_LIMIT_PER_S, RATE_LIMIT_BURST),
                    ResponseCache(CACHE_TTL_S), limit_response_size],
        **auth_kwargs,
    )
    for fn in TOOLS:
        server.add_tool(fn)          # same as decorating the function with @server.tool()
    return server


# A module-level server with no auth. `mcp dev server.py` and `mcp run server.py`
# look for a variable named `mcp`, and stdio mode uses it too.
mcp = build_server()


# ===========================================================================
# ENTRY POINT — runs when you type `python server.py ...`
# ===========================================================================
def main() -> None:
    p = argparse.ArgumentParser(description="USGS Water Data MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                   help="stdio: talk to one local program. http: listen on the network.")
    p.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 = only this machine can connect (safe default). 0.0.0.0 = any network interface.")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()

    if a.transport == "stdio":
        # stdio: the client starts this script as a subprocess and talks through
        # stdin/stdout. No network port is opened, so no token is needed.
        mcp.run(transport="stdio")
        return

    # HTTP mode: anyone who can reach the port could call the tools, so we
    # require a bearer token (a shared secret sent in the Authorization header).
    token = os.getenv("MCP_TOKEN")
    if not token or len(token) < 32:
        raise SystemExit("Refusing to start: set MCP_TOKEN to a long random string "
                         "(e.g. `openssl rand -hex 32`) before using --transport http.")
    if not os.getenv("API_USGS_PAT"):
        log.warning("API_USGS_PAT not set: you will hit USGS rate limits (HTTP 429) quickly.")

    server = build_server(token=token, public_url=f"http://{a.host}:{a.port}")
    log.info("Serving MCP on http://%s:%s/mcp", a.host, a.port)
    # "streamable-http" is the SDK's name for the modern HTTP transport.
    server.run(transport="streamable-http", host=a.host, port=a.port, streamable_http_path="/mcp")


if __name__ == "__main__":
    main()
