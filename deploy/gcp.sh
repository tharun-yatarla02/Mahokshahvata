#!/usr/bin/env bash
# The VM and what's around it: budget alerts, firewall, static IP, and the VM
# itself, running as the app's own service account (created by
# deploy/milestone4.sh). Re-running it updates the firewall list. From the repo root:
#
#   ALLOWED_IPS=1.2.3.4/32,5.6.7.8/32 ./deploy/gcp.sh
#
# The VM holds no state: the database is Cloud SQL, files are in the storage
# bucket and keys are in Secret Manager. So rebuilding it after a deletion is:
#   ALLOWED_IPS=... ./deploy/gcp.sh && TAG=<version> ./deploy/docker_deploy.sh
# The static IP is a separate resource and survives a VM deletion, so the
# address (and Firebase's authorized domain) stays the same.
# Nothing is copied to the VM here: no code, no .env, no key files.
set -euo pipefail

PROJECT=${PROJECT:-project-262a2026-879b-4b49-88e}
: "${ALLOWED_IPS:?set ALLOWED_IPS to the team public IPs as CIDRs, e.g. 98.197.228.36/32}"
ZONE=${ZONE:-us-central1-a}
REGION=${ZONE%-*}
VM=${VM:-mahokshahvata}
MACHINE=${MACHINE:-e2-micro}  # free tier (us-central1/us-east1/us-west1); app peaks ~330 MB, swap covers spikes
SA=${SA:-mahokshahvata-vm@$PROJECT.iam.gserviceaccount.com}

g() { gcloud --project "$PROJECT" "$@"; }

g iam service-accounts describe "$SA" >/dev/null 2>&1 || { echo "Missing $SA; run deploy/milestone4.sh first" >&2; exit 1; }

# 1. Budget alerts before anything billable exists. Only on a fresh deploy (no VM yet):
# listing budgets needs billing-account access we may not have, so we can't dedupe.
if ! g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1 && ! g compute addresses describe "$VM-ip" --region "$REGION" >/dev/null 2>&1; then
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

# 3. Static IP (kept when the VM is deleted) + the VM, as the app's service account.
g compute addresses describe "$VM-ip" --region "$REGION" >/dev/null 2>&1 || g compute addresses create "$VM-ip" --region "$REGION"
IP=$(g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)')
if ! g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  g compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 20GB --boot-disk-type pd-standard --tags "$VM" --address "$IP" \
    --service-account "$SA" --scopes cloud-platform   # IAM roles, not scopes, decide what it can reach
  sleep 30  # let sshd come up
fi

echo "VM ready at $IP. Deploy the app with: TAG=<version> ./deploy/docker_deploy.sh"
