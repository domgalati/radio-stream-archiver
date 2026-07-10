## Radio Archive (`radioarchive`)

`radioarchive` is a small Python app that **records scheduled radio shows from a stream URL** using `ffmpeg`.

It’s designed to be simple to operate:

- **One YAML file** (`schedule.yaml`) controls the schedule and settings
- An optional **web UI** lets you manage the schedule and browse recordings (runs as a separate process from listener/recorder)

# Optional Web UI

- **History**: scan `output_root` for recordings; play/download files
- **Logs**: tail of `radioarchive.log`
- **Settings**: edit global settings and run “Measure stream preroll”


- **Dashboard**: live status for recording module
![Dashboard](docs/screenshots/Dashboard.png)

- **Schedule**: add/edit/delete shows with a UI
![Schedule](docs/screenshots/Schedule.png)

## Requirements

- **Python 3.10+**
- **ffmpeg** available on PATH (or set `ffmpeg_path` in `schedule.yaml`)

Python dependencies:

- `pyyaml`
- `flask`
- `mutagen`

# Setup

### From the `radioarchive/` directory:

```bash
python -m venv .venv
```

### Activate the venv:

- **Windows (PowerShell)**:

```powershell
.\.venv\Scripts\Activate.ps1
```

- **Linux/macOS (bash/zsh)**:

```bash
source .venv/bin/activate
```

### Install dependencies:

```bash
pip install -r requirements.txt
```

### Create your config:

```bash
cp schedule.example.yaml schedule.yaml
```

Then edit `schedule.yaml` (stream URL, output folder, shows, and optionally web UI credentials).

## Installing ffmpeg
ffmpeg is at the core of this projects functionality and is a hard requirement.
### Windows

Using `winget`:

in powershell:
```powershell
winget install Gyan.FFmpeg
```

optionally, you can download a build and set `ffmpeg_path` var in `schedule.yaml` (example: `C:/ffmpeg/bin/ffmpeg.exe`).

### Linux (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y ffmpeg
```

# Running

### Recorder (scheduler + recording loop)

From the `radioarchive/` directory:

```bash
python main.py
```


### Web management UI (optional, separate process)

The web UI is a separate Flask process. To enable it, your `schedule.yaml` must include a `web:` block (see below).
```yaml
## Security here will be beefed up in future revisions, as of now this is only meant to run locally.
web:
  username: "admin"
  password: "change-me"
  port: 8080
```

Run:

```bash
python web/app.py
```

Then open `http://localhost:<web.port>` and log in.

# Configuration (`schedule.yaml`)

Refer to [DOCS.md](DOCS.md)
