# Configuration (`schedule.yaml`)

Start from `schedule.example.yaml`.

Top-level keys:

- **`stream_url`** (required): stream URL used for all recordings
- **`output_root`** (required): directory where recordings are written (relative paths are relative to the app directory)
- **`ffmpeg_path`** (optional): defaults to `ffmpeg`
- **`stream_preroll_seconds`** (optional): integer \(0–600\), default `0`
- **`shows`** (required): list of scheduled shows (can be empty)
- **`web`** (optional): only required if you run the web UI

Example:

```yaml
stream_url: "http://example.com:8000/stream.aac"
output_root: "recordings"
ffmpeg_path: "ffmpeg"
stream_preroll_seconds: 0
web:
  username: "admin"
  password: "change-me"
  port: 8080
  secret_key: "replace-this-with-a-random-string"
shows:
```

### `web` block (web UI login)

If you run `python web/app.py`, the `web:` block is required:

- **`username`** / **`password`**: single shared login (Flask session)
- **`port`**: TCP port
- **`secret_key`**: cookie signing secret (generate a long random string)

### Show entries

Each entry in `shows:` supports:

- **`title`** (required): used as the show folder name (sanitized for Windows + Linux)
- **`day`** (required): `Monday` … `Sunday`
- **`start`** / **`end`** (required): `HH:MM:SS` local time (same-day only; cross-midnight is rejected)
- **`format`** (required): `192mp3`, `320mp3`, or `wav`
- **`enabled`** (optional): boolean, default `true`
- **`end_date`** (optional): `YYYY-MM-DD` (exclusive upper bound; no starts on/after this date)
- **`stream_url`** (optional): accepted for future use but **not implemented** yet (global `stream_url` is used)

```yaml
shows:
- title: Peter Framptons Jamaican Vacation
  day: Tuesday
  start: '14:00:00'
  end: '16:00:00'
  format: 320mp3
  enabled: true
```

### Notes when editing via the web UI

- **Saving from the UI rewrites `schedule.yaml`** using PyYAML.
- **YAML comments are not preserved** when saving from the UI.

## Output layout

Final recordings are written under:

`{output_root}/{show_title}/YYYY-MM-DD_HH-MM-SS.{ext}`

Example:

`recordings/Supersonic Radio Show/2026-04-15_14-00-00.mp3`

During reconnects, temporary parts are written to:

`.../YYYY-MM-DD_HH-MM-SS.{ext}.parts/part0001...`

# Troubleshooting

- **Dashboard shows recorder stopped**: ensure `python main.py` is running and `status.json` is being updated.
- **`ffmpeg not found`**: install `ffmpeg` and ensure it’s on PATH, or set `ffmpeg_path` in `schedule.yaml`.
- **Schedule edits not taking effect**: if `schedule.yaml` is invalid YAML, the recorder keeps the last known-good config and logs the error.
