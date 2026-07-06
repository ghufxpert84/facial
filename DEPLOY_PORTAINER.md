# Deploying via Portainer (prebuilt images from GHCR)

GitHub Actions builds the three custom images (`telegram-listener`,
`face-worker`, `dashboard`) on every push to `main` and publishes them to
GitHub Container Registry (GHCR). Portainer then just **pulls** those images
— no git clone, no build step, no build-context problem on the NAS.

Storage is SQLite, and as of this version **there are no environment
variables at all** — Telegram credentials, thresholds, retention, poll
interval, and user accounts are all configured through the dashboard's web
UI after deploying, not Portainer's environment variable fields. This also
means the earlier Portainer `${VAR}` substitution issues (which caused
several failed deploys before) no longer apply — the compose file is fully
static.

One caveat remains: **bootstrapping Portainer itself typically needs one SSH
command**, because Synology's Container Manager GUI generally can't
bind-mount `/var/run/docker.sock` (it only browses shared folders).
Everything after that — the app stack, worker enrollment, all configuration
— is 100% Portainer UI + the dashboard's own web UI.

## 1. Let GitHub Actions build the images

The workflow at `.github/workflows/build-and-push.yml` triggers on push to
`main`. After pushing, check the **Actions** tab on
`github.com/ghufxpert84/facial` and confirm all three build jobs go green.
Images then appear under your GitHub profile → **Packages** as:
- `ghcr.io/ghufxpert84/facial-telegram-listener`
- `ghcr.io/ghufxpert84/facial-face-worker`
- `ghcr.io/ghufxpert84/facial-dashboard`

If these packages are private, pulling requires a **classic** PAT with
`read:packages` + `repo` scopes (private-repo-linked packages need `repo`
too, not just `read:packages`) — configured as a Registry in Portainer
(step 4). Making the packages public removes this requirement entirely,
since they contain no secrets or employee data, only infrastructure code.

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

## 4. (If packages are private) Add GHCR as a registry in Portainer

1. Create a **classic** PAT (GitHub → Settings → Developer settings →
   Personal access tokens → Tokens (classic)) with `read:packages` **and**
   `repo` scopes checked.
2. Portainer → **Registries** → **Add registry** → Custom
   - Name: `ghcr`
   - URL: `ghcr.io`
   - Authentication: ON → username = your GitHub username → password = the
     PAT from step 1.

## 5. Create the folder for persistent data

Via File Station, create `/volume1/docker/telegram-worker-tracker-data`
with three empty subfolders inside it: `db`, `incoming`, `enrolled`.

## 6. Create the stack

Portainer → Environments → local → **Stacks** → **Add stack**

- Name: `telegram-worker-tracker`
- Build method: **Web editor**
- Paste the contents of `docker-compose.portainer.yml` from this repo (it
  references only `ghcr.io/ghufxpert84/facial-*` images and hardcodes the
  `/volume1/docker/telegram-worker-tracker-data` path — update that path in
  the pasted text first if you used a different folder).
- **Environment variables**: none needed. Leave this section empty.
- Click **Deploy the stack**.

`telegram-listener` will log "Waiting for Telegram to be connected via the
admin UI..." on a loop until you complete step 7 below — that's expected,
not an error. `face-worker` and `dashboard` come up immediately.

## 7. First-run setup (in your browser)

1. Open the dashboard at `http://<nas-ip>:8080` (or via a reverse proxy —
   see step 8). With no users yet, you'll see a **Create your admin
   account** page instead of a login form.
2. Log in, then go to **Admin → Telegram**: enter your API ID/hash (from
   my.telegram.org) and phone number → **Send code**. Enter the login code
   Telegram sends you, and your 2FA password if you have one set. Done —
   no Portainer Console, no temp containers, no copy-pasting session
   strings.
3. **Admin → Settings**: set the channels to watch (comma-separated
   usernames/ids), match threshold, retention window, poll interval.
4. **Admin → Users**: add `viewer` accounts for anyone who should only see
   the dashboard.
5. Confirm in Portainer: `telegram-listener`'s Logs should now show
   "Watching channels: [...]" instead of the waiting message.

## 8. Enroll workers

1. Upload each worker's 3-5 reference photos to
   `/volume1/docker/telegram-worker-tracker-data/enrolled/<name>/` via File
   Station, e.g. `.../enrolled/jane/1.jpg`.
2. Containers → Add container using
   `ghcr.io/ghufxpert84/facial-face-worker:latest`:
   - Command override:
     `python enroll.py --name "Jane Doe" --employee-id E123 --consent-date 2026-07-01 --photos /data/enrolled/jane/1.jpg /data/enrolled/jane/2.jpg /data/enrolled/jane/3.jpg`
   - Volumes:
     - `/volume1/docker/telegram-worker-tracker-data/db` → `/data/db`
     - `/volume1/docker/telegram-worker-tracker-data/enrolled` → `/data/enrolled`
3. Deploy, check Logs for `Enrolled worker_id=...`, remove the temp
   container. Repeat per worker.

## 9. Map a channel to a site name

Do this from the dashboard now — **Admin → Channels** lists every channel
the listener has seen, with an inline field to set its site label. No
Console/SQL needed anymore.

## 10. Reverse proxy / HTTPS

DSM → Control Panel → Login Portal → Advanced → Reverse Proxy: create a rule
pointing your chosen hostname at `127.0.0.1:8080` (the dashboard). Issue a
Let's Encrypt cert via DSM's Certificate manager. Keep this LAN/VPN-reachable
only — don't port-forward it to the open internet.

## Updating the stack after a code change

Push to `main` → Actions rebuilds and re-pushes the `:latest` images →
Portainer → Stacks → `telegram-worker-tracker` → **Pull and redeploy** (or
delete + recreate the containers) to pick up the new image. Since there are
no environment variables to re-enter, this is now a one-click operation.
