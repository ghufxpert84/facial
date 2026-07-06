# Telegram Field-Worker Location Tracker

Tracks where enrolled, consenting workers are currently deployed by matching
faces in photos they post to a shared Telegram channel, using the channel/site
as a location proxy. Captures "Field Report" text as biodata linked to the
worker.

**Privacy design, not optional:** face matching only runs against an enrolled
gallery of your own consenting workers. Any detected face that doesn't match
an enrolled worker is discarded on the spot — never written to disk or DB.
There is no "unknown persons" table. Don't remove this behavior without
re-checking the legal basis for processing bystanders' biometric data.

Storage is SQLite (a single file, no separate database server) — simple
enough for a modest-scale internal tool, and it removed a whole class of
deployment friction compared to running Postgres as its own service.

## Phase 0 — before you start

1. Get `TG_API_ID` / `TG_API_HASH` from https://my.telegram.org, logged in as
   your own Telegram account.
2. Gather 3-5 clear, single-face reference photos per worker you plan to
   enroll, plus their documented consent date.
3. On the Synology DS220+: install **Container Manager** from Package Center
   (DSM 7.2+). Confirm available RAM — 2GB stock is tight running the
   face-recognition model + Telegram client together; 6GB is recommended if
   you can add a second SO-DIMM.

## Setup (SSH / plain docker compose)

```bash
cp .env.example .env
# fill in TG_API_ID, TG_API_HASH, TG_CHANNELS, etc.

docker compose run --rm telegram-listener python login.py
# paste the printed session string into .env as TG_SESSION_STRING

docker compose run --rm dashboard python hash_password.py 'your-password'
# paste the printed hash into .env as DASHBOARD_PASSWORD_HASH

docker compose up -d
```

For Portainer-based deployment (Web editor / Stacks), use
`docker-compose.portainer.yml` instead — see `DEPLOY_PORTAINER.md`.

## Enroll a worker

```bash
docker compose run --rm face-worker python enroll.py \
  --name "Jane Doe" --employee-id E123 \
  --consent-date 2026-07-01 \
  --photos /data/enrolled/jane/1.jpg /data/enrolled/jane/2.jpg /data/enrolled/jane/3.jpg
```

Reference photos need to be placed under `./data/enrolled/` first (bind-mount
this folder in, or `docker cp` them in). Each photo must contain exactly one
face — the command skips and warns on photos with zero or multiple faces.

## Mapping a channel to a human-readable site name

After the listener has run once, the channel will show up in the `channels`
table. Set its `site_label` so the dashboard shows a readable site name
instead of the raw channel title:

```bash
docker compose exec face-worker python -c "
from db import get_conn
conn = get_conn()
conn.execute(\"UPDATE channels SET site_label = ? WHERE name = ?\", ('Project Site A', 'some-channel-name'))
conn.commit()
"
```

(Or, via Portainer: open the `face-worker` container's Console and run the
same snippet, or use any SQLite browser against
`DATA_DIR/db/tracker.db`.)

## Dashboard

Runs on `127.0.0.1:8080` on the NAS (intentionally not exposed beyond
localhost). Put it behind DSM's built-in reverse proxy with HTTPS
(Let's Encrypt) and keep access to LAN/VPN only — this app holds biometric
data and should never be reachable from the open internet.

## Tuning

- `MATCH_THRESHOLD` in `.env` (default 0.45) — cosine similarity cutoff for a
  face to count as a match. Raise it if you see false positives, lower it if
  real workers aren't being matched. Tune against real photos before relying
  on it.
- `RETENTION_DAYS` (default 90) — how long sighting/photo records are kept
  before automatic purge.
- `report_extractor.py`'s parsing rules are a generic placeholder ("Field
  Report" marker + `Key: Value` lines). Share a real anonymized sample
  message to tighten this to your actual format.

## What's been verified vs. what still needs testing on your side

Verified in this environment (no Docker/ML libs available here):
- All Python files compile without syntax errors.
- `report_extractor.py` unit-tested directly (marker detection, key:value
  parsing, no-marker → `None`, marker-with-no-fields → `{}`).
- The SQLite schema and every hand-written query (upserts, the dashboard's
  "last known site" join, sightings/field_reports inserts) run correctly
  against a real SQLite database — verified directly, not just syntax-checked.

Still needs testing once you have the stack running (on the NAS or a Docker
dev machine):
- Face enrollment + recognition against real reference photos (needs
  insightface/onnxruntime, not available in this environment).
- **Bystander check**: run a photo containing only non-enrolled faces through
  the pipeline and confirm zero new rows in `worker_face_embeddings` /
  `sightings` and no leftover image artifacts for that face.
- End-to-end: a real posted photo with a Field Report caption flowing through
  to the dashboard with correct site, timestamp, and parsed fields.
- Concurrent writes from telegram-listener and face-worker under real load
  (WAL mode + busy_timeout should handle it, but worth watching for
  "database is locked" errors under heavy volume).
- Container Manager project startup, reverse proxy/HTTPS, and the retention
  job actually purging old rows.
