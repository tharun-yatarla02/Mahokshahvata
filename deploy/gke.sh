#!/usr/bin/env bash
# Milestone 5: the GKE cluster the app runs on, plus the global IP its load
# balancer uses. Safe to re-run. From the repo root:
#
#   ./deploy/gke.sh                      # cluster + IP, then applies k8s/
#   ./deploy/gke.sh --delete             # tear the cluster and IP down after grading
#
# Zonal Standard cluster (one zonal cluster's management fee is covered by the
# GKE free tier). Nodes run as the app's own service account, mahokshahvata-vm,
# which already has exactly the roles the app needs (deploy/milestone4.sh), so
# pods reach Secret Manager, the bucket and the registry with no key files and
# no new IAM bindings. VPC-native on the default network, so pods reach Cloud
# SQL's private IP over the same peering the VM used.
set -euo pipefail

PROJECT=${PROJECT:-project-262a2026-879b-4b49-88e}
ZONE=${ZONE:-us-central1-a}
CLUSTER=${CLUSTER:-mahokshahvata}
MACHINE=${MACHINE:-e2-medium}  # 4 GB; on e2-small GKE's own system pods already request ~1 of 1.3 GB
SA=mahokshahvata-vm@$PROJECT.iam.gserviceaccount.com
IP_NAME=mahokshahvata-gke-ip   # global: a GKE Ingress needs a global address

g() { gcloud --project "$PROJECT" --quiet "$@"; }

if [ "${1:-}" = "--delete" ]; then
  g container clusters delete "$CLUSTER" --zone "$ZONE"
  g compute addresses delete "$IP_NAME" --global
  exit 0
fi

g services enable container.googleapis.com
g compute addresses describe "$IP_NAME" --global >/dev/null 2>&1 || g compute addresses create "$IP_NAME" --global
IP=$(g compute addresses describe "$IP_NAME" --global --format='value(address)')

if ! g container clusters describe "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1; then
  g container clusters create "$CLUSTER" --zone "$ZONE" --release-channel regular \
    --network default --enable-ip-alias \
    --machine-type "$MACHINE" --disk-type pd-standard --disk-size 30 \
    --num-nodes 2 --enable-autoscaling --min-nodes 2 --max-nodes 3 \
    --service-account "$SA" --scopes cloud-platform \
    --addons HttpLoadBalancing,HorizontalPodAutoscaling
fi
g container clusters get-credentials "$CLUSTER" --zone "$ZONE"

kubectl apply -f k8s/
echo "Load balancer IP $IP -> https://${IP//./-}.sslip.io (the certificate takes 15-60 min to become Active)"
