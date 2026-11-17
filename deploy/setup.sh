#!/usr/bin/env bash
# One-time server setup (Ubuntu/Debian VPS, run as root):  sudo bash deploy/setup.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
REPO_URL="${REPO_URL:-https://github.com/dr8157/groww-risk-agent.git}"
BRANCH="${BRANCH:-main}"

apt-get update -qq && apt-get install -y -qq python3 python3-venv git
id riskagent >/dev/null 2>&1 || useradd --system --home /opt/risk-agent --shell /usr/sbin/nologin riskagent
mkdir -p /var/lib/risk-agent /etc/risk-agent
[ -d /opt/risk-agent/.git ] || git clone --branch "$BRANCH" "$REPO_URL" /opt/risk-agent
python3 -m venv /opt/risk-agent/venv
/opt/risk-agent/venv/bin/pip install -q -r /opt/risk-agent/requirements.txt
chown -R riskagent:riskagent /opt/risk-agent /var/lib/risk-agent

if [ ! -f /etc/risk-agent/secrets.env ]; then
  cp /opt/risk-agent/.env.example /etc/risk-agent/secrets.env
  echo ">> Fill in /etc/risk-agent/secrets.env (nano), then re-run this script."
fi
chown root:riskagent /etc/risk-agent/secrets.env; chmod 640 /etc/risk-agent/secrets.env

install -m 644 /opt/risk-agent/deploy/risk-agent.service /etc/systemd/system/risk-agent.service
# SSH user for the GitHub Action: may run ONLY update.sh as root. Add its public key to ~deploy/.ssh/authorized_keys
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
echo "deploy ALL=(root) NOPASSWD: /opt/risk-agent/deploy/update.sh" > /etc/sudoers.d/risk-agent
chmod 440 /etc/sudoers.d/risk-agent
git config --system --add safe.directory /opt/risk-agent
systemctl daemon-reload
systemctl enable risk-agent
echo
echo "Next: fill secrets, then run the READ-ONLY check before starting:"
echo "  sudo -u riskagent bash -c 'set -a; . /etc/risk-agent/secrets.env; set +a; cd /opt/risk-agent && RISK_AGENT_DATA=/var/lib/risk-agent venv/bin/python -m risk_agent.main --check'"
echo "If it looks right:  systemctl start risk-agent && journalctl -u risk-agent -f"
