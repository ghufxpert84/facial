# Deploying via Portainer (prebuilt images from GHCR)

GitHub Actions builds the three custom images (`telegram-listener`,
`face-worker`, `dashboard`) on every push to `main` and publishes them to
GitHub Container Registry (GHCR). Portainer then just **pulls** those images
— no git clone, no build step, no build-context problem on the NAS.

One caveat remains: **bootstrapping Portainer itself typically needs one SSH
command**, because Synology's Container Manager GUI generally can't
bind-mount `/var/run/docker.sock` (it only browses shared folders).
Everything after that — the app stack, worker enrollment, tuning — is
100% Portainer UI.

## 1. Let GitHub Actions build the images

The workflow at `.github/workflows/build-and-push.yml` triggers on push to
`main`. After pushing, check the **Actions** tab on
`github.com/ghufxpert84/facial` and confirm all three build jobs go green.
Images then appear under your GitHub profile → **Packages** as:
- `ghcr.io/ghufxpert84/facial-telegram-listener`
- `ghcr.io/ghufxpert84/facial-face-worker`
- `ghcr.io/ghufxpert84/facial-dashboard`

These are **private** packages (inherited from the private repo) — that's
intentional, given what this system processes. Pulling them requires a
credential (step 4).

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

## 4. Add GHCR as a registry in Portainer

Since the images are private, Portainer needs a credential to pull them:

1. Create a **classic** PAT (GitHub → Settings → Developer settings →
   Personal access tokens → Tokens (classic)) with just the `read:packages`
   scope. (Fine-grained tokens have inconsistent package support — classic
   is more reliable for GHCR pulls.)
2. Portainer → **Registries** → **Add registry** → Custom
   - Name: `ghcr`
   - URL: `ghcr.io`
   - Authentication: ON → username = your GitHub username → password = the
     PAT from step 1.

## 5. Create the stack

Portainer → Environments → local → **Stacks** → **Add stack**

- Name: `telegram-worker-tracker`
- Build method: **Web editor**
- Paste the contents of this project's `docker-compose.yml` directly (it
  now references `ghcr.io/ghufxpert84/facial-*` images, no build context
  needed).
- **Environment variables** (add each as a key/value pair):
  - `TG_API_ID`, `TG_API_HASH`, `TG_CHANNELS`
  - `TG_SESSION_STRING` — leave blank for now (step 6)
  - `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
  - `DATABASE_URL` — must match the Postgres values above,
    e.g. `postgresql://tracker:change_me@db:5432/worker_tracker`
  - `POLL_INTERVAL_SECONDS` (60), `MATCH_THRESHOLD` (0.45),
    `RETENTION_DAYS` (90)
  - `DASHBOARD_USER` (e.g. `admin`)
  - `DASHBOARD_PASSWORD_HASH` — leave blank for now (step 7)
  - `DATA_DIR` — **set this to an absolute path**, e.g.
    `/volume1/docker/telegram-worker-tracker-data` (create it via File
    Station first).
- Click **Deploy the stack**. Portainer pulls the three GHCR images using the
  registry credential from step 4, plus the public `pgvector/pgvector:pg16`
  image.

`telegram-listener` will crash-loop until `TG_SESSION_STRING` is set — that's
expected, it exits on purpose when the session string is missing. The other
three services should come up fine.

## 6. Generate TG_SESSION_STRING (no SSH)

`login.py` needs an interactive prompt (phone number, login code, optional
2FA), so this uses Portainer's Console feature:

1. Containers → **Add container**
   - Name: `telegram-login-temp`
   - Image: `ghcr.io/ghufxpert84/facial-telegram-listener:latest`
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

## 7. Generate DASHBOARD_PASSWORD_HASH

No interactive input needed this time:

1. Containers → Add container using
   `ghcr.io/ghufxpert84/facial-dashboard:latest`, Command override:
   `python hash_password.py "your-chosen-password"` → deploy (one-shot, it
   runs and exits).
2. Check its **Logs** for the printed bcrypt hash, copy it.
3. Remove the temp container.
4. Update the stack's `DASHBOARD_PASSWORD_HASH` env var, redeploy.

## 8. Enroll workers

1. Confirm `DATA_DIR/enrolled` exists (e.g.
   `/volume1/docker/telegram-worker-tracker-data/enrolled`) via File Station;
   create it if the stack hasn't auto-created it yet.
2. Upload each worker's 3-5 reference photos there via File Station or an
   SMB share, e.g. `.../enrolled/jane/1.jpg`.
3. Containers → Add container using
   `ghcr.io/ghufxpert84/facial-face-worker:latest`:
   - Command override:
     `python enroll.py --name "Jane Doe" --employee-id E123 --consent-date 2026-07-01 --photos /data/enrolled/jane/1.jpg /data/enrolled/jane/2.jpg /data/enrolled/jane/3.jpg`
   - Env vars: `DATABASE_URL` (same as stack)
   - Volumes: `DATA_DIR/enrolled` → `/data/enrolled`
   - Network: attach to the stack's network (Portainer names it
     `telegram-worker-tracker_default`) so `db` resolves
4. Deploy, check Logs for `Enrolled worker_id=...`, remove the temp
   container. Repeat per worker.

## 9. Map a channel to a site name

Containers → `db` → Console → Connect via `/bin/sh`, then:
```bash
psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "UPDATE channels SET site_label = 'Project Site A' WHERE name = 'some-channel-name';"
```

## 10. Reverse proxy / HTTPS

DSM → Control Panel → Login Portal → Advanced → Reverse Proxy: create a rule
pointing your chosen hostname at `127.0.0.1:8080` (the dashboard). Issue a
Let's Encrypt cert via DSM's Certificate manager. Keep this LAN/VPN-reachable
only — don't port-forward it to the open internet.

## Updating the stack after a code change

Push to `main` → Actions rebuilds and re-pushes the `:latest` images →
Portainer → Stacks → `telegram-worker-tracker` → **Pull and redeploy** (or
delete + recreate the containers) to pick up the new image.
