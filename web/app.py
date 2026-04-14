from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    Response,
    send_file,
    session,
    url_for,
)

APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import ConfigError, WEEKDAY_NAMES, ShowFormat, load_config  # noqa: E402
from recorder import build_stream_record_cmd  # noqa: E402

SCHEDULE_PATH = APP_ROOT / "schedule.yaml"
STATUS_PATH = APP_ROOT / "status.json"
LOG_PATH = APP_ROOT / "radioarchive.log"
STREAM_PREROLL_MEASURE_DIR = WEB_DIR / "data"
STREAM_PREROLL_MEASURE_STATE_PATH = STREAM_PREROLL_MEASURE_DIR / "stream_preroll_measure.json"
_stream_preroll_measure_lock = threading.Lock()

# HTMX poll intervals (seconds) for live fragments — tune UI refresh vs server load here.
DASHBOARD_LIVE_POLL_SECONDS = 2
LOGS_TAIL_POLL_SECONDS = 2

# If last_tick is older than this, the UI treats status.json as stale (recorder likely stopped).
DASHBOARD_TICK_STALE_SECONDS = 45

FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.(mp3|wav)$", re.IGNORECASE)


def read_yaml_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML: {e}") from e
    if raw is None or not isinstance(raw, dict):
        raise ConfigError("YAML root must be a mapping/object.")
    return raw


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    tmp = path.parent / f"{path.name}.tmp"
    out = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(path)


def validate_schedule_document(data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        delete=False,
        encoding="utf-8",
    ) as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp_name = f.name
    tmp_path = Path(tmp_name)
    try:
        load_config(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def load_cfg() -> Any:
    return load_config(SCHEDULE_PATH)


def show_dict_from_form(form: Any) -> dict[str, Any]:
    title = (form.get("title") or "").strip()
    day = (form.get("day") or "").strip()
    start = (form.get("start") or "").strip()
    end = (form.get("end") or "").strip()
    fmt = (form.get("format") or "").strip()
    enabled = form.get("enabled") == "1" or form.get("enabled") == "on"
    end_date_raw = (form.get("end_date") or "").strip()
    entry: dict[str, Any] = {
        "title": title,
        "day": day,
        "start": start if len(start) == 8 else f"{start}:00" if re.match(r"^\d{2}:\d{2}$", start) else start,
        "end": end if len(end) == 8 else f"{end}:00" if re.match(r"^\d{2}:\d{2}$", end) else end,
        "format": fmt,
        "enabled": enabled,
    }
    if end_date_raw:
        entry["end_date"] = end_date_raw
    return entry


def normalize_time_hhmmss(s: str) -> str:
    s = s.strip()
    if re.match(r"^\d{2}:\d{2}$", s):
        return f"{s}:00"
    return s


def read_status() -> dict[str, Any]:
    if not STATUS_PATH.is_file():
        return {
            "polling": False,
            "currently_recording": [],
            "next_scheduled": None,
            "stream_reachable": False,
            "last_tick": None,
            "scheduler_started_at": None,
        }
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "polling": False,
            "currently_recording": [],
            "next_scheduled": None,
            "stream_reachable": False,
            "last_tick": None,
            "scheduler_started_at": None,
        }


def resolve_ffmpeg_path(ffmpeg_path: str) -> str:
    """Best-effort resolved executable path for display (PATH lookup when needed)."""
    t = (ffmpeg_path or "").strip()
    if not t:
        return ""
    p = Path(t)
    if p.is_file():
        return str(p.resolve())
    if not p.is_absolute() and len(p.parts) == 1:
        w = shutil.which(t)
        if w:
            return str(Path(w).resolve())
    return t


def resolve_ffprobe_path(ffmpeg_path: str) -> str:
    """Resolve ffprobe next to ffmpeg, else PATH."""
    resolved = resolve_ffmpeg_path(ffmpeg_path)
    p = Path(resolved)
    if p.is_file():
        name = p.name.lower()
        if name == "ffmpeg.exe":
            sib = p.parent / "ffprobe.exe"
        elif name == "ffmpeg":
            sib = p.parent / "ffprobe"
        else:
            sib = p.parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if sib.is_file():
            return str(sib.resolve())
    w = shutil.which("ffprobe")
    return w or "ffprobe"


def _subprocess_no_window_flags() -> int:
    try:
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        return 0


def _default_stream_preroll_measure_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "target_start": None,
        "message": "",
        "measured_duration_seconds": None,
        "suggested_stream_preroll_seconds": None,
        "updated_at": None,
    }


def read_stream_preroll_measure_state() -> dict[str, Any]:
    out = _default_stream_preroll_measure_state()
    try:
        if STREAM_PREROLL_MEASURE_STATE_PATH.is_file():
            raw = json.loads(STREAM_PREROLL_MEASURE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out.update(raw)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return out


def _atomic_write_stream_preroll_measure_state(payload: dict[str, Any]) -> None:
    STREAM_PREROLL_MEASURE_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = STREAM_PREROLL_MEASURE_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STREAM_PREROLL_MEASURE_STATE_PATH)


def _pick_measure_show_format(cfg: Any) -> ShowFormat:
    for show in cfg.shows:
        if show.enabled:
            return show.format  # type: ignore[return-value]
    return "192mp3"


def _next_full_minute(now: datetime) -> datetime:
    cur = now.replace(second=0, microsecond=0)
    if now >= cur:
        return cur + timedelta(minutes=1)
    return cur


def _probe_duration_seconds(ffprobe_path: str, media_path: Path) -> float:
    r = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        creationflags=_subprocess_no_window_flags(),
    )
    if r.returncode != 0:
        err = (r.stderr or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(err)
    line = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    if not line or line.lower() == "n/a":
        raise RuntimeError("ffprobe returned no duration")
    return float(line)


def _stream_preroll_measure_worker() -> None:
    out_path: Path | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        cfg = load_config(SCHEDULE_PATH)
        fmt = _pick_measure_show_format(cfg)
        ext = ".wav" if fmt == "wav" else ".mp3"
        ffmpeg_path = cfg.ffmpeg_path.strip() or "ffmpeg"
        ffprobe_path = resolve_ffprobe_path(cfg.ffmpeg_path)

        now = datetime.now()
        t0 = _next_full_minute(now)
        _atomic_write_stream_preroll_measure_state(
            {
                **_default_stream_preroll_measure_state(),
                "status": "waiting",
                "target_start": t0.isoformat(timespec="seconds"),
                "message": f"Waiting until {t0.strftime('%H:%M:%S')} to record 60s…",
            }
        )

        delay = (t0 - datetime.now()).total_seconds()
        if delay > 0:
            time.sleep(delay)

        STREAM_PREROLL_MEASURE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = STREAM_PREROLL_MEASURE_DIR / f"preroll_measure_{int(time.time())}{ext}"
        cmd = build_stream_record_cmd(
            ffmpeg_path=ffmpeg_path,
            stream_url=cfg.stream_url,
            show_format=fmt,
            out_path=out_path,
        )
        _atomic_write_stream_preroll_measure_state(
            {
                **read_stream_preroll_measure_state(),
                "status": "recording",
                "message": "Recording 60 seconds of wall-clock time…",
            }
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_subprocess_no_window_flags(),
        )
        record_started = datetime.now()
        end_wall = record_started + timedelta(seconds=60)
        while datetime.now() < end_wall:
            time.sleep(0.25)
            if proc.poll() is not None:
                raise RuntimeError(f"ffmpeg exited early (code {proc.returncode})")

        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        proc = None

        if not out_path.is_file():
            raise RuntimeError("Output file was not created")

        _atomic_write_stream_preroll_measure_state(
            {
                **read_stream_preroll_measure_state(),
                "status": "analyzing",
                "message": "Reading duration with ffprobe…",
            }
        )
        duration = _probe_duration_seconds(ffprobe_path, out_path)
        suggested = max(0, int(round(60.0 - duration)))
        _atomic_write_stream_preroll_measure_state(
            {
                **_default_stream_preroll_measure_state(),
                "status": "done",
                "target_start": t0.isoformat(timespec="seconds"),
                "message": (
                    f"Measured container duration ≈ {duration:.2f}s for a 60s wall-clock capture. "
                    f"Suggested stream preroll: {suggested}s."
                ),
                "measured_duration_seconds": round(duration, 3),
                "suggested_stream_preroll_seconds": suggested,
            }
        )
    except Exception as e:  # noqa: BLE001
        _atomic_write_stream_preroll_measure_state(
            {
                **_default_stream_preroll_measure_state(),
                "status": "error",
                "message": str(e),
            }
        )
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if out_path is not None:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
        _stream_preroll_measure_lock.release()


def format_shows_yaml_section(raw: dict[str, Any]) -> str:
    shows = raw.get("shows")
    if not isinstance(shows, list):
        shows = []
    return yaml.safe_dump(
        {"shows": shows},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def output_root_total_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def is_parts_path(p: Path) -> bool:
    parts = p.parts
    return any(x.endswith(".parts") for x in parts)


def scan_recordings(output_root: Path) -> list[dict[str, Any]]:
    root = output_root.resolve()
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".mp3", ".wav"):
            continue
        if is_parts_path(path):
            continue
        rel = path.relative_to(root)
        show_title = rel.parts[0] if rel.parts else path.stem
        m = FILENAME_RE.match(path.name)
        sort_key: tuple[Any, ...]
        if m:
            date_s, time_s, _ext = m.group(1), m.group(2), m.group(3)
            recorded = f"{date_s} {time_s.replace('-', ':')}"
            try:
                dt = datetime.strptime(f"{date_s} {time_s.replace('-', ':')}", "%Y-%m-%d %H:%M:%S")
                sort_key = (dt, path.stat().st_mtime_ns)
            except ValueError:
                sort_key = (0, path.stat().st_mtime_ns)
                recorded = path.name
        else:
            sort_key = (0, path.stat().st_mtime_ns)
            recorded = path.name
        size = path.stat().st_size
        duration_sec = _audio_duration_seconds(path, size)
        rows.append(
            {
                "show_title": show_title,
                "recorded_label": recorded,
                "sort_key": sort_key,
                "duration_sec": duration_sec,
                "size": size,
                "rel_posix": rel.as_posix(),
            }
        )
    rows.sort(key=lambda r: r["sort_key"], reverse=True)
    return rows


def _audio_duration_seconds(path: Path, file_size: int) -> float | None:
    suf = path.suffix.lower()
    if suf == ".mp3":
        try:
            from mutagen.mp3 import MP3

            audio = MP3(str(path))
            if audio.info is not None and audio.info.length:
                return float(audio.info.length)
        except Exception:
            return None
    if suf == ".wav":
        # PCM 16-bit stereo 44100 Hz (matches recorder)
        bytes_per_sec = 44100 * 2 * 2
        if file_size > 44:
            return max(0.0, (file_size - 44) / bytes_per_sec)
        return 0.0
    return None


def summarize_by_show(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agg: dict[str, list[float]] = {}
    for r in rows:
        st = r["show_title"]
        if st not in agg:
            agg[st] = [0.0, 0.0]
        agg[st][0] += float(r["size"])
        agg[st][1] += 1.0
    out = [
        {"show_title": k, "total_size": int(v[0]), "count": int(v[1])}
        for k, v in sorted(agg.items(), key=lambda x: x[0].lower())
    ]
    out.sort(key=lambda x: x["total_size"], reverse=True)
    return out


def safe_file_under_root(root: Path, rel_posix: str) -> Path | None:
    root = root.resolve()
    if not rel_posix or rel_posix.startswith("/"):
        return None
    candidate = (root / rel_posix).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if is_parts_path(candidate):
        return None
    return candidate


def tail_log_lines(path: Path, n: int = 200) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if len(lines) <= n:
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return "\n".join(lines[-n:]) + "\n"


def _parse_status_instant(iso: str | None) -> datetime | None:
    if not iso or not isinstance(iso, str):
        return None
    s = iso.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _format_hms(total_sec: float) -> str:
    sec = int(max(0.0, float(total_sec)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _format_uptime_human(delta: timedelta) -> str:
    sec = int(max(0, int(delta.total_seconds())))
    if sec < 60:
        return f"{sec}s"
    minutes, s = divmod(sec, 60)
    if sec < 3600:
        return f"{minutes}m {s:02d}s" if s else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if sec < 86400:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {minutes}m"


def build_dashboard_live_context() -> dict[str, Any]:
    cfg = load_cfg()
    status = read_status()
    now = datetime.now()

    stream_ok = bool(status.get("stream_reachable"))

    last_tick_raw = status.get("last_tick")
    tick_display = last_tick_raw if isinstance(last_tick_raw, str) and last_tick_raw.strip() else "—"
    tick_dt = _parse_status_instant(last_tick_raw) if isinstance(last_tick_raw, str) else None
    tick_stale = bool(tick_dt and (now - tick_dt) > timedelta(seconds=DASHBOARD_TICK_STALE_SECONDS))

    recordings: list[dict[str, Any]] = []
    raw_recs = status.get("currently_recording")
    if isinstance(raw_recs, list):
        for rec in raw_recs:
            if not isinstance(rec, dict):
                continue
            sa = rec.get("started_at")
            ea = rec.get("ends_at")
            sdt = _parse_status_instant(sa) if isinstance(sa, str) else None
            edt = _parse_status_instant(ea) if isinstance(ea, str) else None
            title = str(rec.get("title") or "Recording")
            pct = 0.0
            elapsed_hms = "0:00:00"
            remaining_hms = "0:00:00"
            if sdt and edt:
                total_sec = (edt - sdt).total_seconds()
                elapsed_sec = (now - sdt).total_seconds()
                remaining_sec = (edt - now).total_seconds()
                if total_sec <= 0:
                    total_sec = 1.0
                pct = min(100.0, max(0.0, (elapsed_sec / total_sec) * 100.0))
                elapsed_hms = _format_hms(elapsed_sec)
                remaining_hms = _format_hms(remaining_sec)
            try:
                rc = int(rec.get("reconnect_count") or 0)
            except (TypeError, ValueError):
                rc = 0
            recordings.append(
                {
                    "title": title,
                    "pct": round(pct, 1),
                    "elapsed_hms": elapsed_hms,
                    "remaining_hms": remaining_hms,
                    "reconnect_count": rc,
                }
            )

    next_info: dict[str, Any] | None = None
    ns = status.get("next_scheduled")
    if isinstance(ns, dict):
        ts = ns.get("starts_at")
        title = str(ns.get("title") or "")
        starts_str = ts if isinstance(ts, str) else ""
        countdown_hms = "—"
        if isinstance(ts, str) and ts.strip():
            tdt = _parse_status_instant(ts)
            if tdt:
                countdown_hms = _format_hms((tdt - now).total_seconds())
        next_info = {
            "title": title,
            "starts_at": starts_str,
            "countdown_hms": countdown_hms,
        }

    out_root = (APP_ROOT / cfg.output_root).resolve() if not cfg.output_root.is_absolute() else cfg.output_root.resolve()

    stream_url = str(cfg.stream_url or "")
    polling_flag = bool(status.get("polling"))
    # Fresh last_tick means main.py is updating status.json on its scheduler interval.
    main_loop_ok = tick_dt is not None and not tick_stale
    start_cmd = "python main.py"

    started_raw = status.get("scheduler_started_at")
    started_dt = _parse_status_instant(started_raw) if isinstance(started_raw, str) else None
    uptime_display = "—"
    if main_loop_ok and started_dt is not None:
        uptime_display = _format_uptime_human(now - started_dt)

    tick_age_display = "—"
    tick_age_seconds: int | None = None
    if tick_dt is not None:
        tick_age_seconds = int(max(0, (now - tick_dt).total_seconds()))
        if tick_age_seconds < 90:
            tick_age_display = f"{tick_age_seconds}s ago"
        elif tick_age_seconds < 3600:
            tick_age_display = f"{tick_age_seconds // 60}m ago"
        else:
            tick_age_display = f"{tick_age_seconds // 3600}h ago"

    disk_used_gb = "—"
    disk_total_gb = "—"
    disk_fraction = "—"
    disk_used_pct = 0.0
    disk_free_gb = "—"
    try:
        check_path = out_root if out_root.exists() else out_root.parent
        if check_path.exists():
            du = shutil.disk_usage(check_path)
            used = float(du.used)
            total = float(du.total)
            if total > 0:
                disk_used_pct = min(100.0, max(0.0, (used / total) * 100.0))
            u_gb = used / (1024**3)
            t_gb = total / (1024**3)
            disk_used_gb = f"{u_gb:.2f}"
            disk_total_gb = f"{t_gb:.2f}"
            disk_fraction = f"{u_gb:.2f}GB/{t_gb:.2f}GB"
            disk_free_gb = f"{du.free / (1024 ** 3):.2f}"
    except OSError:
        pass

    shows_enabled = sum(1 for s in cfg.shows if s.enabled)
    shows_total = len(cfg.shows)

    return {
        "stream_ok": stream_ok,
        "stream_url": stream_url,
        "polling": polling_flag,
        "main_loop_ok": main_loop_ok,
        "start_command": start_cmd,
        "tick_display": tick_display,
        "tick_stale": tick_stale,
        "tick_age_display": tick_age_display,
        "tick_age_seconds": tick_age_seconds,
        "uptime_display": uptime_display,
        "shows_total": shows_total,
        "shows_enabled": shows_enabled,
        "recordings": recordings,
        "next": next_info,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_fraction": disk_fraction,
        "disk_used_pct": round(disk_used_pct, 2),
        "disk_free_gb": disk_free_gb,
    }


def create_app() -> Flask:
    app = Flask(
        __name__,
        root_path=str(WEB_DIR),
        template_folder="templates",
        static_folder="static",
    )

    try:
        initial = load_cfg()
    except ConfigError as e:
        raise SystemExit(f"Invalid schedule.yaml: {e}") from e
    if initial.web is None:
        raise SystemExit(
            "schedule.yaml must include a 'web' block (username, password, port, secret_key) "
            "to run the management UI. See README."
        )
    app.secret_key = initial.web.secret_key

    @app.context_processor
    def inject_poll_intervals() -> dict[str, Any]:
        return {
            "dashboard_poll_seconds": DASHBOARD_LIVE_POLL_SECONDS,
            "logs_poll_seconds": LOGS_TAIL_POLL_SECONDS,
            "dashboard_tick_stale_seconds": DASHBOARD_TICK_STALE_SECONDS,
        }

    @app.before_request
    def require_login() -> Any:
        if request.endpoint == "login" or request.endpoint == "static":
            return None
        try:
            cfg = load_cfg()
        except ConfigError:
            session.clear()
            flash("schedule.yaml is invalid; fix the file and reload.")
            return redirect(url_for("login"))
        if cfg.web is None:
            session.clear()
            flash("Web credentials missing from schedule.yaml.")
            return redirect(url_for("login"))
        if session.get("user") != cfg.web.username:
            return redirect(url_for("login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "POST":
            try:
                cfg = load_cfg()
            except ConfigError as e:
                flash(f"Config error: {e}")
                return render_template("login.html"), 400
            if cfg.web is None:
                flash("Web credentials not configured.")
                return render_template("login.html"), 400
            user = (request.form.get("username") or "").strip()
            pw = request.form.get("password") or ""
            if user == cfg.web.username and pw == cfg.web.password:
                session["user"] = user
                nxt = (request.form.get("next") or request.args.get("next") or "").strip()
                if not nxt.startswith("/"):
                    nxt = url_for("dashboard")
                return redirect(nxt)
            flash("Invalid username or password.")
        return render_template("login.html")

    @app.post("/logout")
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard() -> str:
        live = build_dashboard_live_context()
        return render_template("dashboard.html", live=live)

    @app.get("/partials/dashboard-live")
    def partial_dashboard_live() -> str:
        live = build_dashboard_live_context()
        return render_template("partials/dashboard_live.html", live=live)

    @app.get("/partials/logs-tail")
    def partial_logs_tail() -> str:
        body = tail_log_lines(LOG_PATH, 200)
        return render_template("partials/logs_tail.html", log_text=body)

    @app.route("/schedule", methods=["GET", "POST"])
    def schedule_page() -> Any:
        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            try:
                raw = read_yaml_document(SCHEDULE_PATH)
            except (OSError, ConfigError) as e:
                flash(f"Could not read schedule: {e}")
                return redirect(url_for("schedule_page"))
            shows = raw.get("shows")
            if not isinstance(shows, list):
                flash("Invalid shows list in YAML.")
                return redirect(url_for("schedule_page"))
            try:
                if action == "delete":
                    idx = int(request.form.get("index", "-1"))
                    if 0 <= idx < len(shows):
                        shows.pop(idx)
                    else:
                        flash("Invalid show index.")
                        return redirect(url_for("schedule_page"))
                elif action in ("add", "edit"):
                    entry = show_dict_from_form(request.form)
                    entry["start"] = normalize_time_hhmmss(str(entry.get("start", "")))
                    entry["end"] = normalize_time_hhmmss(str(entry.get("end", "")))
                    if not entry.get("end_date"):
                        entry.pop("end_date", None)
                    if action == "add":
                        shows.append(entry)
                    else:
                        idx = int(request.form.get("index", "-1"))
                        if 0 <= idx < len(shows):
                            old = shows[idx]
                            if isinstance(old, dict):
                                merged = dict(old)
                                merged.update(entry)
                                if not (request.form.get("end_date") or "").strip():
                                    merged.pop("end_date", None)
                                entry = merged
                            shows[idx] = entry
                        else:
                            flash("Invalid show index.")
                            return redirect(url_for("schedule_page"))
                else:
                    flash("Unknown action.")
                    return redirect(url_for("schedule_page"))
                raw["shows"] = shows
                validate_schedule_document(raw)
                atomic_write_yaml(SCHEDULE_PATH, raw)
                flash("Schedule saved.")
            except ConfigError as e:
                flash(f"Validation error: {e}")
            except (ValueError, OSError, KeyError) as e:
                flash(f"Save failed: {e}")
            return redirect(url_for("schedule_page"))

        cfg = load_cfg()
        raw_doc = read_yaml_document(SCHEDULE_PATH)
        shows_yaml = format_shows_yaml_section(raw_doc)
        shows_by_day: list[list[Any]] = [[] for _ in range(7)]
        for show in cfg.shows:
            if 0 <= show.day <= 6:
                shows_by_day[show.day].append(show)
        return render_template(
            "schedule.html",
            shows_by_day=shows_by_day,
            weekday_names=WEEKDAY_NAMES,
            shows_list=list(cfg.shows),
            shows_yaml=shows_yaml,
        )

    @app.get("/history")
    def history_page() -> str:
        cfg = load_cfg()
        out_root = cfg.output_root
        if not out_root.is_absolute():
            out_root = (APP_ROOT / out_root).resolve()
        else:
            out_root = out_root.resolve()
        rows = scan_recordings(out_root)
        summary = summarize_by_show(rows)
        return render_template(
            "history.html",
            rows=rows,
            summary=summary,
        )

    @app.get("/logs")
    def logs_page() -> str:
        log_text = tail_log_lines(LOG_PATH, 200)
        return render_template("logs.html", log_text=log_text)

    @app.get("/settings")
    def settings_page() -> str:
        cfg = load_cfg()
        return render_template(
            "settings.html",
            cfg=cfg,
            ffmpeg_resolved=resolve_ffmpeg_path(cfg.ffmpeg_path),
            ffprobe_resolved=resolve_ffprobe_path(cfg.ffmpeg_path),
            preroll_measure=read_stream_preroll_measure_state(),
        )

    @app.post("/settings/save")
    def settings_save() -> Any:
        try:
            raw = read_yaml_document(SCHEDULE_PATH)
        except (OSError, ConfigError) as e:
            flash(f"Could not read schedule: {e}")
            return redirect(url_for("settings_page"))
        web_raw = raw.get("web")
        if not isinstance(web_raw, dict):
            flash("Invalid web section.")
            return redirect(url_for("settings_page"))
        raw["stream_url"] = (request.form.get("stream_url") or "").strip()
        raw["output_root"] = (request.form.get("output_root") or "").strip()
        raw["ffmpeg_path"] = (request.form.get("ffmpeg_path") or "").strip()
        try:
            pr = int((request.form.get("stream_preroll_seconds") or "0").strip())
        except ValueError:
            flash("Stream preroll must be an integer.")
            return redirect(url_for("settings_page"))
        if not (0 <= pr <= 600):
            flash("Stream preroll must be between 0 and 600 seconds.")
            return redirect(url_for("settings_page"))
        raw["stream_preroll_seconds"] = pr
        web_raw["username"] = (request.form.get("web_username") or "").strip()
        web_raw["password"] = (request.form.get("web_password") or "").strip()
        try:
            web_raw["port"] = int(request.form.get("web_port") or "8080")
        except ValueError:
            flash("Port must be an integer.")
            return redirect(url_for("settings_page"))
        raw["web"] = web_raw
        try:
            validate_schedule_document(raw)
            atomic_write_yaml(SCHEDULE_PATH, raw)
            flash("Settings saved.")
        except ConfigError as e:
            flash(f"Validation error: {e}")
        except OSError as e:
            flash(f"Save failed: {e}")
        return redirect(url_for("settings_page"))

    @app.post("/settings/measure-stream-preroll")
    def settings_measure_stream_preroll() -> Any:
        if not _stream_preroll_measure_lock.acquire(blocking=False):
            flash("A stream preroll measurement is already in progress.")
            return redirect(url_for("settings_page"))
        threading.Thread(target=_stream_preroll_measure_worker, daemon=True).start()
        flash(
            "Measurement scheduled for the next full minute (~2 minutes total). "
            "Keep the web server running; status updates below."
        )
        return redirect(url_for("settings_page"))

    @app.get("/api/stream-preroll-measure")
    def api_stream_preroll_measure() -> Any:
        data = read_stream_preroll_measure_state()
        return Response(json.dumps(data), mimetype="application/json")

    @app.get("/files/<path:rel_posix>")
    def serve_file(rel_posix: str) -> Any:
        cfg = load_cfg()
        out_root = cfg.output_root
        if not out_root.is_absolute():
            out_root = (APP_ROOT / out_root).resolve()
        else:
            out_root = out_root.resolve()
        path = safe_file_under_root(out_root, rel_posix)
        if path is None:
            abort(404)
        dl = request.args.get("dl")
        mimetype = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
        return send_file(
            path,
            mimetype=mimetype,
            as_attachment=dl == "1",
            download_name=path.name,
        )

    @app.get("/api/status")
    def api_status() -> Any:
        data = read_status()
        return Response(json.dumps(data), mimetype="application/json")

    @app.get("/api/logs")
    def api_logs() -> Any:
        body = tail_log_lines(LOG_PATH, 200)
        return Response(body, mimetype="text/plain; charset=utf-8")

    return app


app = create_app()


if __name__ == "__main__":
    cfg = load_cfg()
    if cfg.web is None:
        raise SystemExit("Missing web section in schedule.yaml")
    app.run(host="0.0.0.0", port=cfg.web.port, debug=False)
