# Milestone 6: Observability and automated deployment

Live app: **https://34-8-137-93.sslip.io** (GKE, 2–4 replicas, Google-managed certificate).
Deployment branch: **`master`**. Every push to it builds, deploys, health-checks and,
if anything fails, rolls back by itself.

| Piece | File |
|---|---|
| JSON logs (one line per request, plus every app warning) | [`cloud/log.py`](../../cloud/log.py), `request_log` in [`api/main.py`](../../api/main.py), [`cloud/uvicorn_log.json`](../../cloud/uvicorn_log.json) |
| Controlled 5xx for the demo | `GET /debug/fail` in [`api/main.py`](../../api/main.py) |
| 5xx log-based metric, alert policy, email channel, dashboard | [`terraform/monitoring.tf`](../../terraform/monitoring.tf) |
| Cloud infrastructure (SQL, bucket, secrets, IAM, registry, GKE, IP) | [`terraform/infra.tf`](../../terraform/infra.tf), adopted with [`terraform/imports.tf`](../../terraform/imports.tf) |
| CI identity (Workload Identity Federation, no key file) | [`terraform/ci.tf`](../../terraform/ci.tf) |
| Pipeline: test → build → deploy → health check → rollback | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) |

## Architecture

```
 git push master ──► GitHub Actions (ci.yml)
                      test ─► deploy job ──(OIDC token)──► Workload Identity Federation
                                │                            └► github-deployer SA (AR writer, GKE developer)
                                ├─ build + push  app:<commit SHA> ─► Artifact Registry
                                ├─ kubectl set image / rollout status ─► GKE "mahokshahvata"
                                ├─ health check  https://34-8-137-93.sslip.io/health == SHA, /ready 200
                                └─ on failure: kubectl rollout undo, then verify the old SHA serves

 Browser ─► HTTPS load balancer (34.8.137.93) ─► pods "app" (2–4) ─► Cloud SQL (private IP), bucket, Secret Manager
                                                   │ stdout: one JSON line per request
                                                   ▼
                     Cloud Logging ─► log-based metric app_http_5xx (jsonPayload.status>=500)
                                          ├─► dashboard "Mahokshahvata app" (+ CPU, memory, restarts, LB requests)
                                          └─► alert "App is returning HTTP 5xx" ─► email sidsidsid328@gmail.com
```

## Logging

The app prints one JSON object per line to stdout. GKE's logging agent (cluster logging
component `WORKLOADS`) turns each line into a structured Cloud Logging entry: `severity`
becomes the entry's severity, `message` its summary line, and every other key lands in
`jsonPayload`. A request line looks like this:

```json
{"severity": "ERROR", "message": "GET /debug/fail 500", "logger": "app", "version": "<commit SHA>",
 "method": "GET", "path": "/debug/fail", "status": 500, "latency_ms": 0.8}
```

- The `request_log` middleware wraps every request. If a handler raises an exception, it
  still logs `status: 500` before re-raising, so unhandled errors are counted, not only
  returned 5xx responses.
- uvicorn's own messages (startup, tracebacks) use the same formatter (`--log-config
  cloud/uvicorn_log.json`); its plain-text access log is off because `request_log` replaces it.
- Successful `/health` and `/ready` calls are not logged: the probes and the load balancer
  check them every few seconds, and those lines would drown the real traffic. Their failures are logged.
- `version` is the commit the image was built from, so every log line shows which deploy wrote it.

## Metric, dashboard, alert

**Log-based metric `app_http_5xx`** (counter, DELTA INT64) counts log entries matching:

```
resource.type="k8s_container"
resource.labels.cluster_name="mahokshahvata"
resource.labels.container_name="app"
jsonPayload.status>=500
```

Every 5xx the app answers adds exactly 1, since `request_log` writes one line per request.
A log-based metric counts only entries written after it exists.

**Dashboard "Mahokshahvata app"** (Monitoring → Dashboards) shows these panels:
- 5xx per minute (the metric above)
- Load balancer requests by response class (2xx/3xx/4xx/5xx)
- CPU and memory per pod (`kubernetes.io/container/cpu/core_usage_time`, `memory/used_bytes`)
- Container restarts
- The alert's state

**Alert "App is returning HTTP 5xx"** fires when the metric's sum over 1 minute is above 0,
so a single 5xx fires it. It emails the notification channel **sidsidsid328@gmail.com** and
closes the incident, with a "recovered" email, once a full minute passes with no 5xx (Nov 8: opened
06:36:19Z, recovered 53 s later). That threshold suits a class project with
little traffic; with real traffic it would be a rate (for example, 5xx above 1% of requests for 5 minutes).

## Terraform coverage

State is in `gs://project-262a2026-879b-4b49-88e-tfstate` (versioned, private). The repository
has no state, plans or `.terraform/` (`.gitignore`). Secrets are managed as containers only:
their values (secret versions) and the database user's password are never in Terraform, so
they are never in state either.

**Managed by Terraform:**
- Private services access range + VPC peering
- Cloud SQL instance, database, user
- Storage bucket
- The 3 secrets
- The app's service account + its 6 least-privilege bindings
- Artifact Registry repo + cleanup policy
- GKE cluster + node pool (autoscaling 2–3)
- The load balancer's global IP
- The 5xx metric, notification channel, alert policy, dashboard
- The deploy service account, Workload Identity pool/provider and its 3 bindings
- The STS API

**Not managed by Terraform, and why:**

| Resource | Why |
|---|---|
| Kubernetes objects in `k8s/` (Deployment, Service, Ingress, cert, HPA, PDB) | Applied with `kubectl apply -f k8s/`. The pipeline owns the image tag and the HPA owns the replica count. If Terraform managed them, every deploy would show up as drift. |
| The load balancer, NEGs, health check and Google-managed cert *behind* the Ingress | GKE's Ingress controller creates them from `k8s/ingress.yaml` and deletes them with it. |
| Secret **values** and the DB password | Kept out of state on purpose (see above). Added with `gcloud secrets versions add`. |
| Terraform state bucket | It has to exist before `terraform init` (one `gcloud storage buckets create`). |
| Budget alerts ($10/$25/$50) | Created by `deploy/gcp.sh` on the billing account. Only the billing admin (Tharun) can read it, and Terraform runs as Sid. Tharun confirms them in Billing → Budgets & alerts. |
| Firebase project (Google sign-in) | A separate Firebase project, configured in the Firebase console. |
| The retired Milestone 3 VM, its IP and firewall rules | No longer runs the app (stopped at the GKE cutover). Scheduled for deletion, not worth adopting. |
| Project APIs other than STS, the `default` VPC, project IAM for people | Project-level setup that predates the app. |

`deploy/milestone4.sh` and `deploy/gke.sh` created these resources originally. They are
kept as history, but Terraform is now the source of truth. Run future changes through
`terraform apply`, not those scripts.

Run the plan (no secret values on screen):

```bash
export CLOUDSDK_ACTIVE_CONFIG_NAME=mahok
cd terraform && terraform init && terraform plan     # refresh is on by default → "No changes."
```

If someone stops Cloud SQL or resizes the node pool by hand to save money, the plan stops
reporting "No changes" until it is put back. Use `terraform apply` for that instead.

## Pipeline

`.github/workflows/ci.yml`, on every push to `master`:

1. **test**: the whole pytest suite against a PostgreSQL service container.
2. **deploy** (only after test passes; one at a time, in push order):
   1. Authenticate to Google with GitHub's OIDC token through Workload Identity Federation.
      There is no key file anywhere. The provider accepts only `tharun-yatarla02/Mahokshahvata` on `refs/heads/master`.
   2. Build the image with `GIT_SHA=<commit>` and push it as `app:<commit SHA>`.
   3. Record the version currently serving, from `/health`. This is the rollback target.
   4. `kubectl set image` to the new tag, then `kubectl rollout status --timeout=5m`. With
      `maxUnavailable: 0`, the old pods keep serving until the new ones pass readiness
      (`/ready`, which includes the database). A new version that never becomes ready fails here.
   5. Health check through the public HTTPS URL, for up to 2 minutes. `/health` must
      report the new SHA, `/ready` must be 200 and `/` must load.
   6. If step 4 or 5 failed, roll back: `kubectl rollout undo`, wait for that rollout,
      then confirm through the URL that `/health` reports the previous SHA and `/ready` is 200.
      The run still ends red, so the failed commit is visible.

Pull requests run the tests and a Docker build check, and never deploy.

## Evidence (Nov 8, 2026)

| What | Where |
|---|---|
| 3 controlled 500s: JSON log entries, metric definition, metric 0 → 3, alert incident opened 06:36:19Z | [`evidence/m6_controlled_500.txt`](evidence/m6_controlled_500.txt) |
| `terraform plan` against the live project: No changes | [`evidence/m6_terraform_plan.txt`](evidence/m6_terraform_plan.txt) |
| Successful build → deploy → health check (16b8f5f, 2 min 53 s) | [run 42454929244](https://github.com/tharun-yatarla02/Mahokshahvata/actions/runs/42454929244) |
| Broken code stopped by the tests, never deployed (fb391d7) | [run 42455180507](https://github.com/tharun-yatarla02/Mahokshahvata/actions/runs/42455180507) |
| Failed deploy (8faceed, wrong port) → automatic rollback → 16b8f5f verified serving | [run 42455266061](https://github.com/tharun-yatarla02/Mahokshahvata/actions/runs/42455266061) |
| Revert deployed normally (7ec2692) | [run 42455772145](https://github.com/tharun-yatarla02/Mahokshahvata/actions/runs/42455772145) |
| Run summaries, rollback log and `kubectl rollout history` | [`evidence/m6_pipeline_runs.txt`](evidence/m6_pipeline_runs.txt) |
| Dashboard, alert policy, received alert email, log-based metric | screenshots in `evidence/` (list in [`MILESTONE6_VIDEO_SCRIPT.md`](MILESTONE6_VIDEO_SCRIPT.md)) |
| Slides, 1-page written analysis | [`Milestone6_Observability.pptx`](Milestone6_Observability.pptx), [`Milestone6_Written_Analysis.pdf`](Milestone6_Written_Analysis.pdf) |

## Keeping it up through grading

- Don't stop Cloud SQL or scale the node pool down. The site would go down, and `terraform plan`
  would stop reporting "No changes". Change things with `terraform apply`.
- `/debug/fail` is public on purpose (it's the controlled error). Each burst of 500s opens an incident
  and sends a "firing" email, then a "recovered" email about a minute after the last error.
- Cost: about $2.2 a day with 2 nodes, about $3 with the autoscaler's 3rd node, plus Cloud SQL (db-f1-micro), until teardown.
