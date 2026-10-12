# Deploying and testing on EC2

This guide assumes an **Ubuntu** instance with the default `ubuntu` user. Everything lives in
`/opt/usgs-waterdata-mcp` with its **own virtual environment**, so it cannot interfere with other
projects on the machine. (On Amazon Linux the user is `ec2-user` and the package manager is `dnf`;
replace those where they appear, including in the two `.service` files.)

The plan, one layer at a time:

| Stage | What you prove | Tool |
|---|---|---|
| 1–3 | The code installs and its tests pass on the instance | `pytest` |
| 4 | The MCP server reaches live USGS data | `smoke_test.py` (no LLM) |
| 5 | Claude can use the tools | `agent.py` (terminal chat) |
| 6 | The web interface works | `ui.py` in your laptop's browser |
| 7 | Everything survives logout and reboot | systemd services |
| 8 | Others can see it | showcase options |

Only move to the next stage when the current one works. If something breaks, you then know which layer to look at.

---

## 1. Prepare the instance

From your laptop:
```bash
ssh -i ~/path/to/key.pem ubuntu@<EC2_PUBLIC_IP>
```

On the instance:
```bash
python3 --version                       # need 3.10 or newer
sudo apt update && sudo apt install -y python3-venv unzip
ss -ltnp | grep -E ':8000|:7860'        # anything printed = the port is already used by another project
```

- **Python older than 3.10?** Install `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`, then open a new shell). In step 2, create the venv with `uv venv --python 3.12 .venv` instead. This downloads its own Python and leaves the system one alone.
- **Port 8000 or 7860 taken?** Pick free ones (e.g. 8010 and 7870) and use them everywhere below: `MCP_URL`, `UI_PORT`, `--port` in `deploy/usgs-mcp.service`, and the SSH tunnels.

**Security group:** inbound only needs **SSH (22) from your IP**. Do **not** open 8000 or 7860; you'll reach both through SSH tunnels.

## 2. Copy the code and create the venv

From your **laptop** (in the folder containing the zip):
```bash
scp -i ~/path/to/key.pem usgs-waterdata-mcp.zip ubuntu@<EC2_PUBLIC_IP>:~
```
(Once it's on GitHub: `git clone <repo> /opt/usgs-waterdata-mcp` instead.)

On the **instance**:
```bash
sudo mkdir -p /opt/usgs-waterdata-mcp && sudo chown ubuntu:ubuntu /opt/usgs-waterdata-mcp
unzip ~/usgs-waterdata-mcp.zip -d /tmp && cp -r /tmp/usgs-waterdata-mcp/. /opt/usgs-waterdata-mcp/
cd /opt/usgs-waterdata-mcp

python3 -m venv .venv                   # the project's private Python environment
source .venv/bin/activate               # your prompt now starts with (.venv)
pip install --upgrade pip
pip install -r requirements.txt
```

`source .venv/bin/activate` only affects the current terminal. Every new SSH session needs `cd /opt/usgs-waterdata-mcp && source .venv/bin/activate` again. The systemd services in step 7 call `.venv/bin/python` directly, so they don't need it.

## 3. Run the offline tests

```bash
pytest -q                               # expect: 19 passed
```

## 4. Settings file, then test the MCP server alone

Create the settings file. It holds secrets, so only your user can read it:
```bash
openssl rand -hex 32                    # copy this: it's your MCP_TOKEN
sudo cp .env.example /etc/usgs-mcp.env
sudo chown ubuntu:ubuntu /etc/usgs-mcp.env && sudo chmod 600 /etc/usgs-mcp.env
nano /etc/usgs-mcp.env
```
Fill in `API_USGS_PAT`, `MCP_TOKEN`, and **one** LLM option (Anthropic key, or the Bedrock lines).
Keep `MCP_URL=http://127.0.0.1:8000/mcp`. For the web UI's login, also add:
```
UI_USERNAME=demo
UI_PASSWORD=choose-a-long-password
```
(The AWS SSM route in `deploy/load-secrets.sh` is the tidier option later; a file is fine for testing.)

**Terminal A**: start the server in the foreground, so you can watch its log:
```bash
cd /opt/usgs-waterdata-mcp && source .venv/bin/activate
set -a; source /etc/usgs-mcp.env; set +a       # load the settings into this shell
python server.py --transport http --port 8000
```
It should print `Serving MCP on http://127.0.0.1:8000/mcp`.

**Terminal B** (a second SSH session):
```bash
cd /opt/usgs-waterdata-mcp && source .venv/bin/activate
set -a; source /etc/usgs-mcp.env; set +a
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/mcp   # expect 401 (no token = rejected)
python smoke_test.py                                                         # expect 6/6 checks passed
```
Terminal A shows a log line for every tool call. `smoke_test.py` uses **no LLM**, so failures here are about the server, the USGS key, or the network, never about Claude.

## 5. Test Claude in the terminal

Still in terminal B (server still running in A):
```bash
python agent.py
you> What is the discharge right now on the Delaware River at Trenton, NJ?
```
You'll see `-> find_sites(...)`, `-> get_latest(...)` and then an answer. Compare the number with
waterdata.usgs.gov for that site. Checking one answer by hand is worth doing before any demo.

## 6. Test the web interface

Terminal B:
```bash
python ui.py                            # prints: Running on local URL: http://127.0.0.1:7860
```

On your **laptop**, open a tunnel in a new terminal and leave it running:
```bash
ssh -i ~/path/to/key.pem -N -L 7860:127.0.0.1:7860 ubuntu@<EC2_PUBLIC_IP>
```
Then browse to **http://127.0.0.1:7860**. The tunnel makes the instance's port 7860 appear on your laptop, encrypted, without opening any firewall port.

Try the example questions. Open the 🔧 panels to see each tool call, and check that the chart on the right fills in after a `get_daily` question.

Stop both (`Ctrl+C` in A and B) before step 7.

## 7. Run both as services (survive logout and reboot)

```bash
cd /opt/usgs-waterdata-mcp
sudo cp deploy/usgs-mcp.service deploy/usgs-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usgs-mcp usgs-ui
systemctl status usgs-mcp usgs-ui --no-pager      # both "active (running)"
python smoke_test.py                               # still 6/6 (with the venv + settings loaded)
```

Day-to-day commands:

| Task | Command |
|---|---|
| Live logs (every tool call) | `journalctl -u usgs-mcp -u usgs-ui -f` |
| Restart after editing code or settings | `sudo systemctl restart usgs-mcp usgs-ui` |
| Stop | `sudo systemctl stop usgs-ui usgs-mcp` |
| Don't start at boot | `sudo systemctl disable usgs-ui usgs-mcp` |

**Updating the code:** copy the new files in, run `pip install -r requirements.txt` (venv active) if requirements changed, run `pytest -q`, then restart the services.

## 8. Showcasing it

**a) You, or a screen share: SSH tunnel (no changes needed).** Same tunnel as step 6. For a meeting or a talk, share your browser window. This is the safest option, and it's usually all a demo needs.

**b) Others use it themselves for a while: public HTTPS with a login.**
You need a domain name pointing at the instance (e.g. a subdomain from your university IT, or any registrar).
1. Add security group rules for **80 and 443** from anywhere (80 is only used to obtain the certificate).
2. Install Caddy, a web server that gets and renews HTTPS certificates automatically:
   ```bash
   sudo apt install -y caddy
   echo 'water-demo.example.edu {
       reverse_proxy 127.0.0.1:7860
   }' | sudo tee /etc/caddy/Caddyfile
   sudo systemctl restart caddy
   ```
3. Visitors go to `https://water-demo.example.edu` and log in with `UI_USERNAME` / `UI_PASSWORD`.

The UI and MCP server still listen only on 127.0.0.1; only Caddy faces the internet.
**Every question costs LLM tokens on your account.** The guardrails help: login required, `UI_MAX_QUESTIONS` per session, and the agent's token budget per question. Still, share the password only with people you trust, and set a spending limit on your Anthropic or AWS account. Stop the UI (`sudo systemctl stop usgs-ui`) when the demo period ends.

---

## Troubleshooting

| Symptom | Look at | Likely cause |
|---|---|---|
| `smoke_test.py` FAIL with `429` | the FAIL output | `API_USGS_PAT` missing or wrong |
| FAIL with a connection or timeout error | `curl -I https://api.waterdata.usgs.gov` | Outbound HTTPS blocked (security group / network ACL egress) |
| `401` from `smoke_test.py` | `/etc/usgs-mcp.env` | Settings not loaded (`set -a; source ...; set +a`) or token mismatch |
| `Address already in use` | `ss -ltnp \| grep 8000` | Another project uses the port (see step 1) |
| Service won't start | `journalctl -u usgs-mcp -n 50` | Wrong path or user in the `.service` file, or the settings file is missing |
| UI: "Something went wrong ... Connect" | `systemctl status usgs-mcp` | MCP server not running |
| UI: authentication or permission error | the error text in the chat | LLM key wrong, or Bedrock model access / IAM `bedrock:InvokeModel` missing |
| Browser can't reach 127.0.0.1:7860 | the tunnel terminal | Tunnel not running, or wrong port |
