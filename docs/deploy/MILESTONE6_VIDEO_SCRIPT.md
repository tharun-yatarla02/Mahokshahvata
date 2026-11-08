# Milestone 6 video script (~15 min, cameras on)

The rollback demo takes about 6 minutes: about 1 min to build, then the 5-minute rollout timeout.
So **push the broken commit in the first minute**, show the monitoring parts while it runs, and come
back to it. The fix you push afterwards is the "push a commit, watch it build, deploy, health-check" demo.

## Before you hit record

- In every terminal: `export CLOUDSDK_ACTIVE_CONFIG_NAME=mahok`, then `cd` to the repo root. Tharun uses his
  own account: `gcloud container clusters get-credentials mahokshahvata --zone us-central1-a --project project-262a2026-879b-4b49-88e`.
- `git pull` so you're on the latest master, and `curl -s https://34-8-137-93.sslip.io/health` to note the live SHA.
- Browser tabs, in this order:
  1. The site, signed in with the test account (not "Continue with Google").
  2. GitHub → Actions.
  3. Logs Explorer with the query `resource.type="k8s_container" resource.labels.container_name="app" jsonPayload.status>=500`.
  4. Log-based metrics → `app_http_5xx`.
  5. Monitoring → Dashboards → "Mahokshahvata app".
  6. Alerting → policy "App is returning HTTP 5xx".
  7. Gmail (sidsidsid328), with the Nov 8 alert email open.
- Never show secret values. Don't run `gcloud secrets versions access`, and don't open `.env`.
- Have the broken change ready to paste (terminal B):
  ```bash
  sed -i '' 's/"--port", "8000", "--no-access-log"/"--port", "8001", "--no-access-log"/' Dockerfile
  git commit -am "Demo: broken release (wrong port) to show automatic rollback" && git push origin master
  ```
  (On Linux, `sed -i` without the `''`.)

---

### 0:00–1:00 · Intro + start the rollback demo
> "We're team Mahokshahvata. Milestone 6 is about seeing what the app does and shipping it automatically. First, I'm going to push a deliberately broken release. Its image listens on the wrong port, so its pods can never pass their health checks. We'll come back in a few minutes to see the pipeline roll it back."

Paste the broken change in terminal B. Show the new run starting in GitHub Actions.

### 1:00–2:00 · The app works on its live HTTPS URL
Open https://34-8-137-93.sslip.io, click the padlock, sign in with the test account and click around. Then:
```bash
curl -s https://34-8-137-93.sslip.io/health      # {"status":"ok","version":"<SHA of what's live>"}
```
> "/health reports the commit the running image was built from. The pipeline uses this to prove which version is serving."

### 2:00–3:30 · Slide 1 (architecture) + slide 2 (logs)
> "Three paths. Deploy: a push to master runs GitHub Actions, which signs in to Google with no key file, through Workload Identity Federation, and rolls out an image tagged with the commit SHA. Serve: the same GKE setup as Milestone 5. Observe: every request writes one JSON line, the 5xx metric counts the errors, and the dashboard and the alert both read that metric."

### 3:30–5:30 · Controlled 5xx → JSON log → metric
```bash
for i in 1 2 3; do curl -s -o /dev/null -w "%{http_code}\n" https://34-8-137-93.sslip.io/debug/fail; done
```
Three `500`s. In Logs Explorer, click **Run query**, expand one entry and point out `severity: ERROR`,
`jsonPayload.status: 500`, `path`, `latency_ms` and `version`.
> "Every line is JSON. GKE turns severity into the entry's severity and puts the rest in jsonPayload."

Open the metric `app_http_5xx`: show the filter (`jsonPayload.status>=500`) and that it's a counter.
> "One line per request, so each 5xx adds exactly one."

Open it in Metrics Explorer, or wait for the dashboard tile. The new points show up 1–3 minutes after the errors, so move on and come back.

### 5:30–7:00 · Dashboard + alert (slide 3)
Dashboard "Mahokshahvata app": the 5xx tile (Nov 8's spike of 3, and today's once it arrives), LB requests by
response class, CPU and memory per pod, and restarts.
Alerting → the policy: condition "sum over 1 min > 0", notification channel email.
Gmail: the alert email received on Nov 8 at about 06:36 UTC (incident opened 4 min after the 500s).
> "One error fires it because we have little traffic. With real traffic we'd alert on a rate, like over 1% of requests for 5 minutes."

### 7:00–8:30 · Terraform (slide 4), live plan
```bash
cd terraform && terraform init -input=false >/dev/null && terraform plan
```
> "Refresh is on, which is the default. It reads every resource from Google and compares: No changes."

While it runs, go through slide 4. Terraform manages 31 resources: SQL, network, GKE, bucket, secrets, IAM, registry, monitoring and CI identity. Then the exceptions and why: the k8s objects, the Ingress-owned load balancer, secret values, the state bucket, budgets, Firebase, the old VM.
Mention that the state is in a GCS bucket, and that no state or secrets are in git (`.gitignore`).

### 8:30–10:30 · The rollback finished (slide 5)
Open the run started at 0:00:
- **Build and push**: green.
- **Deploy**: red, `timed out waiting for the condition`.
  > "maxUnavailable is 0, so the old pods kept serving the whole time. The broken pods never got traffic."
- **Roll back to the previous version**: expand it. You'll see `rolled back`, then
  `serving version=<old SHA> /ready=200`, then `Rolled back: … is serving`.

Then verify it yourself:
```bash
curl -s https://34-8-137-93.sslip.io/health        # the OLD SHA, not the broken one
kubectl rollout history deployment/app | tail -4
```

### 10:30–13:30 · Push a fix: build, deploy, health check
```bash
cd .. && git revert --no-edit HEAD && git push origin master
```
Watch the run: test, then build and push, then Deploy, then **Health check through the load balancer**,
whose log shows `attempt N: version=<new SHA> /ready=200`. It takes about 2.5 minutes.
Walk through slide 5 while it runs. Then:
```bash
curl -s https://34-8-137-93.sslip.io/health        # the new SHA
```

### 13:30–15:00 · Wrap-up
> "To recap: JSON logs, a log-based metric counting 5xx, a dashboard, and an alert we actually received by email. All the infrastructure is in Terraform, with a clean plan. And a pipeline that deploys every push to master, checks it over HTTPS and rolls back on its own."

---

## Screenshots to take for the submission

Save these in `docs/deploy/evidence/`:
1. `m6_dashboard.png`: the dashboard with the 5xx spike and the CPU and memory tiles.
2. `m6_alert_policy.png`: Alerting → the policy page (condition and notification channel).
3. `m6_alert_email.png`: the received alert email in Gmail.
4. `m6_log_metric.png`: Logging → Log-based metrics → `app_http_5xx` (its filter).
5. `m6_log_entry.png`: one expanded 500 entry in Logs Explorer.
6. `m6_pipeline_success.png` and `m6_pipeline_rollback.png`: the two Actions runs.
