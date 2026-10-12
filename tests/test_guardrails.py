"""
Tests for guardrails.py. Run with:   pytest -q
These need no internet, no USGS key, and no LLM, so they are a safe first thing to run.
Each test says: "given this input, the guardrail should accept it / reject it".
"""
import pytest

import guardrails as g


def test_site_ids_are_normalised():
    assert g.clean_site_ids(["01463500"]) == ["USGS-01463500"]          # prefix added
    assert g.clean_site_ids(["USGS-01463500", "USGS-01463500"]) == ["USGS-01463500"]  # de-duplicated


@pytest.mark.parametrize("bad", ["Trenton", "USGS 01463500", "USGS-0146;DROP", ""])
def test_bad_site_ids_rejected(bad):
    with pytest.raises(g.GuardrailError):
        g.clean_site_ids([bad])


def test_too_many_sites_rejected():
    with pytest.raises(g.GuardrailError):
        g.clean_site_ids([f"USGS-{i:08d}" for i in range(g.MAX_SITES_PER_CALL + 1)])


def test_parameter_codes():
    assert g.clean_code("00060") == "00060"
    assert g.clean_code(None) is None
    with pytest.raises(g.GuardrailError):
        g.clean_code("discharge")


@pytest.mark.parametrize("ok", ["P7D", "PT12H", "2025-01-01/2025-01-31", "2025-06-01/..", "2025-01-01"])
def test_valid_time_windows(ok):
    assert g.check_time_window(ok, max_days=3660) == ok


@pytest.mark.parametrize("bad", [
    "last week",                 # natural language
    "../2025-01-01",             # no start date
    "2025-02-01/2025-01-01",     # end before start
    "P2Y",                       # longer than the limit below
    "",
])
def test_invalid_time_windows(bad):
    with pytest.raises(g.GuardrailError):
        g.check_time_window(bad, max_days=366)


def test_continuous_limit():
    g.check_time_window("P31D", g.MAX_DAYS_CONTINUOUS)
    with pytest.raises(g.GuardrailError):
        g.check_time_window("P90D", g.MAX_DAYS_CONTINUOUS)


def test_bbox():
    assert g.clean_bbox([-74.3, 40.5, -73.7, 41.0])
    with pytest.raises(g.GuardrailError):
        g.clean_bbox([-73.7, 40.5, -74.3, 41.0])     # min_lon > max_lon (reversed)
    with pytest.raises(g.GuardrailError):
        g.clean_bbox([-125, 25, -66, 49])            # whole continental US: too big
