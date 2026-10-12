"""
ui.py — a web interface for demos: chat with Claude about USGS water data,
watch every tool call it makes, and see the hydrograph it pulled.

It reuses the same agent loop as agent.py (agent_events), so the guardrails
there (tool allowlist, token budget, call limits) apply here too.

HOW IT FITS TOGETHER
--------------------
  browser ──> ui.py (Gradio, port 7860) ──> Claude (Anthropic API or Bedrock)
                        │
                        └──> MCP server (server.py, port 8000) ──> USGS

RUN
---
  export MCP_URL=http://127.0.0.1:8000/mcp  MCP_TOKEN=...   (server must be running)
  export ANTHROPIC_API_KEY=...   (or the Bedrock variables, see agent.py)
  python ui.py                   # then open http://127.0.0.1:7860

EXTRA SETTINGS (environment variables)
  UI_HOST        default 127.0.0.1 (only this machine). 0.0.0.0 requires a password.
  UI_PORT        default 7860
  UI_USERNAME / UI_PASSWORD   turn on a login screen (required for UI_HOST=0.0.0.0)
  UI_MAX_QUESTIONS            questions allowed per browser session (default 30), a cost guardrail
"""
from __future__ import annotations

import json
import os

import gradio as gr       # builds web UIs from Python; no HTML/JavaScript needed
import pandas as pd

import agent              # our agent loop, system prompt, and limits

MAX_QUESTIONS = int(os.getenv("UI_MAX_QUESTIONS", "30"))
HISTORY_MESSAGES = 12     # how many earlier chat messages Claude sees (keeps cost down)
PREVIEW_CHARS = 1500      # how much of each raw tool result to show in the tool panel

EXAMPLES = [
    "What is the discharge right now on the Delaware River at Trenton, NJ?",
    "Which stream gauges are in Hudson County, New Jersey?",
    "Plot daily mean discharge for the Hudson River at Green Island, NY over the last 90 days.",
    "What were the five largest annual peak flows at USGS-01463500, and in which years?",
    "What does USGS-01376500 measure, and since when?",
]

HEADER = """
# 💧 USGS Water Data Assistant
Ask about USGS stream gauges in plain English. Claude answers by calling **MCP tools** that query the
live USGS Water Data APIs; open the 🔧 panels to see each call and its raw result.
Values marked *Provisional* may still be revised by USGS. Not for flood warnings: see weather.gov.
"""

llm = agent.make_llm()            # one Claude client for the whole app
questions_per_session: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _text_of(content) -> str:
    """Gradio may store message content as a string or as a list of parts; return plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def history_to_claude(history: list[dict]) -> list[dict]:
    """Turn the visible chat into Claude messages.

    Tool panels (messages with a metadata title) are display-only and are skipped.
    Claude sees the recent questions and answers; if it needs data again it
    simply calls the tool again (the server's cache makes that cheap).
    """
    msgs = []
    for m in history[-HISTORY_MESSAGES:]:
        if (m.get("metadata") or {}).get("title"):
            continue
        text = _text_of(m.get("content"))
        if text and m.get("role") in ("user", "assistant"):
            msgs.append({"role": m["role"], "content": text})
    while msgs and msgs[0]["role"] != "user":        # Claude requires the first message to be from the user
        msgs.pop(0)
    merged: list[dict] = []                          # and roles to alternate
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(m)
    return merged


def series_to_frame(tool_text: str) -> pd.DataFrame | None:
    """If a tool result contains time series (get_daily / get_continuous), return them as a table for plotting."""
    try:
        data = json.loads(tool_text)
    except (ValueError, TypeError):
        return None
    rows = []
    for s in data.get("series", []) if isinstance(data, dict) else []:
        label = f"{s.get('monitoring_location_id', '')} {s.get('parameter_code', '')} ({s.get('unit') or ''})"
        for point in s.get("values", []):
            if point.get("value") is not None:
                rows.append({"time": point["time"], "value": point["value"], "series": label.strip()})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True).dt.tz_localize(None)
    return df.dropna(subset=["time"])


# ---------------------------------------------------------------------------
# The chat function Gradio calls for every question.
# It is an async generator: each `yield` updates the page, so tool calls
# appear live while Claude is still working.
# It yields (chat messages, plot); gr.skip() means "leave the plot unchanged".
# ---------------------------------------------------------------------------
async def respond(message: str, history: list[dict], request: gr.Request):
    session = request.session_hash if request else "local"
    used = questions_per_session.get(session, 0)
    if used >= MAX_QUESTIONS:
        yield [gr.ChatMessage(content=f"This demo session reached its limit of {MAX_QUESTIONS} questions. "
                                      "Reload the page to start a new session.")], gr.skip()
        return
    if len(message) > agent.MAX_QUESTION_CHARS:
        yield [gr.ChatMessage(content=f"Question too long (max {agent.MAX_QUESTION_CHARS} characters).")], gr.skip()
        return
    questions_per_session[session] = used + 1

    messages = history_to_claude(history) + [{"role": "user", "content": message}]
    shown: list[gr.ChatMessage] = []
    open_panels: list[gr.ChatMessage] = []     # tool panels still waiting for their result
    plot = gr.skip()

    try:
        async with agent.make_mcp_client() as mcp:
            tools = await agent.list_allowed_tools(mcp)
            async for ev in agent.agent_events(llm, mcp, tools, messages):
                if ev["type"] == "tool_call":
                    panel = gr.ChatMessage(
                        content=f"**Arguments**\n```json\n{json.dumps(ev['input'], indent=2)}\n```",
                        metadata={"title": f"🔧 {ev['name']}", "status": "pending"},
                    )
                    shown.append(panel)
                    open_panels.append(panel)
                elif ev["type"] == "tool_result":
                    panel = next((p for p in open_panels if p.metadata["title"].endswith(ev["name"])), None)
                    if panel:
                        open_panels.remove(panel)
                        preview = ev["text"][:PREVIEW_CHARS] + ("\n…" if len(ev["text"]) > PREVIEW_CHARS else "")
                        panel.content += f"\n**Result**\n```\n{preview}\n```"
                        panel.metadata["status"] = "done"
                        if ev["is_error"]:
                            panel.metadata["title"] = f"⚠️ {ev['name']} (error)"
                    df = series_to_frame(ev["text"])
                    if df is not None:
                        plot = df
                elif ev["type"] == "answer":
                    shown.append(gr.ChatMessage(content=ev["text"]))
                yield shown, plot
    except Exception as e:   # MCP server down, bad API key, Bedrock access, network...
        shown.append(gr.ChatMessage(content=f"⚠️ Something went wrong: {type(e).__name__}: {e}\n\n"
                                            "Check that the MCP server is running and the LLM settings are correct."))
        yield shown, plot


# ---------------------------------------------------------------------------
# Page layout: chat on the left, hydrograph on the right.
# ---------------------------------------------------------------------------
with gr.Blocks(title="USGS Water Data Assistant") as demo:
    gr.Markdown(HEADER)
    with gr.Row():
        with gr.Column(scale=3):
            plot = gr.LinePlot(x="time", y="value", color="series", title="Latest time series retrieved",
                               x_title="Time", y_title="Value", height=420, render=False)
            gr.ChatInterface(
                respond,
                additional_outputs=[plot],
                examples=EXAMPLES,
                fill_height=True,
                concurrency_limit=4,          # at most 4 questions processed at once
            )
        with gr.Column(scale=2):
            plot.render()
            gr.Markdown("The chart shows the last time series a tool returned (thinned to ≤150 points; "
                        "the summary statistics Claude reports are computed from all values).")


def main() -> None:
    host = os.getenv("UI_HOST", "127.0.0.1")
    port = int(os.getenv("UI_PORT", "7860"))
    user, password = os.getenv("UI_USERNAME"), os.getenv("UI_PASSWORD")
    auth = (user, password) if user and password else None
    if host != "127.0.0.1" and not auth:
        raise SystemExit("Refusing to listen on the network without a login: set UI_USERNAME and UI_PASSWORD.")
    demo.queue().launch(server_name=host, server_port=port, auth=auth, ssr_mode=False, show_error=False)


if __name__ == "__main__":
    main()
