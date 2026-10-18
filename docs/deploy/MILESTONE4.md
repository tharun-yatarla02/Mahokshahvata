# Milestone 4: State Out of the VM

Every piece of state now lives outside the VM. The database is **Cloud SQL for
PostgreSQL on a private IP only**, files are in a **private Cloud Storage
bucket** reached as the VM's service account, and passwords and API keys are in
**Secret Manager**. The VM holds two compose files and nothing else.
Slides: [Milestone4_State.pptx](Milestone4_State.pptx) (2 slides, speaker notes included). The app
runs as image `app:v3` at **http://34.122.164.209** (firewalled to the team's IPs).

## Where state lived, before and after

| State | Milestone 3 | Milestone 4 |
|---|---|---|
| Users, sessions, sign-in history, portfolios, holdings, trades, daily values, autopilot runs | Firestore (Firebase project `mahokshahvata-ae5f9`) | **Cloud SQL PostgreSQL 16** `mahokshahvata-db`, private IP `10.27.48.3`, no public IP |
| Politician trades | SQLite file in a Docker volume on the VM | Cloud SQL (`politician_trades` table) |
| Market snapshot, sentiment history | JSON files in a Docker volume on the VM | Bucket `gs://project-262a2026-879b-4b49-88e-app-data` |
| Profile photos (new upload feature) | (didn't exist) | Same bucket, `avatars/<user id>/` |
| Database credential | Firebase Admin **key file** on the VM | `db-password` in Secret Manager; key file **deleted** |
| Anthropic API key | `.env` on laptops; none on the VM | `anthropic-api-key` in Secret Manager; no `.env` on the VM |
| VM identity | Default Compute Engine service account | Dedicated `mahokshahvata-vm` with least-privilege roles |

## Identity and network

```
 Team IPs only (firewall: tcp 80, 22)
        │
        ▼  34.122.164.209 (static, public)
 ┌───────────────── VPC "default", subnet us-central1 10.128.0.0/20 ─────────────────┐
 │  VM mahokshahvata (10.128.0.3), e2-micro                                          │
 │  runs as mahokshahvata-vm@…  (scope cloud-platform; IAM roles decide access)      │
 │    Caddy :80 ──▶ app container                                                    │
 │                   │  private IP, TLS required                                     │
 │                   ▼                                                               │
 │  Private services access range 10.27.48.0/20 (peered with Google)                 │
 │    Cloud SQL mahokshahvata-db  10.27.48.3   ◀── PRIVATE ONLY, no public IP        │
 └───────────────────────────────────────────────────────────────────────────────────┘
        │ Google APIs as mahokshahvata-vm (no key file)
        ├──▶ Secret Manager: db-password, anthropic-api-key
        ├──▶ Cloud Storage:  gs://project-262a2026-879b-4b49-88e-app-data (private)
        └──▶ Artifact Registry: mahokshahvata/app (image pulls)
```

| Service account `mahokshahvata-vm` | Role | Granted on |
|---|---|---|
| Read the database password and API key | `roles/secretmanager.secretAccessor` | the secrets `db-password` and `anthropic-api-key` only |
| Read and write files | `roles/storage.objectUser` | the one bucket only |
| Pull the app image | `roles/artifactregistry.reader` | the `mahokshahvata` repository only |
| Write VM logs and metrics | `roles/logging.logWriter`, `roles/monitoring.metricWriter` | project |

- **Public:** the VM's HTTP port (team IPs only) and the static IP.
- **Private:** Cloud SQL (reachable only from inside the VPC), the bucket (public access prevention enforced), the secrets.
- **Not granted:** no Cloud SQL IAM role (the app connects over the private IP with the database password), no project-wide Editor, and no access to `db-admin-password` (the database admin's password, which the app never needs).

## Evidence for each rubric item

| Rubric item | Evidence |
|---|---|
| Managed database, private IP only (11) | `gcloud sql instances describe mahokshahvata-db` shows `ipv4Enabled: false`, one `PRIVATE` address `10.27.48.3`, `sslMode: ENCRYPTED_ONLY`. Created by [deploy/milestone4.sh](../../deploy/milestone4.sh) with `--no-assign-ip`. |
| Tables from a migration; row counts match (10) | [db/migrations/001_initial.sql](../../db/migrations/001_initial.sql) (`CREATE TABLE` for all 9 tables), applied by [db/migrate.py](../../db/migrate.py) and recorded in `schema_migrations`. Row counts below. |
| Uploads in a bucket via the VM's service account, no key files (10) | Profile photos: `POST /auth/me/photo` → [cloud/blobs.py](../../cloud/blobs.py) uses `storage.Client()` with Application Default Credentials from the metadata server. The Firebase key file was shredded off the VM; no key file exists anywhere. |
| All secrets in Secret Manager; none in code, image or `.env` (8) | [cloud/secrets.py](../../cloud/secrets.py) loads `db-password` and `anthropic-api-key` at startup into the process's memory. [docker-compose.vm.yml](../../docker-compose.vm.yml) has no `env_file`; `.dockerignore` keeps `.env` and key files out of the image. |
| Written analysis (6) | Below. |

### Row counts: Firestore vs Cloud SQL

Printed by [db/firestore_to_postgres.py](../../db/firestore_to_postgres.py) on the VM right after the copy (saved in [row_counts.txt](row_counts.txt)):

| Table | Firestore | Cloud SQL | Match |
|---|---:|---:|:-:|
| users | 6 | 6 | yes |
| sessions | 3 | 3 | yes |
| auth_events | 6 | 6 | yes |
| portfolios | 6 | 6 | yes |
| holdings | 4 | 4 | yes |
| transactions | 6 | 6 | yes |
| snapshots | 8 | 8 | yes |
| autopilot_runs | 0 | 0 | yes |
| **total** | **39** | **39** | **yes** |

The copy kept every id (so existing links and sessions still work) and ran as one
transaction. Politician trades had no rows to move (no Quiver key was configured).

---

## Written analysis: if the VM were deleted right now

**What survives.** Everything a user owns or the app needs to restart:
- **All account and portfolio data** is in Cloud SQL, which is a separate managed
  service with daily backups and deletion protection. Deleting the VM doesn't touch it.
- **Profile photos, the market snapshot and sentiment history** are in the bucket.
- **The database password and API key** are in Secret Manager.
- **The images** (`v1` to `v3`) are in Artifact Registry.
- **The static IP `34.122.164.209`** is a separate reserved address that outlives the
  VM, so the URL and Firebase's authorized domain for Google sign-in stay valid.
- **The firewall rules, the service account and its role bindings, and the budget
  alerts** are project resources, not part of the VM.
- **Users stay signed in:** sessions are rows in Cloud SQL, not memory.

**What breaks.**
- **The website is down until a new VM exists.** Nothing serves traffic in the
  meantime: there is no load balancer, managed instance group or second VM. Recovery
  is manual: `ALLOWED_IPS=… ./deploy/gcp.sh` then `TAG=v3 ./deploy/docker_deploy.sh`,
  about 10 minutes (create the VM, install Docker, pull the image, start). Both scripts
  copy no state and no keys; the new VM is created as `mahokshahvata-vm` and finds
  everything through IAM. Someone with `gcloud` access to the project has to run them.
- **In-memory state is lost and rebuilt automatically:** the login-throttle counters
  (an attacker would get a fresh 5 attempts per email), and the caches for headlines,
  stock explanations and price charts. Headlines get re-classified by Claude, a few
  cents of API use.
- **The autopilot can miss a day.** Its scheduler runs inside the app. If the outage
  covers the 10:00–15:30 New York trading window, that day's run doesn't happen, and if
  it covers the close, that day's closing value isn't recorded, so the next daily-loss
  check compares against an older value.
- **Container logs are lost.** Docker keeps them on the VM's disk; they aren't shipped
  to Cloud Logging (the role is granted, but no logging agent or driver is configured).
  So the record of what happened just before the deletion would be gone. Switching the
  Docker log driver to `gcplogs` would fix this.

**Not caused by the VM, but worth saying:** the VM and Cloud SQL are both in the
single zone `us-central1-a`, so a zone outage would take down the database too.
Cloud SQL's high-availability option would fix that at about twice the cost.

In short: deleting the VM loses no data and no configuration, only uptime, and roughly
ten minutes of someone running two scripts brings it back.

---

## Screenshots to take (Google Cloud console, project `project-262a2026-879b-4b49-88e`)

1. **Cloud SQL, private IP only:** SQL → `mahokshahvata-db` → **Connections** → Networking tab: "Private IP" checked with `10.27.48.3`, "Public IP" unchecked.
   https://console.cloud.google.com/sql/instances/mahokshahvata-db/connections?project=project-262a2026-879b-4b49-88e
2. **IAM bindings for the service account:** IAM & Admin → IAM → filter `mahokshahvata-vm` (project roles). Also show the resource-level grants: Secret Manager → `db-password` → Permissions, the bucket → Permissions, Artifact Registry → `mahokshahvata` → Permissions.
   https://console.cloud.google.com/iam-admin/iam?project=project-262a2026-879b-4b49-88e
3. **Secret Manager list** (names and dates, no values):
   https://console.cloud.google.com/security/secret-manager?project=project-262a2026-879b-4b49-88e
4. **Bucket with an uploaded photo:** Cloud Storage → the bucket → `avatars/` folder after uploading a photo in the app.
   https://console.cloud.google.com/storage/browser/project-262a2026-879b-4b49-88e-app-data?project=project-262a2026-879b-4b49-88e
5. **Row counts:** the table above, or the terminal output in [row_counts.txt](row_counts.txt).

## Video script (10 minutes; every presenter on camera for their part)

| Time | Show | Say |
|---|---|---|
| 0:00 | Slide 1 | What moved where (before/after table). Goal: deleting the VM costs only downtime. |
| 1:30 | Slide 2 | VPC, private Cloud SQL, the service account and its roles, what's public vs private. |
| 3:00 | Cloud SQL Connections page | Private IP `10.27.48.3` only; public IP off; TLS required. |
| 3:45 | The app: sign in, place a trade | The app works end to end against Cloud SQL. |
| 4:30 | VM terminal: run the row query below | The trade just placed is a row in Cloud SQL. |
| 5:15 | [001_initial.sql](../../db/migrations/001_initial.sql) and row counts | Tables come from the migration; 39 of 39 rows match. |
| 6:15 | Profile → upload a photo, then the bucket's `avatars/` folder | The upload lands in the bucket, written as the VM's service account with no key. |
| 7:15 | VM terminal: the `.env` check below | No `.env` and no key file in the container or on the VM. |
| 8:00 | Secret Manager list | Secret names visible, values hidden; the VM can read only two of them. |
| 8:45 | Written analysis | What survives a VM deletion, and what honestly still breaks. |

Commands for the video, run from the repo root:

```bash
# Open a shell on the VM
gcloud compute ssh mahokshahvata --zone us-central1-a --project project-262a2026-879b-4b49-88e

# On the VM: no .env or key file, on the VM or in the container
ls -la /opt/mahokshahvata
sudo docker compose -p mahokshahvata exec app sh -c 'ls -la /app | grep -iE "env|json|key" || echo "no .env or key files"'

# On the VM: the latest trades and row counts, straight from Cloud SQL
sudo docker compose -p mahokshahvata exec app python -c "
from cloud.secrets import load_secrets; load_secrets()
from db.connection import connect
with connect() as c:
    print('connected to', c.info.host, '(Cloud SQL private IP), TLS:', bool(c.pgconn.ssl_in_use))
    for t in ['users', 'portfolios', 'transactions']:
        print(t, c.execute(f'select count(*) n from {t}').fetchone()['n'])
    for r in c.execute('select timestamp, ticker, type, quantity, price from transactions order by id desc limit 3'):
        print(r)"
```

## Re-running or rolling back

- Infrastructure (safe to re-run): `./deploy/milestone4.sh` (add `--switch` to move the VM to the service account).
- Deploy: `TAG=v3 ./deploy/docker_deploy.sh`.
- The one-time data move was `TAG=v3 ./deploy/migrate_data.sh`; it refuses to copy into a database that already has rows.
- Images `v1`/`v2` use Firestore and the Firebase key, which no longer exists on the VM, so a rollback past `v3` would need the Milestone 3 setup. Firestore itself still holds the Milestone 3 data, untouched.
