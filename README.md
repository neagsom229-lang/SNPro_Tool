# SNPro

A Flask web app that packages twelve everyday media/document conversions
behind one login: video downloading, speech-to-text, video-to-frames,
PDF-to-Word, images-to-video, QR code generation, text-to-speech, AI
background removal, AI background replacement, photo repair, video
audio remove/replace, and a video enhancer.
Long-running conversions run in the background via **Celery + Redis** with
live progress; instant ones (QR code, text-to-speech) run synchronously.
Every user gets their own private upload/output storage folder.

## What's new in this revision

- **Fixed a real production bug (found before it would've bitten you):**
  behind any reverse proxy (Nginx, Caddy, a VPS load balancer), every
  visitor's `request.remote_addr` was the proxy's own IP, not theirs. In
  practice that means Flask-Limiter's rate limits (10/min on the heavy
  tools) would have applied to *all users of the site combined* instead
  of per-visitor, and Flask would think every request was plain HTTP even
  over real HTTPS. Fixed with Werkzeug's `ProxyFix` middleware, verified
  with a request carrying a spoofed `X-Forwarded-For` header to confirm
  `remote_addr` resolves correctly.
- **New:** `FORCE_HTTPS` setting — turns on the `Secure` flag for session
  cookies once you're actually serving over HTTPS, without breaking local
  `python run.py` over plain http (which silently drops `Secure` cookies
  and makes login look broken if you force it on too early).
- **New:** optional **Caddy** service in `docker-compose.yml`
  (`docker compose --profile https up -d --build`) — automatic HTTPS via
  Let's Encrypt for a real domain, no certbot/manual cert handling.
- **Fixed:** mobile browsers' address bar used to eat into `100vh`,
  causing extra scroll/whitespace on phones — switched to `100dvh` with a
  `100vh` fallback for older browsers.
- **Fixed:** `.job-row` (Dashboard/History/Admin) didn't wrap on narrow
  phone screens, crowding the status pill and download/delete buttons
  against the job title. Now wraps, and long filenames break instead of
  overflowing.
- **New README section:** how to try the site on your actual phone right
  now (same-Wi-Fi LAN IP, or an `ngrok` tunnel — no server needed) and how
  to put it on a real domain with HTTPS once you're ready.

## Revision: 5 new tools + admin diagnostics

- **New tools (5):**
  - **AI Background Remover** — cuts a photo's subject out automatically
    using an on-device AI model (`rembg`, u2netp). First run on a fresh
    server needs internet once to download the ~5MB model.
  - **AI Background Replacer** — extracts the subject and composites it
    onto a different photo, with a soft contact shadow and an optional
    rough lighting-direction match.
  - **Photo Repair** — denoise, contrast restoration (CLAHE), and
    sharpening for old/noisy photos. Deliberately keeps color information
    (the original desktop script this is based on forced grayscale, which
    destroyed the photo's colors — fixed here).
  - **Remove / Replace Video Audio** — strip a video's audio track
    entirely, or swap it for an uploaded MP3/WAV.
  - **Video Enhancer** — denoise, sharpen, resize, and optional motion
    smoothing (fps boost). Deliberately capped at **1080p and 15 minutes**:
    this runs per-frame in Python/OpenCV on a shared server via Celery,
    and 4K/8K upscaling (as in the original desktop script) needs
    dedicated GPU hardware to be practical — offering it here would let
    one job stall the whole queue for everyone else.
  - Skipped porting "Retrieve Selected Text in TextBox" — that's a Tkinter
    widget behavior demo, not something that maps to a web tool.
- **New: Admin → System Info** (`/admin/system`) — repurposes the original
  `Show System Info` desktop scripts (which were Windows-only, via `wmi`
  and raw `systeminfo`) into a safe, cross-platform admin diagnostics page:
  OS/CPU info, live CPU/RAM (via `psutil`, optional), disk usage on the
  storage volume, and a Redis/Celery-broker reachability check. Not exposed
  to regular users — server internals shouldn't be public in a multi-tenant
  app.
- **Fixed (found while testing the new tools, not caused by them):**
  `images_to_video` crashed with a confusing "images must be the same
  size" error whenever the uploaded set mixed transparent PNGs with plain
  JPGs — it's actually an RGBA-vs-RGB channel mismatch, not a dimension
  problem. Every frame is now normalized to RGB (and resized to match the
  first frame) before handing them to moviepy.
- **Fixed (dependency conflict):** `rembg` requires `Pillow>=12.1.0`,
  which conflicted with the `Pillow==10.4.0` pin from the previous
  revision. Loosened the pin to `Pillow>=12.1.0,<13.0.0` and re-verified
  *all twelve* tools end-to-end (not just the new ones) against the
  resulting newer Pillow/numpy versions in a clean virtualenv, since old
  `moviepy` (2020) + brand-new numpy/Pillow is exactly the kind of
  combination that silently breaks.
- Landing page and dashboard updated with all 12 tools, small "AI" badges
  on the two AI-powered tools, and a short value-proposition section.

## Earlier revision: core platform fixes

- **Fixed:** `STORAGE_ROOT` could resolve to two different absolute paths
  depending on which directory a process was launched from (same class of
  bug as the earlier SQLite path issue) — output files written by one
  process could be invisible to another. Now always resolved relative to
  the project root.
- **Fixed:** `/tools/job/<id>/download` gave a bare, unexplained 404. It
  now tells you *why* (no result yet, worker not running, or the file went
  missing) and redirects back to the job page with a flash message instead.
- **Fixed:** rate limiting was backed by Redis, which meant the *entire
  site* 500'd if Redis wasn't running yet — even routes with no heavy
  work. Switched to in-process memory storage for limits.
- **New:** real **History** page (`/tools/history`) — paginated, filterable
  by status, with per-job delete (removes the DB row and its output files).
- **New:** dashboard now shows live stats (total/completed/failed jobs,
  storage used) instead of just the tool tiles.
- **New:** basic **Admin panel** (`/admin`) for users with `is_admin=True`:
  site-wide job/user counts, jobs-by-tool breakdown, and a users list where
  you can toggle admin status. Promote your first admin via
  `flask make-admin` (prompts for a username).
- **New:** `Flask-Limiter` (10/min on the two heaviest tools: video
  download, images-to-video), `Flask-Migrate` wired in for future schema
  changes, and automatic retry (2x, exponential backoff) on the video
  download task since network downloads are the flakiest step.
- **New:** `Dockerfile` + `docker-compose.yml` (web + worker + Redis, with
  shared volumes for `instance/` and `storage/` so both containers always
  agree on file locations).

> **Upgrading from an older copy?** The `User` model gained an `is_admin`
> column. `db.create_all()` won't add columns to an existing table, so
> either delete your dev `instance/snpro.db` (you'll lose existing users/
> jobs) or add the column manually: `ALTER TABLE users ADD COLUMN is_admin
> BOOLEAN NOT NULL DEFAULT 0;`
>
> **If you already have a venv from before:** the Pillow pin changed
> (10.4.0 → >=12.1.0,<13.0.0) to support the new AI tools. Run
> `pip install -r requirements.txt` again in your existing venv to pick up
> the new Pillow, rembg, onnxruntime, and psutil packages.

## Stack


- **Flask 3** — app factory pattern, blueprints (`auth`, `main`, `tools`)
- **Flask-SQLAlchemy** — SQLite by default (swap `DATABASE_URL` for Postgres/MySQL)
- **Flask-Login** — session-based auth, password hashing via Werkzeug
- **Flask-WTF** — CSRF-protected forms for login/register
- **Celery + Redis** — background job queue with a `Job` model tracking
  status/progress/result per user
- **Bootstrap 5 + Bootstrap Icons** — responsive UI with a CSS-variable
  based dark/light theme toggle (persisted in `localStorage`)
- **yt-dlp, SpeechRecognition, pydub, OpenCV, pdf2docx, moviepy, qrcode,
  gTTS** — the actual conversion engines, wrapped from the original scripts
- **rembg + onnxruntime** — on-device AI background removal (u2netp model),
  powering the AI Background Remover and AI Background Replacer
- **psutil** (optional) — live CPU/RAM stats on the admin System Info page;
  the page degrades gracefully if it isn't installed

## Project layout

```
snpro/
├── app/
│   ├── __init__.py          # app factory
│   ├── auth/                # register / login / logout
│   ├── main/                # landing page + dashboard
│   ├── tools/
│   │   ├── routes.py        # one route per tool + job status/download
│   │   └── tasks.py         # Celery tasks (download, STT, frames, etc.)
│   ├── templates/
│   └── static/
│       ├── css/style.css    # theme tokens + components
│       └── js/{theme,job}.js
├── config.py                 # Config + per-user storage path helper
├── extensions.py              # db, login_manager, celery factory
├── models.py                  # User, Job
├── run.py                     # Flask dev server entrypoint
├── celery_worker.py           # Celery worker entrypoint
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── Caddyfile                  # optional automatic-HTTPS reverse proxy
```

## Setup

### 1. System dependencies

- **Python 3.10+**
- **Redis** (for Celery). On Ubuntu/Debian: `sudo apt install redis-server`.
  On macOS: `brew install redis`. Or run it in Docker:
  `docker run -p 6379:6379 redis:7`
- **FFmpeg** (required by yt-dlp's MP3 postprocessor and moviepy):
  `sudo apt install ffmpeg` / `brew install ffmpeg`

### 2. Python environment

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# edit .env — at minimum set FLASK_SECRET_KEY to a random string
```

### 4. Run it

Open **three terminals** (all with the venv activated):

```bash
# Terminal 1 — Redis (skip if you already have it running)
redis-server

# Terminal 2 — Celery worker
celery -A celery_worker.celery worker --loglevel=info
# on Windows, add: -P solo

# Terminal 3 — Flask app
python run.py
```

Visit `http://localhost:5000`, create an account, and start converting.

### Windows notes

There's no official native Windows build of Redis anymore, so `redis-server`
won't be a recognized command. Pick one:

1. **Docker Desktop** (easiest): `docker run -p 6379:6379 redis:7`
2. **WSL** (Windows Subsystem for Linux): install Ubuntu via WSL, then
   `sudo apt install redis-server` and run it inside WSL — it's still
   reachable at `localhost:6379` from your Windows Python process.
3. **Memurai** (https://www.memurai.com/) — a native Windows Redis-protocol
   server with a free developer edition.

Also use `-P solo` on the Celery command on Windows (prefork isn't
supported there):
```powershell
celery -A celery_worker.celery worker --loglevel=info -P solo
```

If you previously created a `.env` with `DATABASE_URL=sqlite:///instance/snpro.db`
(a relative path), delete that line or blank it out — the app now always
resolves the SQLite file to an absolute path automatically, which avoids
a `sqlite3.OperationalError: unable to open database file` error that
relative paths can trigger depending on how the process was launched.

### Docker (alternative to the 3-terminal setup)

```bash
cp .env.example .env   # set FLASK_SECRET_KEY
docker compose up --build
```

This starts Redis, the Flask app (via gunicorn), and a Celery worker as
three containers sharing `instance/` and `storage/` through bind mounts —
no manual Redis install needed. Visit `http://localhost:5000`.
The database (SQLite) and per-user file storage are created automatically
on first run, under `instance/` and `storage/`.

### Production notes

- Swap the Flask dev server for `gunicorn run:app` behind a reverse proxy.
- Point `DATABASE_URL` at Postgres/MySQL for concurrent access.
- Storage under `storage/users/<id>/{uploads,outputs}/<tool>/...` grows
  over time — add a cleanup cron/Celery-beat task if you need to prune
  old files.
- Set `MAX_CONTENT_LENGTH_MB` in `.env` to cap upload size.
- Each tool's heavy lifting (yt-dlp, OpenCV, moviepy, pdf2docx) runs
  inside a Celery worker process, so the web process stays responsive
  even on large files.
- Behind any reverse proxy (Nginx, Caddy, a load balancer), the app now
  applies Werkzeug's `ProxyFix` automatically (see `app/__init__.py`).
  Without it, `request.remote_addr` would just be the proxy's own IP for
  every visitor — which would make Flask-Limiter's per-visitor rate
  limits apply to *all users combined* instead of individually, and would
  make Flask think every request is plain HTTP even over real HTTPS.
- Set `FORCE_HTTPS=1` in `.env` once you're actually serving over HTTPS,
  so session cookies get the `Secure` flag. Leave it unset for local
  `python run.py` over plain http, or your own login will look broken
  (browsers silently drop `Secure` cookies sent over non-HTTPS).

### Try it on your phone right now (no server needed)

**Same Wi-Fi as your dev machine** — the fastest option. `python run.py`
already binds to `0.0.0.0`, so it's reachable from any device on the same
network:
1. Find your machine's LAN IP: `ipconfig` (Windows, look for IPv4 Address)
   or `ifconfig` / `ip addr` (Linux/macOS).
2. On your phone's browser, visit `http://<that-ip>:5000` — e.g.
   `http://192.168.1.42:5000`.
3. If it doesn't load, your firewall is likely blocking incoming
   connections on port 5000 — allow it (Windows: "Allow an app through
   Windows Firewall") or temporarily disable the firewall to confirm
   that's the cause.

**Different network, or you want real HTTPS to test with** — use a
tunnel, no deployment required:
```bash
# ngrok (https://ngrok.com, free tier is enough)
ngrok http 5000
```
It prints a public `https://xxxx.ngrok-free.app` URL — open that on your
phone from anywhere (mobile data works too). Since this URL is real
HTTPS, it's also the easiest way to test `FORCE_HTTPS=1` behavior before
you have a real domain.

### Hosting it for real (a domain, real HTTPS, reachable from any phone)

The Docker Compose setup already in this repo includes an optional
**Caddy** service that gets you automatic HTTPS with no manual
certificate work:

1. Get any small Ubuntu VPS (DigitalOcean, Hetzner, a spare machine with
   a public IP — anything that can run Docker).
2. Point your domain's DNS **A record** at the server's public IP.
3. Copy the project to the server, then:
   ```bash
   cp .env.example .env
   # edit .env: set FLASK_SECRET_KEY, FORCE_HTTPS=1, and DOMAIN=yourdomain.com
   docker compose --profile https up -d --build
   ```
4. Once DNS has propagated, visit `https://yourdomain.com` from any
   phone, laptop, or tablet, anywhere. Caddy requests and renews the
   Let's Encrypt certificate on its own — no certbot commands.
5. Once Caddy is fronting the site, you can remove the `ports: 5000:5000`
   line under the `web` service in `docker-compose.yml` so port 5000
   isn't also exposed directly to the internet.

No domain yet? Skip `DOMAIN` and Caddy serves plain HTTP on port 80
instead — fine for quickly checking things from a phone, but keep
`FORCE_HTTPS` unset in that case too.

## Local Run on Windows

1. **Prerequisites:** Python 3.10+, Redis (or Memurai on Windows, or Docker Redis), and FFmpeg (installed and added to system PATH).
2. **Setup virtual environment:**
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. **Configure environment:**
   Copy `.env.example` to `.env` and configure:
   - `FLASK_SECRET_KEY`: your secret key
   - `CELERY_BROKER_URL`: `redis://localhost:6379/0`
   - `CELERY_RESULT_BACKEND`: `redis://localhost:6379/1`
   - `FFMPEG_HWACCEL`: `cuda`, `vulkan`, or leave unset for CPU fallback (`libx264`)
   - `JOB_RETENTION_DAYS`: `7` (default retention for old jobs & storage cleanup)
   - `MAX_ACTIVE_JOBS_PER_USER`: `3` (max concurrent pending/running jobs per user)
4. **Run Redis & Celery Worker:**
   Start Redis (e.g. via Docker or Memurai). In a separate PowerShell terminal:
   ```powershell
   .\venv\Scripts\Activate.ps1
   celery -A celery_worker.celery worker -B --loglevel=info -P solo -Q heavy,light
   ```
5. **Run Flask App:**
   In another PowerShell terminal:
   ```powershell
   .\venv\Scripts\Activate.ps1
   python run.py
   ```
   Visit `http://localhost:5000`.

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | `dev-secret-change-me` | Secret key for Flask sessions and CSRF protection (must be changed in production). |
| `DATABASE_URL` | `sqlite:///instance/app.db` | SQLAlchemy database URI (SQLite by default; use Postgres URL for multi-service production deploys). |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery message broker Redis URL. |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/1` | Celery result backend Redis URL. |
| `STORAGE_ROOT` | `storage` | Root directory for user uploads and job outputs. |
| `FFMPEG_HWACCEL` | `none` | Hardware acceleration mode for FFmpeg (`cuda`, `vulkan`, or empty for CPU). |
| `JOB_RETENTION_DAYS` | `7` | Retention period in days for periodic Celery beat cleanup task. |
| `MAX_ACTIVE_JOBS_PER_USER` | `3` | Maximum active (pending/running) jobs allowed per user simultaneously. |

## How each tool works

| Tool | Route | Execution | Notes |
|---|---|---|---|
| Video Downloader | `/tools/video-downloader` | Celery | yt-dlp, MP4 or MP3, live % progress |
| Speech to Text | `/tools/speech-to-text` | Celery | Google Speech Recognition via `SpeechRecognition`, auto WAV conversion via `pydub` |
| Video to Frames | `/tools/video-to-frames` | Celery | OpenCV frame extraction at a chosen rate, zipped for download |
| PDF to Word | `/tools/pdf-to-word` | Celery | `pdf2docx` conversion |
| Images to Video | `/tools/images-to-video` | Celery | `moviepy` slideshow, optional audio track |
| QR Code Generator | `/tools/qr-code` | Synchronous | `qrcode`, instant PNG |
| Text to Speech | `/tools/text-to-speech` | Synchronous | `gTTS`, instant MP3 |
| AI Background Remover | `/tools/background-remover` | Celery | `rembg` (u2netp), transparent PNG output |
| AI Background Replacer | `/tools/background-replacer` | Celery | `rembg` extraction + PIL compositing, soft shadow, optional lighting-direction match |
| Photo Repair | `/tools/photo-repair` | Celery | OpenCV: color-preserving denoise, CLAHE contrast, unsharp mask |
| Remove / Replace Audio | `/tools/audio-remover` | Celery | `moviepy`, strips or swaps the audio track |
| Video Enhancer | `/tools/video-enhancer` | Celery | OpenCV denoise/sharpen/resize + optional fps-smoothing; capped at 1080p / 15 min for shared-server safety |

All jobs (background or instant) are recorded in the `Job` table and
shown on `/tools/job/<id>`, which polls `/tools/job/<id>/status.json`
every 1.5s until the job finishes, then reveals a download link.

## Security notes

- Passwords are hashed with Werkzeug's `generate_password_hash`.
- All tool routes are `@login_required`; job/download routes verify the
  requesting user owns the job before serving it.
- Uploaded filenames are sanitized with `secure_filename` and stored
  under a per-user, per-tool, UUID-prefixed path.
- Forms are CSRF-protected via Flask-WTF.

## Attribution

The conversion logic in `app/tools/tasks.py` is adapted from a set of
standalone Tkinter desktop scripts (video/audio downloading, speech-to-
text, frame extraction, PDF-to-Word, images-to-video, QR generation,
text-to-speech) into Flask-friendly, per-user background jobs.

## Connect us
# Email: chheangsamnang.wu@gmail.com
