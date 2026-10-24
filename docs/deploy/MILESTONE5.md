# Milestone 5: Orchestration on GKE

The app moved off the single VM and onto a GKE cluster. It now runs as 2–4 replicas behind a Google
HTTPS load balancer at **https://34-8-137-93.sslip.io**, with a Google-managed certificate,
and http:// redirects to https://. Kubernetes replaces a pod that dies, and the autoscaler
adds replicas under load. As in Milestone 4, all state is outside the pods: Cloud SQL,
the storage bucket and Secret Manager.

| Piece | File |
|---|---|
| Cluster + global static IP | [`deploy/gke.sh`](../../deploy/gke.sh) |
| Deployment (2+ replicas, 3 probes, preStop drain) | [`k8s/deployment.yaml`](../../k8s/deployment.yaml) |
| Service (container-native load balancing) | [`k8s/service.yaml`](../../k8s/service.yaml) |
| Load balancer health check + draining | [`k8s/backendconfig.yaml`](../../k8s/backendconfig.yaml) |
| Ingress + ManagedCertificate + http→https redirect | [`k8s/ingress.yaml`](../../k8s/ingress.yaml) |
| HorizontalPodAutoscaler (2–4 on CPU) | [`k8s/hpa.yaml`](../../k8s/hpa.yaml) |
| PodDisruptionBudget (minAvailable 1) | [`k8s/pdb.yaml`](../../k8s/pdb.yaml) |

## Topology

```
Browser ── https://34-8-137-93.sslip.io  (sslip.io resolves to 34.8.137.93)
   │
   ▼
Global external HTTPS load balancer  (static IP mahokshahvata-gke-ip, Google-managed cert,
   │                                  port 80 answers 301 → https)
   ▼   GKE Ingress "app"  ──  health check GET /ready :8000, 30 s connection draining
   ▼
Service "app"  (ClusterIP + NEG: the load balancer sends straight to pod IPs)
   │
   ├── pod app-…  ┐  Deployment "app", image app:v6, 2–4 replicas (HPA, CPU 60%)
   ├── pod app-…  ├  spread across nodes; PDB keeps ≥1 up during drains
   └── …          ┘  GKE cluster "mahokshahvata", us-central1-a, 2–3 e2-medium nodes
          │            (nodes run as service account mahokshahvata-vm: no key files)
          ├── Cloud SQL PostgreSQL 16 "mahokshahvata-db", private IP 10.27.48.3, TLS
          ├── Cloud Storage gs://project-262a2026-879b-4b49-88e-app-data
          └── Secret Manager (db-password, anthropic-api-key)
```

The cluster is VPC-native on the `default` network, so pods reach Cloud SQL's private IP over
the same private services peering the VM used. The nodes run as `mahokshahvata-vm`, the
service account from Milestone 4 that already has exactly the needed roles: secret accessor
on two secrets, object user on the bucket, and reader on the image repository. That meant
no new IAM bindings were needed, and no key files exist.

## Health probes: why there are two

| Probe | Path | Checks | On failure |
|---|---|---|---|
| startup | `/health` | the process has finished starting (60–90 s: pandas import, market snapshot) | keeps the other two probes off for up to 5 min |
| **liveness** | `/health` | the process answers HTTP. **No database** | the container is restarted |
| **readiness** | `/ready` | the process answers **and** Cloud SQL runs `SELECT 1`, on its own connection with a 2 s timeout | the pod leaves the Service and the load balancer (the LB health check uses `/ready` too); nothing restarts |

They are different because they guard against different failures. A stuck process is fixed by a
restart, so liveness checks only that. A database outage is not fixed by restarting anything.
If liveness checked the database, one Cloud SQL blip (for example, its weekly maintenance)
would fail every pod's liveness probe at once. Kubernetes would then restart all of them
together, and each would take 60–90 s to come back. Readiness just stops sending traffic
to pods that can't serve and lets them back in when the database answers.
`/ready` opens its own short-lived connection, so it never waits behind the engine's lock,
which a slow request may be holding.

## Making the app safe to run as several replicas

- **Autopilot scheduler:** it ran in every process, so two replicas could both place the
  day's paper trades. It now runs only in the replica holding a Postgres advisory lock
  (`_only_one_replica` in `api/main.py`); the others skip that turn. A dead pod's connection
  drops, which frees the lock for another replica (`tests/test_replicas.py`).
- **Migrations** already take an advisory lock (`db/migrate.py`), so pods starting together
  don't run them twice.
- **Sessions** are in Cloud SQL, so any replica can serve any signed-in user.
- **Per-pod, accepted as-is:** the sign-in rate limiter (in memory: 5 failures per email per
  pod, so up to 5×replicas in total) and the 5-minute sentiment cache. Each replica also runs
  its own market-data thread.
- **The VM** no longer runs the app: its containers were stopped at cutover, so it can't run
  a second, unlocked scheduler against the same database. `sudo docker compose -p mahokshahvata start` on the VM brings them back.

## Evidence

| What | Where |
|---|---|
| `kubectl get pods` before and after deleting a pod | [`evidence/kubectl_pods_delete_test.txt`](evidence/kubectl_pods_delete_test.txt) |
| Autoscaler scaling 2 → 3 → 4 under load (HPA events) | [`evidence/hpa_scaling.txt`](evidence/hpa_scaling.txt) |
| No errors while a pod is deleted and a node is drained (668 of 668 requests 200) | [`evidence/zero_downtime_test.txt`](evidence/zero_downtime_test.txt) |
| Certificate Active, http→https redirect | [`evidence/certificate_and_redirect.txt`](evidence/certificate_and_redirect.txt) (+ your console screenshot) |
| Old Firebase key deleted, VM scanned (Milestone 4 feedback) | [`evidence/firebase_key_revocation.txt`](evidence/firebase_key_revocation.txt) |

**Pod deletion (Oct 24):** `app-…-474bk` was deleted at 19:20:42Z. The ReplicaSet created
`app-…-67wtb` within 1 s, and it was Running and Ready 19 s later. In a second run with
requests going through the load balancer about 3 times a second, every request returned
200 during the deletion and during a node drain (`evidence/zero_downtime_test.txt`).

**Autoscaling (Oct 24):** 16 request loops from a pod inside the cluster pushed CPU to 225–280%
of the target. The HPA went 2 → 3 at 19:21:38Z and 3 → 4 at 19:21:53Z. The cluster
autoscaler added a third node for the fourth pod, and after the load stopped the
replica count went back to 2.

## Written analysis: the 5xx that a missing PodDisruptionBudget causes

The failure shows up as a few seconds to about two minutes of 502s, typically during a GKE node
upgrade or when the cluster autoscaler removes a node. Nothing in the app is broken.
Draining a node evicts every pod on it. Without a PodDisruptionBudget, Kubernetes evicts
them all at once. If both replicas share that node (the scheduler allows it, as we saw after
our own pod deletion), or two nodes drain back to back, every replica is terminating at the
same moment. The load balancer still lists them for the few seconds it takes to notice. Their
replacements then need 60–90 s each to start and pass `/ready`. Until then the
load balancer has no healthy backend and returns 502 (`failed_to_pick_backend` in its logs). The
failure is intermittent because it happens only when a drain lines up with where the pods
landed. The rest of the time two replicas look like plenty.

We did not get these errors. On Oct 24 we drained a node holding one replica (`kubectl drain`,
which uses the same eviction API as a GKE upgrade) while sending requests through the
load balancer, and all 668 requests returned 200. The reason is that `k8s/pdb.yaml` sets `minAvailable: 1`. The eviction API
refuses to evict the last ready replica, so a drain waits until the replacement on another
node passes readiness. Three other settings close the remaining gaps:
- `maxUnavailable: 0` keeps a rollout from dropping below 2 serving pods.
- The topology spread places replicas on different nodes.
- The `preStop` sleep (15 s) and the load balancer's 30 s connection draining let a pod
  finish in-flight requests after the load balancer stops sending it new ones.

A PDB only applies to evictions. A plain `kubectl delete pod`, as in our demo, bypasses it,
which is why the second replica's readiness, not the PDB, kept the site up during that test.

## Video demo (10 min, cameras on)

Run from the repo root. In every terminal you record, first run
`export CLOUDSDK_ACTIVE_CONFIG_NAME=mahok` (the gcloud config for sidsidsid328@gmail.com on
this project; without it kubectl uses the default Qwiklabs account and fails). Tharun, with
his own account: `gcloud container clusters get-credentials mahokshahvata --zone us-central1-a --project project-262a2026-879b-4b49-88e`.

**Before recording, Tharun must add `34-8-137-93.sslip.io`** to Firebase console →
project `mahokshahvata` → Authentication → Settings → Authorized domains. Until then,
"Continue with Google" fails on the new URL. Email/password sign-in works either way.
The site is public now: the VM was firewalled to the team's IPs, but the load balancer is not.

1. **Replicas healthy:** `kubectl get deploy,pods -o wide`. 2/2 READY, on different nodes.
2. **Probes:** `kubectl describe deploy app | grep -E "Liveness|Readiness|Startup"`, then
   `curl -s https://34-8-137-93.sslip.io/health` and `.../ready`.
3. **Self-healing (live):** in one terminal `kubectl get pods -w`; in another
   `kubectl delete pod <one of them>`. The old pod shows Terminating, a new one appears in about 1 s,
   and it reaches 1/1 Ready in about 20 s. Reload the site during this: it keeps working.
4. **Autoscaler:** terminal A `kubectl get hpa app -w`; terminal B:
   ```bash
   kubectl run load --image=busybox:1.36 --restart=Never -- /bin/sh -c \
     'for i in $(seq 16); do (while true; do wget -q -O /dev/null http://app/momentum?top=50; done) & done; wait'
   ```
   REPLICAS goes 2 → 3 → 4 within a minute. Then `kubectl describe hpa app` shows the
   SuccessfulRescale events. Stop it with `kubectl delete pod load`; about 3 minutes later it scales back to 2.
5. **HTTPS:** open https://34-8-137-93.sslip.io, click the padlock, and show the certificate
   (issued by Google Trust Services for 34-8-137-93.sslip.io). Then type
   `http://34-8-137-93.sslip.io` and watch the address bar become https, or show
   `curl -sI http://34-8-137-93.sslip.io` → `301` with `Location: https://…`.
6. **Certificate Active:** Console → Network services → Load balancing → Certificates (or
   `kubectl describe managedcertificate app-cert` → `Certificate Status: Active`).
7. **PDB:** `kubectl get pdb app` → `MIN AVAILABLE 1, ALLOWED DISRUPTIONS 1`.

## Cost and teardown

About $2.2 a day: 2 e2-medium nodes (sometimes 3) plus the load balancer's forwarding rule.
The zonal cluster's management fee is covered by the GKE free tier. This is on top of Cloud SQL.
After grading, run `./deploy/gke.sh --delete`, which removes the cluster and the global IP.
Bring the VM back with `sudo docker compose -p mahokshahvata start` on it.
