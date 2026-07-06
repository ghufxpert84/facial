# Deploying via Portainer (Git-repository stack)

This deploys the stack entirely through the Portainer UI, building from a
private Git repo. One caveat: **bootstrapping Portainer itself typically
needs one SSH command**, because Synology's Container Manager GUI generally
can't bind-mount `/var/run/docker.sock` (it only browses shared folders).
Everything after that — the actual app stack, worker enrollment, tuning — is
100% Portainer UI, no SSH.

## 1. Push this project to a private repo

1. On GitHub: **New repository** → private → do NOT initialize with a
   README/`.gitignore` (this repo already has its own).
2. Locally (already done: `git init` + initial commit exist in this folder):
   ```bash
   git remote add origin https://github.com/<you>/telegram-worker-tracker.git
   git push -u origin main
   ```
3. Create a **fine-grained personal access token** Portainer will use to
   clone the private repo: GitHub → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → generate, scoped to just
   this repo, permission **Contents: Read-only**. Save the token somewhere
   safe — you'll paste it into Portainer once.

`.env` and `data/` are gitignored — no secrets or photos go into the repo.
Runtime config is supplied via Portainer's stack environment variables
instead.

## 2. Install Portainer on the NAS (one-time SSH step)

1. DSM → Control Panel → Terminal & SNMP → enable SSH service.
2. From your Mac: `ssh <your-dsm-user>@<nas-ip>`
3. Create a persistent data folder via File Station first (or let docker
   create it): `/volume1/docker/portainer_data`
4. Run:
   ```bash
   sudo docker run -d \
     -p 9443:9443 \
     --name portainer \
     --restart=always \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v /volume1/docker/portainer_data:/data \
     portainer/portainer-ce:latest
   ```
5. You can disable the SSH service again afterward if you'd rather not
   leave it on.

## 3. First login

Visit `https://<nas-ip>:9443`, accept the self-signed cert warning, create
the admin account, then select the **local** Docker environment.

## 4. Create the stack

Portainer → Environments → local → **Stacks** → **Add stack**

- Name: `telegram-worker-tracker`
- Build method: **Repository**
- Repository URL: `https://github.com/<you>/telegram-worker-tracker.git`
- Repository reference: `refs/heads/main`
- Authentication: ON → username = your GitHub username, password = the PAT
  from step 1.3
- Compose path: `docker-compose.yml`
- **Environment variables** (add each as a key/value pair — this is
  Portainer's equivalent of `.env`):
  - `TG_API_ID`, `TG_API_HASH`, `TG_CHANNELS`
  - `TG_SESSION_STRING` — leave blank for now (step 5)
  - `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
  - `DATABASE_URL` — must match the Postgres values above,
    e.g. `postgresql://tracker:change_me@db:5432/worker_tracker`
  - `POLL_INTERVAL_SECONDS` (60), `MATCH_THRESHOLD` (0.45),
    `RETENTION_DAYS` (90)
  - `DASHBOARD_USER` (e.g. `admin`)
  - `DASHBOARD_PASSWORD_HASH` — leave blank for now (step 6)
  - `DATA_DIR` — **set this to an absolute path**, e.g.
    `/volume1/docker/telegram-worker-tracker-data` (create it via File
    Station first). Don't leave this relative — Portainer clones the repo
    into its own internal storage, so a relative path would bury your
    Postgres data and worker photos somewhere hard to find and easy to lose
    on redeploy.
- Click **Deploy the stack**.

`telegram-listener` will crash-loop until `TG_SESSION_STRING` is set — that's
expected, it exits on purpose when the session string is missing. The other
three services should come up fine.

## 5. Generate TG_SESSION_STRING (no SSH)

`login.py` needs an interactive prompt (phone number, login code, optional
2FA), so this uses Portainer's Console feature:

1. Containers → **Add container**
   - Name: `telegram-login-temp`
   - Image: the built `telegram-listener` image (check the exact tag under
     Images — it'll be named after the stack, e.g.
     `telegram-worker-tracker-telegram-listener`)
   - Command: override to `sleep infinity` (keeps it alive so you can attach
     a console — the default command would exit immediately without a
     session string)
   - Env vars: `TG_API_ID`, `TG_API_HASH` (same values as the stack)
   - Deploy the container
2. Open it → **Console** → Connect via `/bin/bash`
3. Run: `python login.py`
4. Follow the prompts: phone number with country code, the login code
   Telegram sends you, 2FA password if you have one set.
5. Copy the printed session string.
6. Stop and remove `telegram-login-temp` — it's no longer needed.
7. Stacks → `telegram-worker-tracker` → Editor → update `TG_SESSION_STRING`
   in Environment variables → **Update the stack**.
8. Confirm: Containers → `telegram-listener` → Logs should now show
   "Watching channels: [...]" instead of crash-looping.

## 6. Generate DASHBOARD_PASSWORD_HASH

No interactive input needed this time:

1. Containers → Add container using the `dashboard` image, Command override:
   `python hash_password.py "your-chosen-password"` → deploy (one-shot, it
   runs and exits).
2. Check its **Logs** for the printed bcrypt hash, copy it.
3. Remove the temp container.
4. Update the stack's `DASHBOARD_PASSWORD_HASH` env var, redeploy.

## 7. Enroll workers

1. Confirm `DATA_DIR/enrolled` exists (e.g.
   `/volume1/docker/telegram-worker-tracker-data/enrolled`) via File Station;
   create it if the stack hasn't auto-created it yet.
2. Upload each worker's 3-5 reference photos there via File Station or an
   SMB share, e.g. `.../enrolled/jane/1.jpg`.
3. Containers → Add container using the `face-worker` image:
   - Command override:
     `python enroll.py --name "Jane Doe" --employee-id E123 --consent-date 2026-07-01 --photos /data/enrolled/jane/1.jpg /data/enrolled/jane/2.jpg /data/enrolled/jane/3.jpg`
   - Env vars: `DATABASE_URL` (same as stack)
   - Volumes: `DATA_DIR/enrolled` → `/data/enrolled`
   - Network: attach to the stack's network (Portainer names it
     `telegram-worker-tracker_default`) so `db` resolves
4. Deploy, check Logs for `Enrolled worker_id=...`, remove the temp
   container. Repeat per worker.

## 8. Map a channel to a site name

Containers → `db` → Console → Connect via `/bin/sh`, then:
```bash
psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "UPDATE channels SET site_label = 'Project Site A' WHERE name = 'some-channel-name';"
```

## 9. Reverse proxy / HTTPS

DSM → Control Panel → Login Portal → Advanced → Reverse Proxy: create a rule
pointing your chosen hostname at `127.0.0.1:8080` (the dashboard). Issue a
Let's Encrypt cert via DSM's Certificate manager. Keep this LAN/VPN-reachable
only — don't port-forward it to the open internet.
