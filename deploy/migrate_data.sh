#!/usr/bin/env bash
# Milestone 4, run once: move the data that lived on or behind the VM into the
# new managed services, then remove what's left on the VM. From the repo root:
#
#   TAG=v3 ./deploy/migrate_data.sh
#
#   1. Stops the app, so nobody writes to Firestore during the copy.
#   2. Copies Firestore into Cloud SQL (db/firestore_to_postgres.py, in a
#      throwaway container from the new image) and prints the row counts of
#      both side by side; saved to docs/deploy/row_counts.txt here.
#   3. Copies the market cache and sentiment history from the VM's Docker
#      volume into the storage bucket.
#   4. Only if every count matches: deletes the Firebase key file and the old
#      volume from the VM. Run TAG=v3 ./deploy/docker_deploy.sh afterwards.
set -euo pipefail

PROJECT=${PROJECT:-project-262a2026-879b-4b49-88e}
REGION=${REGION:-us-central1}
ZONE=${ZONE:-us-central1-a}
VM=${VM:-mahokshahvata}
TAG=${TAG:?set TAG, e.g. TAG=v3}
SQL_INSTANCE=${SQL_INSTANCE:-mahokshahvata-db}
BUCKET=${BUCKET:-$PROJECT-app-data}
DIR=/opt/mahokshahvata
IMAGE=$REGION-docker.pkg.dev/$PROJECT/mahokshahvata/app:$TAG
VOLUME=/var/lib/docker/volumes/mahokshahvata_app-data/_data

g() { gcloud --project "$PROJECT" "$@"; }
DB_HOST=$(g sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses.filter("type:PRIVATE").extract(ipAddress).flatten())')

mkdir -p docs/deploy
set +e  # check the copy's result ourselves, so a failure prints how to recover
g compute ssh "$VM" --zone "$ZONE" --command "set -euo pipefail
cd $DIR
sudo docker compose -p mahokshahvata stop app caddy 2>/dev/null || true
sudo gcloud auth configure-docker $REGION-docker.pkg.dev --quiet >/dev/null 2>&1
sudo docker pull -q $IMAGE >/dev/null
# The Firebase key is mounted read-only for Firestore alone; Secret Manager and
# Cloud SQL are reached as the VM's service account.
sudo docker run --rm -v $DIR/firebase-admin.json:/run/firestore-key.json:ro \
  -e FIRESTORE_KEY_FILE=/run/firestore-key.json -e DB_HOST=$DB_HOST -e DB_SSLMODE=require \
  -e SECRETS_PROJECT=$PROJECT $IMAGE python -m db.firestore_to_postgres
" | tee docs/deploy/row_counts.txt
status=${PIPESTATUS[0]}
set -e
if [ "$status" -ne 0 ]; then
  echo "Copy failed or counts differ (exit $status); nothing was deleted. Restart the old app with:" >&2
  echo "  gcloud compute ssh $VM --zone $ZONE --command 'cd $DIR && sudo docker compose -p mahokshahvata start'" >&2
  exit "$status"
fi

g compute ssh "$VM" --zone "$ZONE" --command "set -euo pipefail
for f in market_cache.json sentiment_history.json; do
  sudo test -f $VOLUME/\$f && sudo gcloud storage cp $VOLUME/\$f gs://$BUCKET/\$f --quiet
done
sudo shred -u $DIR/firebase-admin.json
sudo docker compose -p mahokshahvata down 2>/dev/null || true
sudo docker volume rm mahokshahvata_app-data
echo 'Removed the Firebase key file and the old data volume from the VM.'
sudo ls -la $DIR"
echo "Row counts saved to docs/deploy/row_counts.txt. Now deploy: TAG=$TAG ./deploy/docker_deploy.sh"
