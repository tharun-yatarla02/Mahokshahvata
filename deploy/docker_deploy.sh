#!/usr/bin/env bash
# Run a pushed image version on the VM (Milestone 3). From the repo root:
#
#   TAG=v1 ./deploy/docker_deploy.sh
#
# The VM needs only Docker: it pulls $TAG from Artifact Registry and runs
# docker-compose.yml + docker-compose.vm.yml from /opt/mahokshahvata. Nothing
# is built or pip-installed on the VM. Same firewall, IP and URL as Milestone 2
# (deploy/gcp.sh created those).
#
# First run only, migrating from the Milestone 2 systemd deploy: installs
# Docker, copies the Firebase key and the market cache from the old app folder,
# and stops (but keeps) the old mahokshahvata + caddy services. Roll back with:
#   sudo docker compose -p mahokshahvata down && sudo systemctl start mahokshahvata caddy
set -euo pipefail

PROJECT=${PROJECT:-project-262a2026-879b-4b49-88e}
ZONE=${ZONE:-us-central1-a}
VM=${VM:-mahokshahvata}
TAG=${TAG:-v1}
REGISTRY=us-central1-docker.pkg.dev
DIR=/opt/mahokshahvata
FIREBASE_KEY=${FIREBASE_KEY:-$HOME/.config/mahokshahvata/firebase-admin.json}

g() { gcloud --project "$PROJECT" "$@"; }

g compute scp docker-compose.yml docker-compose.vm.yml "$VM:/tmp/" --zone "$ZONE"
# A local Firebase key replaces the VM's copy; without one the VM keeps its own.
[ -f "$FIREBASE_KEY" ] && g compute scp "$FIREBASE_KEY" "$VM:/tmp/firebase-admin.json" --zone "$ZONE"

g compute ssh "$VM" --zone "$ZONE" --command "set -euo pipefail
command -v docker >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq docker.io docker-compose-v2; }
sudo mkdir -p $DIR && sudo mv /tmp/docker-compose.yml /tmp/docker-compose.vm.yml $DIR/
[ -f /tmp/firebase-admin.json ] && sudo mv /tmp/firebase-admin.json $DIR/
if ! sudo test -f $DIR/firebase-admin.json; then   # migrate the Milestone 2 key
  OLD=\$(sudo sh -c 'ls /home/*/app/firebase-admin.json 2>/dev/null | head -1')
  [ -n \"\$OLD\" ] || { echo 'No Firebase key on the VM; set FIREBASE_KEY to your local copy' >&2; exit 1; }
  sudo cp \"\$OLD\" $DIR/firebase-admin.json
fi
sudo chown 10001:10001 $DIR/firebase-admin.json && sudo chmod 400 $DIR/firebase-admin.json   # readable only by the container's app user

# Pull as the VM's own service account (repository reader role): gcloud's credential
# helper fetches a fresh token on every pull, so a manual \`sudo docker pull\` works too.
sudo docker logout $REGISTRY >/dev/null 2>&1 || true
sudo gcloud auth configure-docker $REGISTRY --quiet >/dev/null 2>&1
cd $DIR
compose() { sudo TAG=$TAG docker compose -f docker-compose.yml -f docker-compose.vm.yml \"\$@\"; }
compose pull

# Milestone 2 -> 3: seed the data volume with the old market cache, then stop the old services.
if ! sudo docker volume inspect mahokshahvata_app-data >/dev/null 2>&1; then
  compose create
  OLD=\$(sudo sh -c 'ls /home/*/app/momentum/market_cache.json 2>/dev/null | head -1')
  [ -n \"\$OLD\" ] && sudo docker run --rm --user 0 --entrypoint sh -v \"\$OLD\":/src.json:ro -v mahokshahvata_app-data:/data \
    $REGISTRY/$PROJECT/mahokshahvata/app:$TAG -c 'cp /src.json /data/market_cache.json && chown 10001 /data/market_cache.json'
fi
for svc in mahokshahvata caddy; do
  systemctl is-enabled -q \$svc 2>/dev/null && sudo systemctl disable -q --now \$svc || true
done

compose up -d --remove-orphans
# Startup takes 60-90 s on the e2-micro; Caddy starts once the app's health check passes.
for i in \$(seq 90); do curl -sf -o /dev/null localhost:80/health && break; sleep 3; done
compose ps
sudo docker image ls --digests $REGISTRY/$PROJECT/mahokshahvata/app
curl -sf -o /dev/null localhost:80/ || { echo 'App did not answer within 4.5 minutes; check: sudo docker compose -p mahokshahvata logs app' >&2; exit 1; }
sudo docker image prune -f >/dev/null   # drop superseded images; the disk is 20 GB"

IP=$(g compute instances describe "$VM" --zone "$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo "Deployed $TAG: http://$IP"
