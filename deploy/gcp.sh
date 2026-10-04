#!/usr/bin/env bash
# Milestone 2: create the VM, firewall, static IP and budget, and (on a VM not yet
# moved to containers) run uvicorn under systemd behind Caddy. Since Milestone 3,
# app deploys go through deploy/docker_deploy.sh; this script stops after the
# infrastructure steps once the VM runs containers.
# A VM (not Cloud Run) because the app keeps SQLite + JSON caches on local disk
# and runs a background market-data thread; both need one long-lived process.
#
#   PROJECT=my-gcp-project ALLOWED_IPS=1.2.3.4/32,5.6.7.8/32 ./deploy/gcp.sh   # first deploy and every redeploy
#
# Order on a fresh project: budget alerts ($10/$25/$50) -> firewall -> IP -> VM.
# Ships the committed code (git archive HEAD), your local .env and the Firebase
# Admin key. Accounts and portfolios live in Firestore; caches on the
# VM are never overwritten by a redeploy.
set -euo pipefail

: "${PROJECT:?set PROJECT to your GCP project id}"
: "${ALLOWED_IPS:?set ALLOWED_IPS to the team public IPs as CIDRs, e.g. 98.197.228.36/32}"
ZONE=${ZONE:-us-central1-a}
REGION=${ZONE%-*}
VM=${VM:-mahokshahvata}
MACHINE=${MACHINE:-e2-micro}  # free tier (us-central1/us-east1/us-west1); app peaks ~330 MB, swap covers spikes
FIREBASE_KEY=${FIREBASE_KEY:-$HOME/.config/mahokshahvata/firebase-admin.json}  # Firebase Admin key: sign-in + Firestore
PYTHON=$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3.14)

g() { gcloud --project "$PROJECT" "$@"; }
ssh_vm() { g compute ssh "$VM" --zone "$ZONE" --command "$1"; }

[ -f "$FIREBASE_KEY" ] || { echo "Missing $FIREBASE_KEY (Firebase console > Project settings > Service accounts > Generate new private key)" >&2; exit 1; }

if grep -qiE '^\s*USE_DEMO_AUTH\s*=\s*(1|true|yes|on)' .env 2>/dev/null; then
  echo "Refusing to deploy: .env has USE_DEMO_AUTH on (shared demo account). Remove it first." >&2
  exit 1
fi

# 1. Budget alerts before anything billable exists. Only on a fresh deploy (no VM yet):
# listing budgets needs billing-account access we may not have, so we can't dedupe.
if ! g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  BILLING=$(g billing projects describe "$PROJECT" --format='value(billingAccountName)' | cut -d/ -f2)
  g services enable billingbudgets.googleapis.com
  g billing budgets create --billing-account "$BILLING" --display-name "$VM-budget" \
    --budget-amount 50USD --filter-projects "projects/$PROJECT" \
    --threshold-rule percent=0.2 --threshold-rule percent=0.5 --threshold-rule percent=1.0  # $10 / $25 / $50
fi

# 2. Firewall: only the team's IPs reach HTTP and SSH. Re-running updates the list.
g services enable compute.googleapis.com
for rule in "$VM-web:tcp:80" "$VM-ssh:tcp:22"; do
  name=${rule%%:*} port=${rule#*:}
  if g compute firewall-rules describe "$name" >/dev/null 2>&1; then
    g compute firewall-rules update "$name" --source-ranges "$ALLOWED_IPS"
  else
    g compute firewall-rules create "$name" --allow "$port" --source-ranges "$ALLOWED_IPS" --target-tags "$VM"
  fi
done

# 3. Static IP + VM.
if ! g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  g compute addresses describe "$VM-ip" --region "$REGION" >/dev/null 2>&1 || g compute addresses create "$VM-ip" --region "$REGION"
  g compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 20GB --boot-disk-type pd-standard --tags "$VM" \
    --address "$(g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)')"
  sleep 30  # let sshd come up
fi
IP=$(g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)')

# Since Milestone 3 the VM runs containers; the systemd install below would fight them for port 80.
if g compute ssh "$VM" --zone "$ZONE" --command 'test -f /opt/mahokshahvata/docker-compose.yml' 2>/dev/null; then
  echo "Infrastructure is up to date. The VM runs containers now: deploy with TAG=<version> ./deploy/docker_deploy.sh"
  exit 0
fi

# 4. Code: committed files only, so .venv, *.db and caches never ship.
git archive --format=tar HEAD | g compute ssh "$VM" --zone "$ZONE" --command 'mkdir -p ~/app && tar -x -C ~/app'
[ -f .env ] && g compute scp .env "$VM:~/app/.env" --zone "$ZONE" && ssh_vm 'chmod 600 ~/app/.env'
g compute scp "$FIREBASE_KEY" "$VM:~/app/firebase-admin.json" --zone "$ZONE" && ssh_vm 'chmod 600 ~/app/firebase-admin.json'
# Seed the market snapshot once so the first boot isn't empty.
if [ -f momentum/market_cache.json ] && ! ssh_vm 'test -f ~/app/momentum/market_cache.json'; then
  g compute scp momentum/market_cache.json "$VM:~/app/momentum/" --zone "$ZONE"
fi

# 5. Install + run. Plain HTTP on the bare IP: Let's Encrypt can't reach a VM
# firewalled to the team, so there's no public cert to get.
# ponytail: HTTP only, the firewall is the protection; add a domain + DNS-01 cert if it goes public.
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
Environment=GOOGLE_APPLICATION_CREDENTIALS=\$HOME/app/firebase-admin.json
ExecStart=\$HOME/app/.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
echo ':80 {
  reverse_proxy 127.0.0.1:8000
}' | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q --now mahokshahvata caddy
sudo systemctl restart mahokshahvata caddy
# Startup takes ~60-90s on an e2-micro; Caddy returns 502 until uvicorn listens.
for i in \$(seq 60); do curl -sf -o /dev/null localhost:80/ && exit 0; sleep 3; done
echo 'App did not answer within 3 minutes; check journalctl -u mahokshahvata' >&2; exit 1"

echo "Deployed: http://$IP  (logs: gcloud compute ssh $VM --zone $ZONE --command 'journalctl -u mahokshahvata -f')"
