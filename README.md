# Radio Show Archiver (`radioarchive`)

`radioarchive` is a small Python 3.10+ application that records scheduled radio shows from a streaming URL using **ffmpeg**. It’s designed to run as a long-running background process on Windows and Linux, controlled entirely by a YAML file and logs.

## Requirements

- Python **3.10+**
- `ffmpeg` installed and available on PATH (or set `ffmpeg_path` in `schedule.yaml`)

## Setup

From the `radioarchive/` directory:

```bash
python -m venv .venv
```

Activate:

- **Windows (PowerShell)**:

```powershell
.\.venv\Scripts\Activate.ps1
```

- **Linux/macOS (bash)**:

```bash
source .venv/bin/activate
```

Install Python dependency:

```bash
pip install -r requirements.txt
```

## Installing ffmpeg

### Windows

- Option A (recommended): `winget`:

```powershell
winget install Gyan.FFmpeg
```

- Option B: Download a build and set `ffmpeg_path` in `schedule.yaml` to something like `C:/ffmpeg/bin/ffmpeg.exe`.

### Linux (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y ffmpeg
```

## Running

From the `radioarchive/` directory:

```bash
python main.py
```

Logs go to:

- stdout
- `radioarchive.log` (rotating; 5MB max, 3 backups) next to `main.py`

### Web management UI (optional, separate process)

The recorder (`main.py`) and the web UI are **two separate processes**. They coordinate only through files on disk: `schedule.yaml`, `status.json`, and `radioarchive.log`. There is no message queue, database, or socket between them.

From the `radioarchive/` directory, create your private `schedule.yaml` from the template and then add your `web` section (see below):

```bash
copy schedule.example.yaml schedule.yaml
```

```bash
python web/app.py
```

The UI listens on `0.0.0.0` and the port from `web.port` (default `8080`). Use session login with the configured username and password. Change `web.secret_key` to a long random string before exposing the service on a network.

While `main.py` is running, it writes **`status.json`** in the same directory on every scheduler tick. The loop sleeps until the next show start or end when that is soon, otherwise at most about 30 seconds so status, config reload, and ffmpeg health checks stay fresh. The UI reads that file (and never imports `main.py`). Each tick also performs a lightweight HTTP `HEAD` request to `stream_url` and records whether the stream appears reachable.

## Configuration (`schedule.yaml`)

Top-level keys:

- **`stream_url`**: global stream URL (applies to all shows)
- **`output_root`**: recordings root directory (paths are handled via `pathlib.Path`)
- **`ffmpeg_path`**: `ffmpeg` or full path (e.g. `C:/ffmpeg/bin/ffmpeg.exe`)
- **`stream_preroll_seconds`** (optional): integer **0–600**, default **0**. The scheduler starts ffmpeg this many seconds **before** each show’s scheduled start so network connect, probe, and encode time do not cut off the beginning of the program. The scheduled **end** time is unchanged. On the Settings page you can run **Measure stream preroll**, which records one wall-clock minute from the next round minute, reads the output duration with `ffprobe`, and suggests a value (e.g. ~6 if the file is ~54 seconds short of 60).
- **`web`** (optional): required only if you use the web UI; see below
- **`shows`**: list of show entries

### `web` block (management UI)

When present, all of the following fields are required:

- **`username`** / **`password`**: single shared login for the Flask session
- **`port`**: TCP port for the web server (integer)
- **`secret_key`**: secret used to sign cookies (keep private; not editable from the Settings page)

Example:

```yaml
web:
  username: "admin"
  password: "changeme"
  port: 8080
  secret_key: "replace-this-with-a-random-string"
```

The recorder ignores `web`; you can omit it if you only run `main.py`.

**Note:** Saving schedule or settings through the web UI rewrites `schedule.yaml` with PyYAML. **YAML comments in that file are not preserved** on save. Use the “View raw YAML” view or an external editor if you rely on comments.

Show entry fields:

- **`title`** (required): used for subdirectory name (sanitized for Windows + Linux)
- **`day`** (required): full English day name (`Monday`, `Tuesday`, …)
- **`start`** / **`end`** (required): `HH:MM:SS` local time (same-day only; cross-midnight windows are rejected)
- **`format`** (required): one of:
  - `192mp3` (MP3 192kbps CBR)
  - `320mp3` (MP3 320kbps CBR)
  - `wav` (PCM WAV)
- **`enabled`** (optional): default `true`
- **`end_date`** (optional): `YYYY-MM-DD` (exclusive upper bound; no recording will be started on or after this date)
- **`stream_url`** (optional, per-show): accepted for future use, but **not implemented** yet

## Output layout

Recordings are saved under:

`{output_root}/{show_title}/YYYY-MM-DD_HH-MM-SS.{ext}`

Example:

`recordings/Supersonic Radio Show/2026-04-15_14-00-00.mp3`

## Hot reload behavior

The scheduler wakes on every tick (at least every ~30 seconds, and sooner near show start/end times) and checks `schedule.yaml` when its file modification time changes:

- If it changed, it reloads it without restart.
- If the YAML is temporarily invalid while you edit, it logs the error and keeps the last known-good schedule.

## Reconnect behavior

If the stream drops mid-recording:

- ffmpeg is restarted after a 5-second backoff
- each reconnect attempt writes to a new part file
- when the show ends, parts are stitched into a single final file

