#!/usr/bin/env bash
# Deploy to a Google Compute Engine VM: uvicorn under systemd, Caddy for HTTPS.
# A VM (not Cloud Run) because the app keeps SQLite + JSON caches on local disk
# and runs a background market-data thread; both need one long-lived process.
#
#   PROJECT=my-gcp-project ./deploy/gcp.sh            # first deploy and every redeploy
#   PROJECT=... DOMAIN=app.example.com ./deploy/gcp.sh  # own domain (point its A record at the IP first)
#
# Ships the committed code (git archive HEAD) plus your local .env. Data on the
# VM (accounts, portfolios, caches) is never overwritten by a redeploy.
set -euo pipefail

: "${PROJECT:?set PROJECT to your GCP project id}"
ZONE=${ZONE:-us-central1-a}
REGION=${ZONE%-*}
VM=${VM:-mahokshahvata}
MACHINE=${MACHINE:-e2-micro}  # free tier (us-central1/us-east1/us-west1); app peaks ~330 MB, swap covers spikes
PYTHON=$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3.14)

g() { gcloud --project "$PROJECT" "$@"; }
ssh_vm() { g compute ssh "$VM" --zone "$ZONE" --command "$1"; }

if grep -qiE '^\s*USE_DEMO_AUTH\s*=\s*(1|true|yes|on)' .env 2>/dev/null; then
  echo "Refusing to deploy: .env has USE_DEMO_AUTH on (shared demo account). Remove it first." >&2
  exit 1
fi

if ! g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  g services enable compute.googleapis.com
  g compute addresses create "$VM-ip" --region "$REGION"
  g compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 20GB --boot-disk-type pd-standard --tags http-server,https-server \
    --address "$(g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)')"
  g compute firewall-rules describe allow-http-https >/dev/null 2>&1 ||
    g compute firewall-rules create allow-http-https --allow tcp:80,tcp:443 --target-tags http-server,https-server
  sleep 30  # let sshd come up
fi

IP=$(g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)')
DOMAIN=${DOMAIN:-$IP.sslip.io}

# Code: committed files only, so .venv, *.db and caches never ship.
git archive --format=tar HEAD | g compute ssh "$VM" --zone "$ZONE" --command 'mkdir -p ~/app && tar -x -C ~/app'
[ -f .env ] && g compute scp .env "$VM:~/app/.env" --zone "$ZONE" && ssh_vm 'chmod 600 ~/app/.env'
# Seed the market snapshot once so the first boot isn't empty.
if [ -f momentum/market_cache.json ] && ! ssh_vm 'test -f ~/app/momentum/market_cache.json'; then
  g compute scp momentum/market_cache.json "$VM:~/app/momentum/" --zone "$ZONE"
fi

ssh_vm "set -e
test -f /swapfile || { sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null; }
command -v caddy >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq caddy; }
command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
cd ~/app && ~/.local/bin/uv venv -q --allow-existing --python $PYTHON .venv && ~/.local/bin/uv pip install -q -p .venv -r requirements.txt

# One worker, no --reload: extra workers would each start the market-data thread and race on SQLite.
sudo tee /etc/systemd/system/mahokshahvata.service >/dev/null <<UNIT
[Unit]
After=network-online.target
[Service]
User=\$USER
WorkingDirectory=\$HOME/app
ExecStart=\$HOME/app/.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
echo '$DOMAIN {
  reverse_proxy 127.0.0.1:8000
}' | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q --now mahokshahvata caddy
sudo systemctl restart mahokshahvata caddy"

echo "Deployed: https://$DOMAIN   (logs: gcloud compute ssh $VM --zone $ZONE --command 'journalctl -u mahokshahvata -f')"
