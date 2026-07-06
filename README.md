# Telegram Field-Worker Location Tracker

Tracks where enrolled, consenting workers are currently deployed by matching
faces in photos they post to a shared Telegram channel, using the channel/site
as a location proxy. Captures "Field Report" text as biodata linked to the
worker.

**Privacy design, not optional:** face matching only runs against an enrolled
gallery of your own consenting workers. A face that doesn't match an
enrolled worker is never auto-enrolled or silently discarded either — it's
staged in a temporary review queue (**Admin → Unrecognized Faces**) where an
admin must explicitly name it (recording consent at that moment, creating a
real worker) or dismiss it (e.g. a bystander in a shared channel). Nothing
becomes a permanent biometric record without that human step, and unreviewed
candidates auto-expire after a configurable window (default 72 hours) so
this never becomes a de facto "unknown persons" database. Don't change this
behavior without re-checking the legal basis for processing bystanders'
biometric data — the channel is shared with other companies' staff, and
this design exists specifically to keep their unconsented faces from
becoming permanent records.

Storage is SQLite (a single file, no separate database server). All runtime
configuration (Telegram credentials, match threshold, retention window, poll
interval) and user accounts live in that same database, managed through the
dashboard's web UI — **no environment variables are required**.

## Phase 0 — before you start

1. Get `TG_API_ID` / `TG_API_HASH` from https://my.telegram.org, logged in as
   your own Telegram account (you'll enter these in the web UI, not a file).
2. Gather 3-5 clear, single-face reference photos per worker you plan to
   enroll, plus their documented consent date.
3. On the Synology DS220+: install **Container Manager** from Package Center
   (DSM 7.2+). Confirm available RAM — 2GB stock is tight running the
   face-recognition model + Telegram client together; 6GB is recommended if
   you can add a second SO-DIMM.

## Setup (SSH / plain docker compose)

```bash
cp .env.example .env
# only DATA_DIR needs setting, and only if you want an absolute path

docker compose up -d
```

For Portainer-based deployment (Web editor / Stacks), use
`docker-compose.portainer.yml` instead — see `DEPLOY_PORTAINER.md`.

### First-run setup (in your browser)

1. Visit the dashboard (`http://<host>:8080` or wherever you've exposed it).
   With no users yet, you're shown a **Create your admin account** page
   instead of a login form.
2. Log in, then go to **Admin → Telegram**: enter your API ID/hash and phone
   number, then the login code Telegram sends you (and your 2FA password if
   you have one set). No Portainer Console, no temp containers.
3. **Admin → Settings**: set which channels to watch (comma-separated
   usernames/ids), match threshold, retention window, poll interval.
4. **Admin → Users**: add `viewer` accounts for anyone who should see the
   dashboard without managing settings.
5. **Admin → Channels**: once the listener has processed a channel's first
   message, give it a human-readable site label here. Also shows a **last
   scanned** timestamp per channel, updated every poll cycle regardless of
   whether new messages were found — a quick way to confirm the listener is
   actually alive and scanning, not just idle/stuck.
6. **Admin → Logs**: notable events from `telegram-listener` and
   `face-worker` (connection changes, download failures, errors) — no need
   to open Portainer's container logs for routine troubleshooting.

## Enrolling workers

Two ways to get a worker enrolled:

**Option A — from a photo already seen in the channel (no CLI needed).**
Once `telegram-listener`/`face-worker` are running, any face that doesn't
match an enrolled worker shows up in **Admin → Unrecognized Faces** with a
thumbnail. Click **Name this person**, fill in their name, employee ID, and
confirm their consent date — this both enrolls them and links that exact
sighting to their profile. This is the normal path for "someone new showed
up on camera."

**Option B — pre-enroll ahead of time (CLI), e.g. before anyone's been
photographed yet:**

```bash
docker compose run --rm face-worker python enroll.py \
  --name "Jane Doe" --employee-id E123 \
  --consent-date 2026-07-01 \
  --photos /data/enrolled/jane/1.jpg /data/enrolled/jane/2.jpg /data/enrolled/jane/3.jpg
```

Reference photos need to be placed under `./data/enrolled/` first (bind-mount
this folder in, or `docker cp` them in). Each photo must contain exactly one
face — the command skips and warns on photos with zero or multiple faces.

## Dashboard

Runs on `127.0.0.1:8080` on the NAS (intentionally not exposed beyond
localhost). Put it behind DSM's built-in reverse proxy with HTTPS
(Let's Encrypt) and keep access to LAN/VPN only — this app holds biometric
data and should never be reachable from the open internet.

## Tuning

All of the below are set via **Admin → Settings** in the dashboard, not env
vars or a redeploy:
- **Match threshold** (default 0.45) — cosine similarity cutoff for a face to
  count as a match. Raise it if you see false positives, lower it if real
  workers aren't being matched. Tune against real photos before relying on it.
- **History pull limit (hours)** (default 24) — when a channel is watched
  for the first time, only messages from this far back are pulled, not the
  channel's entire history. Only affects a channel's very first poll;
  ongoing polling always catches every new message going forward regardless
  of this setting.
- **Retention (days)** (default 90) — how long sighting/photo records are
  kept before automatic purge.
- **Unrecognized face review window (hours)** (default 72) — how long an
  unmatched face waits in Admin → Unrecognized Faces before being
  auto-purged if nobody names or dismisses it.
- `report_extractor.py`'s parsing rules are still a generic placeholder
  ("Field Report" marker + `Key: Value` lines) — this one does require a code
  change. Share a real anonymized sample message to tighten it to your actual
  format.

## What's been verified vs. what still needs testing on your side

Verified in this environment (no Docker/ML libs available here):
- All Python files compile without syntax errors.
- `report_extractor.py` unit-tested directly (marker detection, key:value
  parsing, no-marker → `None`, marker-with-no-fields → `{}`).
- The full SQLite schema (including `users`/`app_settings`/
  `unrecognized_faces`) and every hand-written query (upserts, role changes,
  the dashboard's "last known site" join, settings get/set, candidate
  staging/dedupe-update/enroll-from-candidate/dismiss/retention-purge) run
  correctly against a real SQLite database — verified directly, not just
  syntax-checked.
- The shared secret-key file generation logic (used for session signing and
  encrypting Telegram credentials at rest) — verified it's created once and
  reused consistently, with `0600` permissions.

Still needs testing once you have the stack running (on the NAS or a Docker
dev machine) — none of `bcrypt`, `cryptography`, `itsdangerous`, or
`telethon` are installed in this environment:
- Face enrollment + recognition against real reference photos.
- **Bystander check**: run a photo containing only non-enrolled faces through
  the pipeline and confirm they land in `unrecognized_faces` (with a viewable
  crop in Admin → Unrecognized Faces), not `worker_face_embeddings`/
  `sightings` directly.
- Unrecognized-faces dedupe: post the same unmatched person's photo twice
  and confirm it updates one candidate's `last_seen`/`sightings_count`
  rather than creating a duplicate row.
- Naming a candidate from Admin → Unrecognized Faces actually creates a
  working enrolled worker (i.e. their face now matches on the next photo).
- The full auth flow: `/setup` on an empty database, login, logout, role
  enforcement (`viewer` blocked from `/admin/*`).
- The Telegram Connect wizard end-to-end (phone → code → optional 2FA →
  `telegram-listener` picks up the new session and starts polling).
- `/admin/settings` changes actually taking effect on `face-worker`'s next
  loop iteration without a redeploy.
- End-to-end: a real posted photo with a Field Report caption flowing through
  to the dashboard with correct site, timestamp, and parsed fields.
- Concurrent writes from telegram-listener and face-worker under real load
  (WAL mode + busy_timeout should handle it, but worth watching for
  "database is locked" errors under heavy volume).
- Container Manager project startup, reverse proxy/HTTPS, and the retention
  job actually purging old rows.
