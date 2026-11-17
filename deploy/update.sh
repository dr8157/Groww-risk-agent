#!/usr/bin/env bash
# Pull latest main, run tests, restart. Run as root (the GitHub Action uses: sudo update.sh).
# Refuses to restart during market hours (Mon-Fri 09:00-23:55 IST) unless FORCE=1, because a restart
# briefly leaves the account unprotected. Deploy after hours.
set -euo pipefail
cd /opt/risk-agent
H=$(TZ=Asia/Kolkata date +%H); D=$(TZ=Asia/Kolkata date +%u)
if [ "${FORCE:-0}" != 1 ] && [ "$D" -le 5 ] && [ "$H" -ge 9 ]; then
  echo "Market hours - refusing to restart. Re-run after midnight IST or with FORCE=1."; exit 3
fi
git fetch origin main && git reset --hard origin/main
venv/bin/pip install -q -r requirements.txt
venv/bin/python -m pytest -q
chown -R riskagent:riskagent /opt/risk-agent
systemctl restart risk-agent
sleep 3
systemctl is-active risk-agent
