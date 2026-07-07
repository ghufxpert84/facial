# Telegram Field-Worker Location Tracker

Tracks where enrolled, consenting workers are currently deployed by matching
faces in photos (and now videos — a representative frame is extracted and
matched the same way) they post to a shared Telegram channel, using the
channel/site as a location proxy.

Each worker's profile page (click their name from the directory) shows a
small avatar thumbnail, a combined photo gallery (enrollment reference
photos + every sighting photo, with a "video" badge/link where the sighting
came from a video), movement history, the current branch's location/
contacts, and a **Feedback** section — free-text comments any logged-in
user can post about that worker (attendance notes, incidents, training
completed), shown newest-first with the author and timestamp, GitHub-issue
style. Anyone can delete their own comment; admins can delete any comment.
Admins can edit a worker's details or permanently delete them (cascades to
their embeddings, sightings, and feedback comments) from there too.

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
3. **Admin → Settings**: match threshold, retention window, poll interval,
   history pull limit. (Which channels to watch now lives on Admin →
   Channels, not here.)
4. **Admin → Users**: add `viewer` accounts for anyone who should see the
   dashboard without managing settings.
5. **Admin → Channels**: this is where you actually add channels and manage
   them:
   - **Add channels to watch** — a textarea, one username or numeric channel
     id per line.
   - **Enable/disable** toggle per channel — pauses scanning without losing
     its history, site label, or any collected data. A disabled channel
     just sits idle until re-enabled.
   - **Reset scan** — restarts a channel's scan from the History Pull Limit
     window again (i.e. treats it like newly-added: re-pulls the last N
     hours). Different from **skip to latest**, which instead jumps forward
     past any remaining backlog straight to the newest message. Use reset
     scan if you suspect recent photos were missed; use skip to latest if
     you just want to stop waiting on old history.
   - A **scan progress bar** (`telegram-listener`'s message-ID counter vs.
     the channel's current newest message) — tells you how far through the
     backlog it's gotten, not just whether it's "stuck."
   - A **pending face-match** count — the separate `face-worker` queue of
     already-downloaded photos/frames not yet matched against workers. A
     stuck progress bar points at `telegram-listener`; a growing pending
     count points at `face-worker` — check Admin → Logs for whichever one
     isn't moving.
   - A **last scanned** timestamp per channel, updated every poll cycle
     regardless of whether new messages were found.
   - Once a channel is resolved (telegram-listener has seen it at least
     once), you can give it a human-readable **site label**.
   - **Remove** deletes it from the watchlist (stops scanning) but keeps all
     historical sightings/worker data intact — re-add the same identifier
     later to resume.
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

The worker directory has a **branch filter** — a dropdown to show only
workers whose last-known site matches a selected branch. A photo's site
label (Admin → Channels) automatically creates/links a **Branch** entity
(Admin → Branches), where you can fill in address, a map link, and Telegram/
WhatsApp/WeChat contact details. When a channel is first discovered or its
scan is reset, the system also fetches the channel's own Telegram "About"
text and best-effort extracts `Address:`/`Telegram:`/`WhatsApp:`/`WeChat:`
style lines into the branch automatically (never overwriting a manual edit)
— the raw captured text is always kept too, in case the auto-extraction
missed something.

On a worker's profile page, the current branch's contacts show as tappable
icons instead of plain text: **Telegram** and **WhatsApp** open straight
into a chat (`t.me/...` / `wa.me/...` deep links — the Telegram field
should hold a username, the WhatsApp field a phone number with country
code). **WeChat** has no equivalent public deep link to open a chat by ID,
so its icon instead copies the ID to the clipboard with a "Copied!"
confirmation, since the recipient still has to search for it inside WeChat.

A worker's photo gallery uses an in-page **lightbox** (click a thumbnail to
view full-size, or play a video, without leaving the page — Esc or click
outside to close) instead of opening a new tab.

The **Branch directory** page (sidebar, visible to all logged-in users) is
a browsable card grid of every branch — separate from **Admin → Branches**,
which is where address/contacts/coordinates are actually configured.
Clicking a card opens that branch's own page: its map pin, contacts, a
free-text **description** field (e.g. site supervisor, access instructions
— editable by admins, read-only for viewers), and the live list of workers
currently there (same "last known site" logic used everywhere else),
linking straight to each worker's profile.

The **Map** page (sidebar, visible to all logged-in users) plots every
geocoded branch on an OpenStreetMap map (via Leaflet.js) — no API key
needed to view it, since displaying OSM tiles is free. Each pin's popup
shows the branch's address, how many workers are currently there (same
"last known site" logic as the worker directory), and its Telegram/
WhatsApp/WeChat contacts. To place a branch on the map, go to **Admin → Branches** and
either type in its latitude/longitude directly, or save an address and
click "look up from address" to auto-fill coordinates via OpenStreetMap's
free Nominatim geocoder (rate-limited, fine for occasional manual lookups).
If you'd rather use a paid geocoding provider for higher volume/reliability,
**Admin → Settings** has a provider switch (Nominatim / LocationIQ) and an
API key field — only the address-to-coordinates lookup ever needs a key,
never the map display itself.

On **Admin → Channels**, the scan progress bar now updates live (polling
every few seconds) instead of only refreshing on page reload — clicking
**reset scan** drops it to 0% immediately and you can watch it climb back
to 100% as telegram-listener re-scans the History Pull Limit window.

Everything is stored in UTC (the correct practice — avoids ambiguity), but
every timestamp shown in the UI is converted to **GMT+8** for display
(column headers say so explicitly). If you're ever inspecting the database
directly (e.g. via a SQLite browser or `Admin -> Telegram`'s underlying
data), remember the raw values there are UTC.

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
- `face-worker`'s auto-parsed "Field Report" captions (via
  `report_extractor.py`) still get written to the `field_reports` table in
  the background — that pipeline wasn't removed — but the dashboard no
  longer displays them. The worker profile's **Feedback** section (see
  above) is the new, manually-authored replacement for that space. If you'd
  rather fully retire the caption-parsing pipeline (stop face-worker from
  writing to `field_reports` at all), say so and it can be removed cleanly.

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
- The duplicate-sightings bug (multiple identical photos in a worker's
  gallery): reproduced the exact scenario from a real screenshot (3
  sightings sharing one timestamp), confirmed the migration collapses them
  to 1 and the new unique index + idempotent insert prevents it recurring.
- Branch auto-creation/linking from a channel's site label, unlinking on
  clear (branch record itself stays intact), and the channel-About-text
  extraction into address/telegram/wechat fields (only filling empty
  fields, never clobbering a manual edit).
- The worker directory's branch filter query (unfiltered, filtered to a
  specific branch, and a branch with zero workers).
- The `branches.latitude`/`longitude` migration on a pre-existing database
  (columns retrofit correctly, safe to run repeatedly across requests).
- The `/map` page's worker-count-per-branch query and its `unplaced_count`
  (branches missing coordinates) calculation, against seeded data.
- The reset-scan progress math: confirmed `last_message_id` zeroing on
  reset drops the percentage to exactly 0% (not stale), and climbs
  correctly back to 100% as it's updated across simulated poll cycles.
- Every dashboard template (including the new sidebar layout, Map page, and
  updated Branches/Settings/Channels pages) actually renders with Jinja2
  installed locally, both logged-out (login/setup) and logged-in
  (admin/viewer), across empty and populated data — catches template
  syntax errors before deploy, though it can't verify Tailwind/Leaflet
  actually look right in a browser.
- The `branches.whatsapp_contact` migration on a pre-existing database, and
  the `t.me`/`wa.me` deep-link builders: confirmed leading `@` is stripped
  from Telegram usernames, non-digit characters (spaces, dashes, `+`) are
  stripped from WhatsApp numbers, and both return `None` cleanly when the
  contact field is empty (so the icon just doesn't render, rather than
  linking to a broken URL).
- The `branches.description` and `worker_comments` migrations/schema
  against a real SQLite database: description retrofits onto a
  pre-existing `branches` table; deleting a user sets `worker_comments.
  user_id` to `NULL` but keeps the comment (with its denormalized
  `author_username`) rather than losing it; deleting a worker cascades to
  delete their comments.
- The Branch directory's per-branch worker-count query and the branch
  detail page's "workers here now" query, against seeded sightings —
  including that a worker with zero sightings correctly doesn't appear
  anywhere.
- The comment delete-permission rule (comment owner or an admin, nobody
  else) against all three combinations directly, plus confirmed via a
  template render that a viewer never sees a delete link on someone else's
  comment.

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
- End-to-end: a real posted photo flowing through to a worker's profile
  with the correct site and timestamp.
- Concurrent writes from telegram-listener and face-worker under real load
  (WAL mode + busy_timeout should handle it, but worth watching for
  "database is locked" errors under heavy volume).
- Container Manager project startup, reverse proxy/HTTPS, and the retention
  job actually purging old rows.
- The "look up from address" button actually reaching Nominatim (or
  LocationIQ, if configured) over the network and getting back usable
  coordinates for a real address — `requests` isn't installed in this
  environment, so only the surrounding SQL/route logic was verified, not
  the live HTTP call.
- The Map page rendering real OpenStreetMap tiles in a browser (Leaflet
  loaded from a CDN — needs outbound internet from wherever you view the
  dashboard, same as any browser-based map).
- The Admin → Channels progress bar actually animating in a real browser
  after clicking reset scan (the underlying DB math is verified above, but
  the polling JS itself needs a browser to confirm).
- On an actual phone, tapping the Telegram/WhatsApp icons on a worker
  profile should hand off to the installed app (or its web fallback if not
  installed) — this depends on the OS/browser's URI-scheme handling, which
  can't be exercised outside a real device. Same for confirming
  `navigator.clipboard.writeText` succeeds for the WeChat "copy ID" button
  (requires a secure context — HTTPS or localhost — so this only works
  once the dashboard is behind the HTTPS reverse proxy mentioned above).
- The branch detail page's Leaflet map rendering real tiles in a browser,
  same caveat as the Map page above.
- Posting and deleting feedback comments through the actual logged-in
  session (auth/session handling isn't exercised by the SQLite-only tests
  above, since `bcrypt`/`itsdangerous` aren't installed in this
  environment).
