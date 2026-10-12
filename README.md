# USGS Water Data MCP server + Claude agent (AWS-ready)

Ask questions in plain English, such as *"What was the peak discharge on the Delaware River at Trenton this month?"*.
Claude answers by querying live USGS data through an MCP server that you run on an AWS EC2 instance.

---

## 1. The concepts, in plain terms

**LLM (Claude).** Already trained; we don't train anything. It's good at reading a question, deciding what information it needs, and writing an answer. On its own it has no live data.

**Tool.** A normal Python function we allow the LLM to call, for example `find_sites(state_name="New Jersey")`. The LLM never runs code itself. It *asks* for a tool call, and our program runs it.

**MCP (Model Context Protocol).** A standard format for publishing tools, so any MCP-aware app can use them. Examples include our `agent.py`, Claude Desktop, and IDEs. You write the tools once, and every client can use them.

**MCP server (`server.py`).** Publishes the tools and runs them when asked.

**MCP client / agent (`agent.py`).** Connects to the server, shows the tool list to Claude, and runs this loop:

```
 you: "latest flow at Trenton?"
   │
   ▼
 agent.py ──question + tool list──▶ Claude
   ▲                                  │ "call find_sites(name_contains='Trenton', state_name='New Jersey')"
   │                                  ▼
   │                          agent.py ──▶ server.py ──▶ USGS Water Data API
   │                                  ◀── result ◀──
   │                                  │ (Claude may call more tools...)
   └──────────── final answer ◀───────┘
```

**Transport.** How client and server talk:
- `stdio`: the client starts the server as a child process on the same machine. There's no network, so it's simplest for learning.
- `http`: the server listens on a port (8000), which is what you use on EC2. Anyone who can reach the port could call it, which is why it needs a token.

---

## 2. What each file does

| File | Purpose |
|---|---|
| `server.py` | The MCP server (official MCP Python SDK v2): 8 USGS tools, plus server-wide guardrails (rate limit, cache, logging, size cap) |
| `guardrails.py` | All input checks: site IDs, parameter codes, time windows, bounding boxes |
| `agent.py` | Interactive chat: the loop between you, Claude, and the tools, with agent-side limits |
| `ui.py` | Web interface for demos: chat, a live 🔧 panel for each tool call, and a hydrograph |
| `smoke_test.py` | Checks a deployed server against live USGS data, with no LLM involved |
| `DEPLOY.md` | Step-by-step EC2 deployment and testing guide |
| `deploy/usgs-ui.service` | systemd unit that keeps the web interface running |
| `tests/test_guardrails.py` | Tests you can run with no internet or API key |
| `requirements.txt` | Python packages |
| `.env.example` | Template for your settings. Copy it to `.env`; never commit the real one |
| `.gitignore` | Keeps secrets and clutter out of GitHub |
| `Dockerfile` | Optional: run the server in a container instead of a venv |
| `deploy/usgs-mcp.service` | systemd unit: keeps the server running on EC2, restarts it on crash |
| `deploy/load-secrets.sh` | Pulls secrets from AWS SSM Parameter Store into `/etc/usgs-mcp.env` |

**Data source:** `dataretrieval.waterdata` → the modernized USGS Water Data APIs. The older `dataretrieval.nwis` module uses WaterServices, which USGS is retiring in Q1 2027.

### The tools
| Tool | What it answers |
|---|---|
| `find_sites` | "Which gauges are in Hudson County / this HUC / this box?" |
| `site_info` | "Where exactly is USGS-01463500, and what's its drainage area?" |
| `list_timeseries` | "What does this site measure, and since when?" |
| `get_latest` | "What's the flow right now?" |
| `get_daily` | Daily mean/min/max, up to 30 years (returned as stats + a thinned series) |
| `get_continuous` | 15-minute data, up to 31 days (returned as stats + a thinned series) |
| `get_peaks` | Annual peak flow or stage |
| `parameter_codes` | Lookup: 00060 = discharge, 00065 = gage height, ... |

---

## 3. Guardrails: what they are and where they live

A guardrail is a limit that stops the system from doing something harmful, expensive, or wrong, even when the LLM asks for it. There are two kinds:

- **Hard guardrails** are code. They *always* apply.
- **Soft guardrails** are instructions in the system prompt. Claude follows them reliably, but nothing forces it to, so anything important also gets a hard guardrail.

| Risk | Guardrail | Where |
|---|---|---|
| Strangers calling your server | Bearer token required. The server refuses to start in HTTP mode without a 32+ character token | `server.py` `main()` |
| Server exposed to the internet | Binds to `127.0.0.1` by default; security group allows only SSH | `server.py`, `.service`, section 5 |
| LLM invents a site ID or sends junk | Regex validation. Bare numbers get a `USGS-` prefix | `guardrails.py` |
| Enormous downloads (20 yrs of 15-min data) | Max 31 days continuous, 30 yrs daily, 25 sites/call, bbox ≤ 25 deg², a spatial filter required for site search, `max_rows` cap | `guardrails.py`, `server.py` |
| Flooding USGS / burning your API key | 2 req/s rate limit per client, 5-min response cache, retry with backoff on 429/5xx, 60s timeout | `server.py` middleware + `_call()` |
| Filling Claude's context window | Summaries instead of raw rows, 200-row cap, 200 KB response cap, 20k-char cap per tool result in agent | `server.py`, `agent.py` |
| Leaking internals in error messages | `mask_error_details=True`: crashes show a generic error; only our friendly `GuardrailError` messages pass through | `server.py`, `guardrails.py` |
| Runaway agent loop / surprise bill | Max 10 LLM rounds, 15 tool calls, 150k tokens per question; history trimmed to 40 messages | `agent.py` |
| A new, unexpected tool appears on the server | Allowlist of tool names | `agent.py` `ALLOWED_TOOLS` |
| Prompt injection via returned data | Tool output wrapped in `<tool_result>` tags; the prompt says treat it as data | `agent.py` `SYSTEM` |
| Made-up numbers ("hallucination") | Prompt: report only numbers from tool results, with units, window, approval status | `agent.py` `SYSTEM` (soft) |
| Unsafe advice ("is it safe to cross?") | Prompt: decline, point to NWS | `agent.py` `SYSTEM` (soft) |
| Secrets in GitHub | `.gitignore`, secrets from env vars / SSM only | `.gitignore`, `deploy/load-secrets.sh` |
| No record of what happened | Every request is logged (`journalctl -u usgs-mcp`) | `server.py` `LoggingMiddleware` |

**Honest limits of this setup:** the token is a single shared secret (fine for you and a few colleagues, not for public users; for those, switch to OAuth, e.g. AWS Cognito as the authorization server, which the MCP SDK supports through `AuthSettings`). Hallucination can be reduced but never fully eliminated by a prompt, so spot-check answers against waterdata.usgs.gov while you're testing. Amazon Bedrock Guardrails can add content filtering on top if you need it.

---

## 4. Try it on your laptop first

```bash
git clone <your repo> && cd usgs-mcp
python3 -m venv .venv && source .venv/bin/activate     # an isolated Python environment
pip install -r requirements.txt

pytest -q                           # 19 tests; needs nothing else. All should pass.

export API_USGS_PAT=...             # free key: https://api.waterdata.usgs.gov/signup/
export ANTHROPIC_API_KEY=...        # or use Bedrock, see section 6
python agent.py                     # starts server.py automatically over stdio
```
You'll see each tool call printed as `-> find_sites({...})`, so you can watch Claude's reasoning.

**Poke the tools by hand (no LLM):** `mcp dev server.py` opens the MCP Inspector, a web UI where you can fill in arguments and see raw results. This is the best way to learn what the LLM sees.

---

## 5. Put the server on EC2

1. **Launch an instance.** Ubuntu 24.04, `t3.small`. There's no GPU, because the LLM runs at Anthropic/Bedrock, and this server only makes web requests.
2. **Security group (the instance's firewall).** Inbound: allow only **SSH (22) from your IP**. Do *not* open 8000. You'll reach the server through an SSH tunnel.
3. **IAM role** attached to the instance, with `ssm:GetParameter` on `/usgs-mcp/*` (+ `kms:Decrypt`). If the agent will run on EC2 with Bedrock, also add `bedrock:InvokeModel`.
4. **Store secrets** (from your laptop). See the commands at the top of `deploy/load-secrets.sh`.
5. **Install on the instance:**
   ```bash
   ssh ubuntu@<ec2-public-ip>
   sudo apt update && sudo apt install -y python3-venv git awscli
   sudo mkdir -p /opt/usgs-waterdata-mcp && sudo chown ubuntu /opt/usgs-waterdata-mcp
   git clone <your repo> /opt/usgs-waterdata-mcp && cd /opt/usgs-waterdata-mcp
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/pytest -q
   ./deploy/load-secrets.sh
   sudo cp deploy/usgs-mcp.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now usgs-mcp
   systemctl status usgs-mcp          # should say "active (running)"
   journalctl -u usgs-mcp -f          # live log; Ctrl+C to exit
   ```
6. **Connect from your laptop through a tunnel:**
   ```bash
   ssh -N -L 8000:127.0.0.1:8000 ubuntu@<ec2-public-ip>     # leave this running
   # in another terminal:
   export MCP_URL=http://127.0.0.1:8000/mcp MCP_TOKEN=<same token as in SSM>
   python agent.py
   ```
   The tunnel encrypts traffic and means port 8000 is never open to the internet.

**Later, to make it public** (e.g. to add it as a custom connector in Claude): put HTTPS in front with Caddy, nginx + Let's Encrypt, or an ALB + ACM certificate. Open only 443, keep the token, and preferably move to OAuth.

---

## 6. Using Bedrock instead of an Anthropic API key
Enable a Claude model in the Bedrock console (Model access), then:
```bash
export LLM_PROVIDER=bedrock AWS_REGION=us-east-1
export LLM_MODEL=<model ID or inference profile shown in the Bedrock console>
python agent.py
```
On EC2 this uses the instance role, so no key is stored anywhere.

---

## 7. Troubleshooting
| Symptom | Likely cause |
|---|---|
| `HTTP 429` in answers | No or invalid `API_USGS_PAT`, or too many requests. Wait, or check the key |
| `401 Unauthorized` | `MCP_TOKEN` on the client differs from the server's |
| `Refusing to start: set MCP_TOKEN` | Working as intended; see section 5, step 4 |
| Agent says a site has no data | Run `list_timeseries` for it. Not every site measures every parameter |
| `Connection refused` on 8000 | Tunnel not running, or `systemctl status usgs-mcp` shows it crashed. Check `journalctl` |
| Bedrock `AccessDeniedException` | Model access not enabled, or the instance role lacks `bedrock:InvokeModel` |
