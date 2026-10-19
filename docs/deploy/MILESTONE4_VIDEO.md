# Milestone 4 video: step-by-step script

A 10-minute walkthrough that shows every rubric item live. Cameras on: every
presenter's face must be visible while they speak. Facts and evidence are in
[MILESTONE4.md](MILESTONE4.md).

## Before recording

Browser tabs (Chrome, signed in as the project owner, project
`project-262a2026-879b-4b49-88e` selected):

| Tab | Link |
|---|---|
| Slides | `docs/deploy/Milestone4_State.pptx`, ready to present |
| The app | http://34.122.164.209 (signed in) |
| Cloud SQL overview | https://console.cloud.google.com/sql/instances/mahokshahvata-db/overview?project=project-262a2026-879b-4b49-88e |
| Cloud SQL networking | https://console.cloud.google.com/sql/instances/mahokshahvata-db/connections?project=project-262a2026-879b-4b49-88e (Networking tab) |
| Bucket | https://console.cloud.google.com/storage/browser/project-262a2026-879b-4b49-88e-app-data?project=project-262a2026-879b-4b49-88e |
| Secret Manager | https://console.cloud.google.com/security/secret-manager?project=project-262a2026-879b-4b49-88e |
| Secret permissions | https://console.cloud.google.com/security/secret-manager/secret/db-password/permissions?project=project-262a2026-879b-4b49-88e |
| IAM | https://console.cloud.google.com/iam-admin/iam?project=project-262a2026-879b-4b49-88e (filter `mahokshahvata-vm`) |
| VM details | https://console.cloud.google.com/compute/instancesDetail/zones/us-central1-a/instances/mahokshahvata?project=project-262a2026-879b-4b49-88e |

Also: open `db/migrations/001_initial.sql` and `docs/deploy/row_counts.txt` in the
editor, put a small JPEG or PNG (under 2 MB) on the desktop, and connect a terminal
to the VM ahead of time, with the font enlarged:

```bash
gcloud compute ssh mahokshahvata --zone us-central1-a --project project-262a2026-879b-4b49-88e
```

## Script

### 0:00 Introduction (slide 1, on camera)
"This is Milestone 4 for Mahokshahvata, our AI-assisted paper-trading platform. The
goal was to pull every piece of state out of our VM, so deleting it costs nothing
but downtime: the database moved to Cloud SQL on a private IP, uploads to a Cloud
Storage bucket reached through the VM's own identity, and secrets to Secret Manager."

### 0:45 Slide 1: before and after
"In Milestone 3 the VM held a Docker volume (market cache, sentiment history, a
SQLite file) and a Firebase Admin key file the app used to reach Firestore. Now the
VM holds two compose files: data is in Cloud SQL with a private IP only, files in a
private bucket, secrets in Secret Manager, and the image in Artifact Registry. We
copied all 39 rows with every table matching, our tables come from 9 CREATE TABLE
statements, and the app and its container have no key files or .env."

Say "the app and its container", not "anywhere on the VM" (see "Known gap" below).

### 2:00 Slide 2: identity and network
"Only our team's IPs reach the VM, on ports 80 and 22, through a static IP. The VM is
in the default VPC, subnet 10.128.0.0/20, and runs as a dedicated service account,
mahokshahvata-vm, with no key file. Cloud SQL is in a private services access range,
10.27.48.0/20, peered into our VPC; its only address is 10.27.48.3, with TLS
required. The service account can read two secrets, use one bucket, pull from one
repository, and write logs and metrics. It has no Editor role, no Cloud SQL role,
and no access to the database admin password. Google sign-in uses our own Firebase
project, checked with Google's public keys, so no key is involved."

### 3:15 Database with a private IP only (11 pts)
Show the Cloud SQL overview ("Connect to this instance" lists only the private IP
10.27.48.3), then the Networking tab (Private IP checked, Public IP unchecked).
Optional: the Security tab shows SSL-only connections.

"There's no way to reach this database from the internet; even my laptop can't, so
we ran the data migration from inside the VM."

### 4:15 The app working end to end against Cloud SQL
In the app: Portfolio → buy $50 of AAPL (on a weekend the order fills at the last
close). Then in the VM terminal:

```bash
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

"It connects to 10.27.48.3 over TLS, and the newest transaction is the trade I just
placed."

### 5:45 Migration file and row counts (10 pts)
Scroll through `db/migrations/001_initial.sql`, then:

```bash
sudo docker compose -p mahokshahvata exec app python -m db.migrate --status
```

It prints `applied: 001_initial.sql`, `pending: none`. Then open `row_counts.txt`.

"Nine tables with keys and checks, applied at startup and recorded in the database.
All 8 data tables match between Firestore and PostgreSQL, 39 rows in total; the copy
ran as one transaction and refused to write into a non-empty database." (Today's live
counts for sign-in history and daily values are higher because normal use adds rows.)

### 6:45 File upload into the bucket (10 pts)
In the app: avatar → Profile → Upload photo. In the bucket tab, refresh and open
`avatars/<your user number>/`: the new file is there with the current time. Show the
VM details page (service account `mahokshahvata-vm`). Optional proof in the terminal:

```bash
curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email; echo
```

"The server checks it's a real image under 2 MB and writes it as the VM's service
account, which gets credentials from the metadata server: no key file. That account
has Storage Object User on this bucket only, and the bucket blocks public access."

### 7:45 No .env in the container; secrets in Secret Manager (8 pts)

```bash
ls -la /opt/mahokshahvata
sudo docker compose -p mahokshahvata exec app sh -c 'ls -la /app | grep -iE "env|json|key" || echo "no .env or key files"'
sudo docker compose -p mahokshahvata logs app | grep "secrets from Secret Manager"
```

Then the Secret Manager list (three names, values hidden; never click "View secret
value") and db-password's permissions (only `mahokshahvata-vm`).

"No .env on the VM or in the container; the app loads the database password and the
Anthropic key from Secret Manager at startup and keeps them in memory. The passwords
were generated by our setup script and piped straight in, so nobody has seen them."

### 8:45 Written analysis (on camera)
"If the VM were deleted, we'd lose uptime, not data or configuration. Data, files,
secrets, images, the static IP, the firewall and IAM all survive, and users stay
signed in. What breaks: the site is down until someone runs gcp.sh and
docker_deploy.sh, about ten minutes; in-memory caches and login-throttle counters
reset; the autopilot can miss a day; container logs are lost because they live on
the VM's disk. Separately, both the VM and the database are single-zone, though
backups keep the data safe."

### 9:45 Close
"Database on Cloud SQL with a private IP only, tables from a committed migration
with every row accounted for, uploads in a bucket through the VM's identity, and
secrets in Secret Manager. Thanks for watching."

## After recording

Upload the video to OneDrive and share it with sdesini@cougarnet.uh.edu,
krajan@cougarnet.uh.edu and skolanu@cougarnet.uh.edu. Submit only the OneDrive
link in Canvas, with the slides and the written-analysis PDF.

## Known gap

An old Milestone 2 folder in another member's home directory on the VM still holds
a copy of the unused Firebase key file. Nothing reads it, but avoid claiming there
are no key files anywhere on the VM until it is removed.
