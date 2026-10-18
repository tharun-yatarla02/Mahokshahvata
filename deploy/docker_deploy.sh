#!/usr/bin/env bash
# Run a pushed image version on the VM. From the repo root:
#
#   TAG=v3 ./deploy/docker_deploy.sh            # roll back: TAG=<older> ./deploy/docker_deploy.sh
#
# The VM needs only Docker: it pulls $TAG from Artifact Registry and runs
# docker-compose.yml + docker-compose.vm.yml from /opt/mahokshahvata. Nothing is
# built or installed on the VM, and nothing secret is copied to it: the app
# reads its keys from Secret Manager and its data from Cloud SQL and Cloud
# Storage as the VM's service account (deploy/milestone4.sh sets those up).
# Note: images before v3 used Firestore and a Firebase key file; rolling back
# that far needs the Milestone 3 setup.
set -euo pipefail

PROJECT=${PROJECT:-project-262a2026-879b-4b49-88e}
REGION=${REGION:-us-central1}
ZONE=${ZONE:-us-central1-a}
VM=${VM:-mahokshahvata}
TAG=${TAG:?set TAG, e.g. TAG=v3}
SQL_INSTANCE=${SQL_INSTANCE:-mahokshahvata-db}
BUCKET=${BUCKET:-$PROJECT-app-data}
REGISTRY=$REGION-docker.pkg.dev
DIR=/opt/mahokshahvata

g() { gcloud --project "$PROJECT" "$@"; }

DB_HOST=$(g sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses.filter("type:PRIVATE").extract(ipAddress).flatten())')
[ -n "$DB_HOST" ] || { echo "No private IP for Cloud SQL instance $SQL_INSTANCE; run deploy/milestone4.sh first" >&2; exit 1; }

g compute scp docker-compose.yml docker-compose.vm.yml "$VM:/tmp/" --zone "$ZONE"
g compute ssh "$VM" --zone "$ZONE" --command "set -euo pipefail
command -v docker >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq docker.io docker-compose-v2; }
sudo mkdir -p $DIR && sudo mv /tmp/docker-compose.yml /tmp/docker-compose.vm.yml $DIR/

# Pull as the VM's own service account (repository reader role): gcloud's credential
# helper fetches a fresh token on every pull, so a manual \`sudo docker pull\` works too.
sudo docker logout $REGISTRY >/dev/null 2>&1 || true
sudo gcloud auth configure-docker $REGISTRY --quiet >/dev/null 2>&1
cd $DIR
compose() {
  sudo TAG=$TAG DB_HOST=$DB_HOST DATA_BUCKET=$BUCKET SECRETS_PROJECT=$PROJECT \
    docker compose -f docker-compose.yml -f docker-compose.vm.yml \"\$@\"
}
compose pull
compose up -d --remove-orphans
# Startup takes 60-90 s on the e2-micro; Caddy starts once the app's health check passes.
for i in \$(seq 90); do curl -sf -o /dev/null localhost:80/health && break; sleep 3; done
compose ps
curl -sf -o /dev/null localhost:80/ || { echo 'App did not answer within 4.5 minutes; check: sudo docker compose -p mahokshahvata logs app' >&2; exit 1; }
sudo docker image prune -f >/dev/null   # drop superseded images; the disk is 20 GB"

IP=$(g compute instances describe "$VM" --zone "$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
echo "Deployed $TAG: http://$IP"
