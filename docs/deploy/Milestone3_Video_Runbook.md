# Milestone 3 video runbook

A step-by-step script for the 10-minute demo, presented by Tharun. Each
segment says **what to say**, **what to type or click**, and **what to do if
it goes wrong**. Every command here was tested on Oct 4, 2026.

**Rules from the assignment:** your face on camera the whole time (no
audio-only narration). Upload to OneDrive, share with sdesini@, krajan@,
skolanu@cougarnet.uh.edu, and submit the OneDrive link in Canvas.

| # | Segment | Time |
|---|---|---|
| 1 | Intro + architecture (slide 1) | 1:00 |
| 2 | Run everything locally with Docker Compose | 2:30 |
| 3 | The image in Artifact Registry | 1:30 |
| 4 | Pull and run the same image on the VM | 2:30 |
| 5 | Budget and current spending | 1:00 |
| 6 | What got easier (slide 2) + wrap-up | 1:30 |
| | | **10:00** |

---

## Before recording (15 minutes ahead)

1. **Check your IP is allowed** (the VM firewall only lets in listed IPs):
   open https://api.ipify.org. It must be one of `98.40.141.98`,
   `98.197.228.36` or `98.198.85.161`. If not, add it first:
   ```bash
   gcloud compute firewall-rules update mahokshahvata-web --source-ranges 98.40.141.98/32,<new IP>/32
   gcloud compute firewall-rules update mahokshahvata-ssh --source-ranges 98.40.141.98/32,<new IP>/32
   ```
2. **Docker Desktop is running** (whale icon in the menu bar).
3. **Live site works:** http://34.122.164.209 shows the styled sign-in page.
   If it looks like plain HTML, it's the IP (step 1); then press Cmd+Shift+R.
4. **You have a live-site account.** The demo account is off on the VM on
   purpose (a shared account on a public server). At http://34.122.164.209
   click **Create an account** once, with your email and an 8+ character
   password, and check you can sign in. It's stored in the real Firestore, so it
   stays for the video. Always type `http://` (browsers that switch to
   `https://` get no answer).
   If it says your email "already exists and signs in with Google", you used
   Google on the Milestone 2 site: click **Continue with Google** instead, or
   register with a different email.
5. **SSH works:** `gcloud compute ssh mahokshahvata --zone us-central1-a`,
   then `exit`. Use the terminal, not the Console's SSH button: the firewall
   only allows your IP on port 22, so the browser SSH can't connect.
6. **Open these browser tabs, in order:**
   1. The slides: `docs/deploy/Milestone3_Containers.pptx` (in PowerPoint)
   2. http://localhost
   3. Artifact Registry: Cloud Console → Artifact Registry → `mahokshahvata` → `app`
   4. http://34.122.164.209
   5. Billing → Budgets & alerts
   6. Billing → Reports
7. **Open two terminal tabs**, both in the repo folder. Make the font large
   (Cmd + plus, about 18 pt) so commands are readable on video.
8. **Stop local containers** so segment 2 starts from nothing:
   `docker compose down`
9. Turn on Do Not Disturb; close Slack, email and anything showing personal data.

> **Do not push `v1` again during the video.** Pushing a new build under the
> same tag changes its digest, and the laptop/VM comparison stops matching.

---

## 1. Intro + architecture (1:00)

**Show:** slide 1.

**Say:**
> "I'm Tharun, and this is Milestone 3 for Mahokshahvata, a paper-trading app.
> I packaged the app as a Docker image. It runs as two containers: Caddy on
> port 80, which forwards to the FastAPI app on port 8000, over a private
> compose network. Market data lives in a Docker volume, so it survives
> restarts. On the laptop, a Firestore emulator stands in for the real
> database; on the VM, the app uses real Firestore, with its key handed in at
> run time. I build the image once for both Intel and ARM machines, push it to
> Artifact Registry as v1, and both the laptop and the VM pull that same image."

---

## 2. Run everything locally (2:30)

**Do (terminal 1):**
```bash
cat Dockerfile            # scroll briefly: slim base, two stages, non-root user, health check
cat .dockerignore         # point at: .env, firebase-admin.json, .venv, tests
docker compose up --build -d
docker compose ps         # wait until app and firestore say "healthy" (about 20 s)
```

**Say while it builds:**
> "The Dockerfile uses slim Python 3.12 and two stages, so build leftovers
> don't ship. It runs as a non-root user and has a health check. The
> .dockerignore keeps secrets, my local virtualenv, tests and docs out of the
> image. No secrets are baked in; keys are passed in when the container starts."

**Show (browser tab 2, http://localhost):**
1. Click **Continue with demo account**.
2. **Market** page: 3,000 stocks with momentum.
3. Buy **$100 of AAPL** and show the confirmation.
4. **Sentiment** page: live headlines.
5. On the Market page, open a stock and click **Explain**.

**Say:**
> "The whole app is running from containers on my laptop: trading, live news,
> and AI explanations."

**If it goes wrong:**
- `port is already allocated` on 80 → `HTTP_PORT=8080 docker compose up -d`, then use http://localhost:8080.
- The app says "warming up" with few stocks → the market data is still loading; show other pages and come back.

---

## 3. The image in Artifact Registry (1:30)

**Show (browser tab 3):** Artifact Registry → `mahokshahvata` → `app`.
1. Point at the row tagged **`v1, latest`**.
2. Click it and point at the digest **`sha256:656872e4…`**.
3. Point at the two images inside: **linux/amd64** (the VM) and **linux/arm64** (my Mac).

**Do (terminal 1), optional backup:**
```bash
gcloud artifacts docker images list us-central1-docker.pkg.dev/project-262a2026-879b-4b49-88e/mahokshahvata --include-tags --filter="tags:v1"
```

**Say:**
> "This is the image in Artifact Registry with an explicit version tag, v1,
> not only latest. The digest is its fingerprint: if a single byte changed,
> it would change. It's about 133 megabytes compressed. A cleanup policy keeps
> the five newest versions plus v1, so storage stays in the free tier."

---

## 4. Pull and run the same image on the VM (2:30)

**Do (terminal 2):**
```bash
gcloud compute ssh mahokshahvata --zone us-central1-a
cd /opt/mahokshahvata
ls                                  # docker-compose.yml, docker-compose.vm.yml, firebase-admin.json
sudo TAG=v1 docker compose -f docker-compose.yml -f docker-compose.vm.yml pull
sudo TAG=v1 docker compose -f docker-compose.yml -f docker-compose.vm.yml up -d
sudo docker compose -p mahokshahvata ps
sudo docker image inspect us-central1-docker.pkg.dev/project-262a2026-879b-4b49-88e/mahokshahvata/app:v1 --format '{{index .RepoDigests 0}}'
```

**Say:**
> "On the VM, I only installed Docker: no Python, no pip, no virtualenv. I
> pull v1 from the registry and start it. Here's the digest on the VM:
> 656872e4, the same as in the registry."

**Show (browser tab 4):** http://34.122.164.209, the same address as Milestone 2.
Sign in with the account you created (the demo button is disabled here) and
show the portfolio and Market pages.

> "On the live server the demo account is disabled for security, so I sign in
> with a real account stored in Firestore."

**Then prove the laptop runs the same image (terminal 1):**
```bash
docker pull us-central1-docker.pkg.dev/project-262a2026-879b-4b49-88e/mahokshahvata/app:v1
docker image inspect us-central1-docker.pkg.dev/project-262a2026-879b-4b49-88e/mahokshahvata/app:v1 --format '{{index .RepoDigests 0}}'
```
> "And on the laptop, the same tag gives the same digest. It's literally the
> same artifact on both machines."

**Optional, more convincing (adds about 2 minutes and briefly takes the site down):**
delete the VM's copy before pulling, so the video shows a real download:
```bash
sudo docker compose -p mahokshahvata down
sudo docker rmi us-central1-docker.pkg.dev/project-262a2026-879b-4b49-88e/mahokshahvata/app:v1
# then the pull and up commands above; the app takes 60-90 s to report healthy on the e2-micro
```

**If it goes wrong:**
- SSH hangs or is refused → your IP isn't allowed (see "Before recording").
- `unauthorized` on pull → `sudo gcloud auth configure-docker us-central1-docker.pkg.dev --quiet`, then pull again.
- `ps` shows `health: starting` → wait 60–90 s; the small VM is slow to import pandas.

---

## 5. Budget and current spending (1:00)

**Show (browser tab 5):** Billing → Budgets & alerts → **mahokshahvata cap** → Edit.
1. Scope: **this project only**, credits **excluded**.
2. Amount: **$50 per month**.
3. Alerts: **10%, 20%, 25%, 50%, 100%** ($5, $10, $12.50, $25, $50), sent by email.

**Show (browser tab 6):** Billing → Reports, filtered to this project, current month.

**Say:**
> "The budget guardrails are still active: a $50 monthly budget on this
> project with alerts at 10, 25 and 50 percent, plus 20 and 100. Free credits
> are excluded, so alerts track real usage. Here's this month's actual spend.
> Costs stay low because the VM is a free-tier e2-micro, the registry is in the
> same region so pulls are free, and the cleanup policy keeps image storage small."

---

## 6. What got easier + wrap-up (1:30)

**Show:** slide 2.

**Say:**
> "In Milestone 2, every deploy meant SSH-ing in, installing Caddy and uv,
> building a virtualenv and pip-installing on a tiny VM, writing a systemd
> unit, and waiting through 60 to 90 seconds of 502 errors. Now the VM only
> needs Docker: pull the tag, start it. Caddy waits for the app's health check,
> so there's no 502 window, and rolling back is just running the previous tag.
> The specific problem it solved: an outdated library on one laptop silently
> broke every Claude call. Now every machine runs the identical image."

**Say, to close:**
> "To recap: the app runs fully in containers locally, v1 is in Artifact
> Registry, the same image runs on the VM at the Milestone 2 address, there are
> no secrets in the image, and the budget alerts are active. Thanks!"

---

## Screenshots to take (during a rehearsal)

- [ ] Artifact Registry: `app` with `v1` and its digest
- [ ] Terminal: the VM pull, `up -d`, `ps` and digest output (segment 4)
- [ ] Budget "mahokshahvata cap" with its alert thresholds
- [ ] Billing → Reports showing current spend

## After recording

- [ ] Upload the video to OneDrive; share with sdesini@, krajan@, skolanu@cougarnet.uh.edu
- [ ] Canvas: the OneDrive link, the repo link (https://github.com/tharun-yatarla02/Mahokshahvata), the slides, the screenshots, and the written analysis from [MILESTONE3.md](MILESTONE3.md)
- [ ] `docker compose down` on the laptop when done
