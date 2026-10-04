# Milestone 3: Containers

The app now ships as one image, `app:v1`, built for both `linux/amd64` (the VM)
and `linux/arm64` (Apple Silicon laptops) and pushed to Artifact Registry. A
laptop runs it with `docker compose up --build`; the VM pulls the same `v1` and
runs it at the Milestone 2 address, **http://34.122.164.209**.

| | |
|---|---|
| Registry | `us-central1-docker.pkg.dev/project-262a2026-879b-4b49-88e/mahokshahvata/app` |
| Tags | `v1`, `latest` (same image) |
| Index digest (both platforms) | `sha256:656872e42bfe792278830241f79798129356dfc9c92e7feb60826d2e87e656d3` |
| amd64 / arm64 digests | `sha256:9b65f302…` / `sha256:d308916c…` |
| Slides | [Milestone3_Containers.pptx](Milestone3_Containers.pptx) |

Files: [Dockerfile](../../Dockerfile), [.dockerignore](../../.dockerignore),
[docker-compose.yml](../../docker-compose.yml) (shared),
[docker-compose.override.yml](../../docker-compose.override.yml) (laptop: local
build + Firestore emulator), [docker-compose.vm.yml](../../docker-compose.vm.yml)
(VM: Firebase key as a run-time secret), [docker_deploy.sh](../../deploy/docker_deploy.sh),
[registry-cleanup-policy.json](../../deploy/registry-cleanup-policy.json).

---

## Written analysis

**Image size.** The final image is **601 MB on disk (arm64), 578 MB on the VM
(amd64)**, and **133 MB compressed** to pull. Most of it is pandas, numpy and the
Google/Anthropic client libraries the app needs; our own code is 1.2 MB. To
reduce it we:
- used `python:3.12-slim` instead of the full image;
- built in two stages, so pip's caches and build files stay in a stage that
  is thrown away;
- removed `pip` and the test suites pandas and numpy ship inside themselves
  (686 → 601 MB);
- moved test tools (pytest, httpx) to `requirements-dev.txt`, out of the image.

**What `.dockerignore` excludes, and why.**
- *Secrets* (`.env`, `firebase-admin.json`, `*.key`, `*.pem`): anything copied
  into an image can be read by anyone who can pull it, and lives on in the
  registry and its layers. They are handed in at run time instead.
- *Runtime data* (`market_cache.json`, `sentiment_history.json`, `*.db`): these
  belong in the `app-data` volume; a copy baked into the image would ship stale
  prices and be overwritten anyway.
- *Local environments* (`.venv/` is 319 MB of macOS packages, `__pycache__`):
  the image installs its own Linux packages; macOS binaries would not run.
- *Not needed to run* (`.git/`, `tests/`, `docs/`, `deploy/`, `.claude/`,
  `.github/`, slides and PDFs): they would only add size and leak project
  history. Together this keeps the build context to the ~3 MB the app uses.

**No secrets in the image, and how we verified it.**
1. Trivy secret scan of the built image: **0 secrets found**.
2. Exported the image's full filesystem (16,929 files) and searched it: no
   `.env`, no Firebase key file, no `"private_key"` or `PRIVATE KEY` under
   `/app`, and the first 24 characters of our Anthropic key appear in **0**
   files.
3. `docker history` shows no secret in any build step (the only `KEY` is the
   public GPG ID that signs official Python downloads).

At run time the Anthropic key comes from `.env` through `env_file`, and the
Firebase key is mounted at `/run/secrets/firebase-admin`, owned by the app's
user (uid 10001) with mode `0400`.

**Cost control.**
- The $50/month budget is scoped to this project, excludes credits (so alerts
  track real spend even on free-trial credits), and alerts at 10%, 20%, 25%,
  50% and 100% ($5 / $10 / $12.50 / $25 / $50). A second $100 budget alerts at
  50/80/100%.
- The VM is still the free-tier e2-micro, and the Artifact Registry repository
  is in the same region, so pulls cause no network charge.
- A registry cleanup policy keeps the 5 newest versions and `v1`, deletes
  untagged images after 7 days and old ones after 30. Storage stays near the
  0.5 GB free allowance.
- Docker logs are capped at 3 × 10 MB per container, so they can't fill the
  20 GB disk.

Security scan note: Trivy also reports 45 high or critical issues in the Debian
base packages (none in our Python packages); only 1 has a fix released.
Rebuilding on a newer `python:3.12-slim` picks fixes up as Debian ships them.

---

## What is easier than in Milestone 2

| Milestone 2 (manual VM) | Milestone 3 (containers) |
|---|---|
| Install Caddy and uv, build a venv and `pip install` **on the VM** | The VM needs only Docker; nothing is installed or built there |
| Hand-written systemd unit and Caddyfile | Two compose files, the same on every machine |
| 60–90 s of 502 errors after each deploy | Caddy starts only once the app's health check passes |
| Rollback = redo the setup with older code | Rollback = `TAG=<previous> ./deploy/docker_deploy.sh` |
| Each laptop had its own Python setup (an old Brotli library silently broke Claude on one) | Every machine runs the same image digest |

On the VM, the switch-over took about 2 minutes from finished pull to healthy
app. Memory use went from 830 MB (+749 MB swap) under systemd to about 570 MB
(+66 MB swap) in containers, measured just after the switch.

---

## Demo script (10 minutes, cameras on)

1. **Local, ~3 min.** In the repo: `docker compose up --build`. Show the three
   containers (`docker compose ps`), open http://localhost, sign in with the
   demo account, show the Market page, make a $100 trade, open Sentiment and
   Explain on a stock.
2. **Registry, ~2 min.** Cloud Console → Artifact Registry → `mahokshahvata` →
   `app`. Show the `v1` tag and its digest `sha256:656872e4…`, then click it
   to show the amd64 and arm64 images inside.
3. **VM, ~3 min.**
   ```bash
   gcloud compute ssh mahokshahvata --zone us-central1-a
   cd /opt/mahokshahvata
   sudo TAG=v1 docker compose -f docker-compose.yml -f docker-compose.vm.yml pull
   sudo TAG=v1 docker compose -f docker-compose.yml -f docker-compose.vm.yml up -d
   sudo docker compose -p mahokshahvata ps
   sudo docker image ls --digests | grep mahokshahvata
   ```
   Point at the digest: it matches the registry. Open http://34.122.164.209.
4. **Budget, ~1 min.** Billing → Budgets & alerts → "mahokshahvata cap": $50,
   alerts at 10/20/25/50/100%, scoped to this project. Then Billing → Reports
   for current spend.
5. **What got easier, ~1 min.** Show `TAG=v1 ./deploy/docker_deploy.sh` and the
   table above: no setup on the VM, no 502 window, one-command rollback.

## Submission checklist

- [ ] Video uploaded to OneDrive and shared with sdesini@, krajan@, skolanu@cougarnet.uh.edu; link submitted in Canvas
- [ ] Slides: [Milestone3_Containers.pptx](Milestone3_Containers.pptx)
- [ ] Repository link (Dockerfile, docker-compose.yml, .dockerignore are on `master`)
- [ ] Screenshot: Artifact Registry showing `v1` and its digest
- [ ] Terminal output of the VM pull and run (step 3 above)
- [ ] Written analysis (section above)
- [ ] Screenshot: budget alerts and current spend
