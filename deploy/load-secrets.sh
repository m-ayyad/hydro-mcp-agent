#!/usr/bin/env bash
# load-secrets.sh: copy secrets from AWS SSM Parameter Store into /etc/usgs-mcp.env.
#
# Why SSM? So secrets are never typed into code, committed to GitHub, or baked
# into a Docker image. They live encrypted in AWS; only this instance's IAM role
# can read them.
#
# One-time setup (run from your laptop with the AWS CLI configured):
#   aws ssm put-parameter --name /usgs-mcp/usgs_api_key --type SecureString --value "<your USGS key>"
#   aws ssm put-parameter --name /usgs-mcp/mcp_token    --type SecureString --value "$(openssl rand -hex 32)"
# The EC2 instance role needs: ssm:GetParameter on those names (and kms:Decrypt).
#
# Then on the instance:  ./deploy/load-secrets.sh && sudo systemctl restart usgs-mcp

set -euo pipefail                      # stop on any error or unset variable
REGION=${AWS_REGION:-us-east-1}

get() {
  aws ssm get-parameter --region "$REGION" --with-decryption \
      --name "$1" --query Parameter.Value --output text
}

sudo tee /etc/usgs-mcp.env >/dev/null <<ENV
API_USGS_PAT=$(get /usgs-mcp/usgs_api_key)
MCP_TOKEN=$(get /usgs-mcp/mcp_token)
ENV
sudo chown ubuntu:ubuntu /etc/usgs-mcp.env   # your user can load it for manual tests
sudo chmod 600 /etc/usgs-mcp.env             # nobody else can read it
echo "Wrote /etc/usgs-mcp.env"
