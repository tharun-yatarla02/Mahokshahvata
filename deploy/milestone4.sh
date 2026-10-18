#!/usr/bin/env bash
# Milestone 4: move every piece of state off the VM. From the repo root:
#
#   ./deploy/milestone4.sh            # create or update everything below (safe to re-run)
#   ./deploy/milestone4.sh --switch   # also move the VM onto the new service account (stops it ~1 min)
#
# Creates, in the project's default VPC:
#   1. Private services access: a reserved range peered to Google's network, so
#      Cloud SQL gets an IP inside our VPC.
#   2. Cloud SQL for PostgreSQL 16 with a PRIVATE IP ONLY (no public IP), TLS
#      required, daily backups; database "mahokshahvata", user "app".
#   3. A private storage bucket (uniform access, public access prevented).
#   4. Secrets in Secret Manager. db-password is generated here and piped
#      straight in: never printed, never written to a file. anthropic-api-key
#      is created empty; add its value yourself (see the end of this script).
#   5. A dedicated service account for the VM with only what the app uses:
#        secretmanager.secretAccessor  on each secret, not the whole project
#        storage.objectUser            on the bucket only
#        artifactregistry.reader       on the image repository only
#        logging.logWriter, monitoring.metricWriter  (VM logs and metrics)
#      Cloud SQL needs no role: the app connects over the private IP with the
#      database password.
set -euo pipefail

PROJECT=${PROJECT:-project-262a2026-879b-4b49-88e}
REGION=${REGION:-us-central1}
ZONE=${ZONE:-us-central1-a}
VM=${VM:-mahokshahvata}
NETWORK=default
PSA_RANGE=google-managed-services-$NETWORK
SQL_INSTANCE=${SQL_INSTANCE:-mahokshahvata-db}
SQL_TIER=${SQL_TIER:-db-f1-micro}         # shared core, ~$10/month with 10 GB of SSD
DB_NAME=mahokshahvata
DB_USER=app
BUCKET=${BUCKET:-$PROJECT-app-data}
SA_NAME=mahokshahvata-vm
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com
REPO=mahokshahvata
SECRETS=(db-password anthropic-api-key)

g() { gcloud --project "$PROJECT" --quiet "$@"; }
step() { printf '\n== %s\n' "$*"; }

step "APIs"
g services enable sqladmin.googleapis.com servicenetworking.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com iam.googleapis.com compute.googleapis.com

step "Private services access on the $NETWORK network"
g compute addresses describe "$PSA_RANGE" --global >/dev/null 2>&1 || \
  g compute addresses create "$PSA_RANGE" --global --purpose=VPC_PEERING --prefix-length=20 --network="$NETWORK" \
    --description="Range for Google-managed services (Cloud SQL private IP)"
g services vpc-peerings connect --service=servicenetworking.googleapis.com --ranges="$PSA_RANGE" --network="$NETWORK" \
  2>/dev/null || g services vpc-peerings update --service=servicenetworking.googleapis.com --ranges="$PSA_RANGE" \
  --network="$NETWORK" --force

step "Cloud SQL instance $SQL_INSTANCE (private IP only; takes ~10 minutes the first time)"
if ! g sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  g sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE --tier="$SQL_TIER" \
    --region="$REGION" --availability-type=zonal --network="projects/$PROJECT/global/networks/$NETWORK" \
    --no-assign-ip --ssl-mode=ENCRYPTED_ONLY --storage-type=SSD --storage-size=10 --storage-auto-increase \
    --backup-start-time=08:00 --deletion-protection
fi
g sql databases describe "$DB_NAME" --instance="$SQL_INSTANCE" >/dev/null 2>&1 || \
  g sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"

step "Secrets"
for secret in "${SECRETS[@]}"; do
  g secrets describe "$secret" >/dev/null 2>&1 || g secrets create "$secret" --replication-policy=automatic
done
if ! g sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
  # Generated, handed to Cloud SQL and Secret Manager, then dropped. Never echoed or saved.
  password=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)
  g sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$password" >/dev/null
  printf '%s' "$password" | g secrets versions add db-password --data-file=- >/dev/null
  unset password
  echo "Created database user $DB_USER; its password is only in Secret Manager (db-password)."
fi

step "Storage bucket gs://$BUCKET (private)"
g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  g storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access --public-access-prevention

step "Service account $SA and its roles"
g iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  g iam service-accounts create "$SA_NAME" --display-name="Mahokshahvata VM (app runtime)"
sleep 5  # a new service account takes a moment to become usable in IAM policies
for secret in "${SECRETS[@]}"; do
  g secrets add-iam-policy-binding "$secret" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done
g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectUser >/dev/null
g artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" \
  --member="serviceAccount:$SA" --role=roles/artifactregistry.reader >/dev/null
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$role" --condition=None >/dev/null
done

if [ "${1:-}" = "--switch" ]; then
  step "Moving $VM onto $SA (the VM stops for about a minute)"
  current=$(g compute instances describe "$VM" --zone "$ZONE" --format='value(serviceAccounts[0].email)')
  if [ "$current" != "$SA" ]; then
    g compute instances stop "$VM" --zone "$ZONE"
    g compute instances set-service-account "$VM" --zone "$ZONE" --service-account="$SA" --scopes=cloud-platform
    g compute instances start "$VM" --zone "$ZONE"
  fi
fi

step "Summary"
g sql instances describe "$SQL_INSTANCE" --format='table(name,databaseVersion,settings.tier,ipAddresses[].type.list():label=IP_TYPES,ipAddresses[].ipAddress.list():label=IPS,settings.ipConfiguration.ipv4Enabled:label=PUBLIC_IP_ENABLED)'
g secrets list --format='table(name.basename(),createTime.date())'
echo "Bucket: gs://$BUCKET"
echo "VM service account: $(g compute instances describe "$VM" --zone "$ZONE" --format='value(serviceAccounts[0].email)')"
if ! g secrets versions list anthropic-api-key --format='value(name)' --limit=1 | grep -q .; then
  cat <<EOF

anthropic-api-key has no value yet. Add it yourself (the value never appears on screen):
  read -rs KEY && printf '%s' "\$KEY" | gcloud secrets versions add anthropic-api-key --data-file=- --project $PROJECT; unset KEY
EOF
fi
