# Problem and Fix Log: laptop → GCP VM migration

**Team:** Mahokshahvata · **Project:** `project-262a2026-879b-4b49-88e` · **VM:** `mahokshahvata` (e2-micro, us-central1-a)

## What broke

Right after the first deploy, the deploy script printed `Deployed: https://34.66.1.208.sslip.io`, but the site did not load.
Every request came back as an error from the reverse proxy:

```
$ curl -s -o /dev/null -w "%{http_code}\n" https://34.66.1.208.sslip.io/
502
```

In a browser this shows up as a blank page with **HTTP ERROR 502 (Bad Gateway)**.
On the laptop the same app answered on `localhost:8000` straight away, so this only happened on the VM.

## How we diagnosed it

1. **Is the problem the network/firewall or the app?** A 502 (not a timeout) means the request reached the VM and Caddy, the reverse proxy, answered. So the firewall and HTTPS were fine. Caddy could not reach the app behind it on `127.0.0.1:8000`.
2. **Is the app running?** We SSH'd in and checked the service:
   ```
   $ gcloud compute ssh mahokshahvata --zone us-central1-a --command 'systemctl is-active mahokshahvata caddy'
   active
   active
   ```
   systemd said the process was up, so it had not crashed.
3. **What is the app doing?** We read the service log:
   ```
   $ sudo journalctl -u mahokshahvata --no-pager | tail
   ... systemd[1]: Started mahokshahvata.service.
   ```
   The log showed `Started` but **not** uvicorn's `Application startup complete.` line. The process existed but was not listening yet.
4. **Why so slow?** We checked memory with `free -m`: 953 MB total, about 480 MB available, swap unused. So it was not short on memory. The app was simply still starting. At startup it imports pandas/yfinance, then loads the 3,000-stock market snapshot (`momentum/market_cache.json`) and back-fills sectors before uvicorn starts listening. On the laptop that takes a few seconds. On the e2-micro's shared, burstable vCPU it took **about 60–90 seconds**.
5. **Confirmed** by polling. The first three checks, 15 s apart, returned `502` and the fourth returned `200`:
   ```
   502
   502
   502
   200
   ```
   The journal then showed `Application startup complete.`

## Root cause

This was not a crash and not a networking problem. The deploy script printed "Deployed" as soon as `systemctl restart` returned. On the small VM the app needs about a minute and a half before it accepts connections. During that window Caddy has nothing to forward to, so it returns 502.

## Fix

We changed `deploy/gcp.sh` so it no longer reports success until the app actually answers through the proxy. It polls for up to 3 minutes and fails loudly with a pointer to the logs if the app never comes up:

```bash
# Startup takes ~60-90s on an e2-micro; Caddy returns 502 until uvicorn listens.
for i in $(seq 60); do curl -sf -o /dev/null localhost:80/ && exit 0; sleep 3; done
echo 'App did not answer within 3 minutes; check journalctl -u mahokshahvata' >&2; exit 1
```

With this change, the URL works the moment the script prints `Deployed:`. A real startup failure, such as a missing dependency, now shows up as a failed deploy instead of a silent 502.

**Lesson:** "the service is active" and "the service is ready" are different things. Check readiness with a real HTTP request, not just the process state.
