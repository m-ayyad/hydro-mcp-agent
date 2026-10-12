"""
guardrails.py — every safety check the MCP server applies, in one place.

WHY THIS FILE EXISTS
--------------------
An LLM decides which tool to call and what arguments to pass. LLMs are good
at this, but not perfect: they can invent a site ID, ask for 20 years of
15-minute data, or pass "last week" instead of a proper date. Anything a tool
accepts, the LLM will eventually send.

So the server never trusts its inputs. Each function here either returns a
cleaned-up value or raises GuardrailError with a message written FOR THE LLM,
explaining what went wrong and how to fix it. The LLM reads that message and
usually retries correctly on its own.

These are "hard" guardrails: plain Python code that always runs. They are
different from "soft" guardrails (instructions in the system prompt, see
agent.py), which the model usually follows but is not forced to.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd
from mcp.server.mcpserver.exceptions import ToolError


class GuardrailError(ToolError):
    """Raised when a tool input breaks a rule.

    It subclasses the MCP SDK's ToolError on purpose. The SDK hides the text of
    unexpected crashes from the client (it only says "Error executing tool X"),
    but ToolError messages are passed through, so the LLM still sees our
    helpful "here is how to fix it" text.
    """

# ---------------------------------------------------------------------------
# Limits. Tune these to your needs; they are deliberately conservative.
# ---------------------------------------------------------------------------
MAX_SITES_PER_CALL = 25          # how many site IDs one tool call may request
MAX_DAYS_CONTINUOUS = 31         # 15-min data: ~96 values/day, so 31 days ≈ 3,000 rows per site
MAX_DAYS_DAILY = 366 * 30        # daily values: 30 years is fine (~11,000 rows per site)
MAX_BBOX_AREA_DEG2 = 25.0        # a 5°x5° box is roughly the size of New York State
MAX_TEXT_LEN = 60                # for free-text filters like state/county names

# ---------------------------------------------------------------------------
# Regular expressions describing what a valid value looks like.
# A regex is a pattern; re.fullmatch() checks that the WHOLE string fits it.
# ---------------------------------------------------------------------------
SITE_ID_RE = re.compile(r"[A-Z0-9]{2,10}-[A-Za-z0-9]{1,20}")   # e.g. USGS-01463500
PCODE_RE = re.compile(r"\d{5}")                                  # e.g. 00060
TEXT_RE = re.compile(r"[A-Za-z0-9 .,'()\-]+")                   # letters, digits, basic punctuation
# ISO 8601 duration such as P7D, P1M, P1Y2M, PT12H
DURATION_RE = re.compile(
    r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?"
)


def clean_site_ids(ids: list[str]) -> list[str]:
    """Normalise and validate a list of monitoring-location IDs.

    Accepts '01463500' (bare USGS number) and turns it into 'USGS-01463500',
    because LLMs often drop the agency prefix.
    """
    if not ids:
        raise GuardrailError("Provide at least one monitoring_location_id, e.g. 'USGS-01463500'. "
                         "Use find_sites to look IDs up.")
    if len(ids) > MAX_SITES_PER_CALL:
        raise GuardrailError(f"Too many sites ({len(ids)}); max {MAX_SITES_PER_CALL} per call. Split the request.")
    out = []
    for raw in ids:
        s = str(raw).strip()
        if s.isdigit():                       # bare number -> assume USGS
            s = f"USGS-{s}"
        if not SITE_ID_RE.fullmatch(s):
            raise GuardrailError(f"'{raw}' is not a valid site ID. Expected form 'USGS-01463500'.")
        out.append(s)
    return list(dict.fromkeys(out))           # remove duplicates, keep order


def clean_code(code: str | None, what: str = "parameter_code") -> str | None:
    """Validate a 5-digit USGS code (parameter or statistic). None means 'not given'."""
    if code is None:
        return None
    code = str(code).strip()
    if not PCODE_RE.fullmatch(code):
        raise GuardrailError(f"{what} must be 5 digits, e.g. '00060' for discharge. Got '{code}'. "
                         "Call parameter_codes to see common codes.")
    return code


def clean_text(value: str | None, field: str) -> str | None:
    """Validate short free-text filters (state names, county names, site types)."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > MAX_TEXT_LEN or not TEXT_RE.fullmatch(value):
        raise GuardrailError(f"{field} contains unexpected characters or is too long: '{value[:80]}'.")
    return value


def clean_bbox(bbox: list[float] | None) -> list[float] | None:
    """Check [min_lon, min_lat, max_lon, max_lat] is a sensible, not-too-large box."""
    if bbox is None:
        return None
    if len(bbox) != 4:
        raise GuardrailError("bbox must have 4 numbers: [min_lon, min_lat, max_lon, max_lat].")
    x0, y0, x1, y1 = map(float, bbox)
    if not (-180 <= x0 < x1 <= 180 and -90 <= y0 < y1 <= 90):
        raise GuardrailError("bbox out of range or reversed. Order is [min_lon, min_lat, max_lon, max_lat]; "
                         "longitudes in the US are negative (e.g. -74.3).")
    area = (x1 - x0) * (y1 - y0)
    if area > MAX_BBOX_AREA_DEG2:
        raise GuardrailError(f"bbox covers {area:.1f} square degrees; max is {MAX_BBOX_AREA_DEG2}. Use a smaller box "
                         "or filter by state_name instead.")
    return [x0, y0, x1, y1]


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------
def _duration_days(text: str) -> float | None:
    """'P7D' -> 7.0, 'P1M' -> 30.4, 'PT12H' -> 0.5. Returns None if not a duration."""
    m = DURATION_RE.fullmatch(text)
    if not m or text in ("P", "PT"):
        return None
    y, mo, w, d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return y * 365.25 + mo * 30.44 + w * 7 + d + h / 24 + mi / 1440 + s / 86400


def _timestamp(text: str) -> pd.Timestamp:
    ts = pd.Timestamp(text)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts


def check_time_window(time: str, max_days: float) -> str:
    """Make sure a time argument is well formed, bounded, and not too long.

    Accepted forms (all ISO 8601, which is what the USGS API speaks):
      'P7D'                     the last 7 days
      '2025-01-01/2025-03-31'   a closed interval
      '2025-01-01/..'           from a date until now
      '2025-01-01'              a single day
    Rejected: '../2025-01-01' (no start = potentially the whole record).
    """
    t = (time or "").strip()
    if not t:
        raise GuardrailError("A time window is required, e.g. 'P7D' or '2025-01-01/2025-01-31'.")

    days = _duration_days(t)
    if days is None:
        try:
            if "/" in t:
                start_s, end_s = (p.strip() for p in t.split("/", 1))
                if start_s in ("", ".."):
                    raise GuardrailError("Open-ended start ('../date') is not allowed; give a start date.")
                start = _timestamp(start_s)
                end = datetime.now(timezone.utc) if end_s in ("", "..") else _timestamp(end_s)
                if end <= start:
                    raise GuardrailError(f"End ({end_s}) must be after start ({start_s}).")
                days = (end - start).total_seconds() / 86400
            else:
                _timestamp(t)          # just check it parses
                days = 1
        except GuardrailError:
            raise                      # our own message: pass it through unchanged
        except Exception:              # anything pandas couldn't parse
            raise GuardrailError(f"Could not understand time '{t}'. Use ISO 8601 like 'P30D' or "
                             "'2025-01-01/2025-01-31' (natural language like 'last week' is not accepted).")

    if days > max_days:
        raise GuardrailError(f"Time window is {days:.0f} days; this tool allows at most {max_days:.0f}. "
                         "Use a shorter window (or get_daily instead of get_continuous for long periods).")
    return t
