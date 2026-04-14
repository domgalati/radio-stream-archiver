from __future__ import annotations

import shutil
import subprocess
import time as time_mod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from config import Show, ShowFormat, sanitize_title


def build_stream_record_cmd(*, ffmpeg_path: str, stream_url: str, show_format: ShowFormat, out_path: Path) -> list[str]:
    base = [ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-i", stream_url]

    if show_format == "192mp3":
        audio = ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
    elif show_format == "320mp3":
        audio = ["-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100", "-ac", "2"]
    elif show_format == "wav":
        audio = ["-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2"]
    else:
        raise ValueError(f"Unsupported format: {show_format!r}")

    return [*base, *audio, str(out_path)]


@dataclass
class RecordingSession:
    show: Show
    final_path: Path
    parts_dir: Path
    started_at: datetime
    ffmpeg_process: subprocess.Popen[bytes] | None = None
    next_part_index: int = 1
    stopping: bool = False

    def is_running(self) -> bool:
        return self.ffmpeg_process is not None and self.ffmpeg_process.poll() is None


class Recorder:
    def __init__(self, ffmpeg_path: str, logger):
        self._ffmpeg_path = ffmpeg_path
        self._log = logger

    def start(self, show: Show, output_root: Path, started_at: datetime, stream_url: str) -> RecordingSession:
        title_dir = output_root / sanitize_title(show.title)
        title_dir.mkdir(parents=True, exist_ok=True)

        filename = started_at.strftime("%Y-%m-%d_%H-%M-%S") + show.extension
        final_path = title_dir / filename
        parts_dir = final_path.with_suffix(final_path.suffix + ".parts")
        parts_dir.mkdir(parents=True, exist_ok=True)

        session = RecordingSession(show=show, final_path=final_path, parts_dir=parts_dir, started_at=started_at)
        self._log.info("Recording start: %s -> %s", show.title, final_path)

        self._start_next_part(session, stream_url=stream_url)
        return session

    def stop(self, session: RecordingSession) -> None:
        session.stopping = True
        self._terminate_ffmpeg(session)
        self._stitch_parts(session)
        self._log.info("Recording stop: %s -> %s", session.show.title, session.final_path)

    def tick(self, session: RecordingSession, *, now: datetime, stream_url: str, end_dt: datetime) -> None:
        if session.stopping:
            return
        if now >= end_dt:
            return
        if session.is_running():
            return

        # ffmpeg exited early; reconnect.
        self._log.warning("ffmpeg exited early for %s; reconnecting in 5s", session.show.title)
        time_mod.sleep(5)
        if datetime.now() >= end_dt:
            return
        self._start_next_part(session, stream_url=stream_url)

    def _terminate_ffmpeg(self, session: RecordingSession) -> None:
        proc = session.ffmpeg_process
        if proc is None:
            return
        if proc.poll() is not None:
            return

        self._log.info("Stopping ffmpeg for %s", session.show.title)
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._log.warning("ffmpeg did not exit; killing for %s", session.show.title)
                proc.kill()
                proc.wait(timeout=10)
        except Exception as e:  # noqa: BLE001
            self._log.error("Error stopping ffmpeg for %s: %s", session.show.title, e)
        finally:
            session.ffmpeg_process = None

    def _start_next_part(self, session: RecordingSession, *, stream_url: str) -> None:
        part_name = f"part{session.next_part_index:04d}{session.show.extension}"
        part_path = session.parts_dir / part_name
        session.next_part_index += 1

        cmd = build_stream_record_cmd(
            ffmpeg_path=self._ffmpeg_path,
            stream_url=stream_url,
            show_format=session.show.format,
            out_path=part_path,
        )
        self._log.info("Starting ffmpeg for %s (%s)", session.show.title, part_name)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=_windows_creation_flags(),
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"ffmpeg not found. Install ffmpeg and ensure it's on PATH, or set ffmpeg_path in schedule.yaml "
                f"(got {self._ffmpeg_path!r})."
            ) from e

        session.ffmpeg_process = proc

    def _stitch_parts(self, session: RecordingSession) -> None:
        parts = sorted(session.parts_dir.glob(f"part*{session.show.extension}"))
        if not parts:
            self._log.warning("No parts found for %s; nothing to stitch.", session.show.title)
            return

        if len(parts) == 1:
            tmp_final = session.final_path.with_suffix(session.final_path.suffix + ".tmp")
            shutil.copyfile(parts[0], tmp_final)
            tmp_final.replace(session.final_path)
            self._cleanup_parts(session)
            return

        concat_txt = session.parts_dir / "concat.txt"
        # Use relative filenames so ffmpeg concat parsing stays simple cross-platform.
        concat_lines = [f"file '{p.name}'\n" for p in parts]
        concat_txt.write_text("".join(concat_lines), encoding="utf-8")

        tmp_out = session.final_path.with_suffix(session.final_path.suffix + ".tmp")
        tmp_out.parent.mkdir(parents=True, exist_ok=True)

        # First attempt: stream copy via concat demuxer.
        cmd = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_txt),
            "-c",
            "copy",
            str(tmp_out),
        ]

        ok = self._run_ffmpeg(cmd, cwd=session.parts_dir, title=session.show.title, context="stitch-copy")
        if not ok and session.show.format == "wav":
            # Fallback for WAV: concat filter re-encode to PCM.
            filter_cmd = [
                self._ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_txt),
                "-c:a",
                "pcm_s16le",
                str(tmp_out),
            ]
            ok = self._run_ffmpeg(filter_cmd, cwd=session.parts_dir, title=session.show.title, context="stitch-wav-reencode")

        if not ok:
            self._log.error("Failed to stitch parts for %s. Keeping parts at %s", session.show.title, session.parts_dir)
            return

        tmp_out.replace(session.final_path)
        self._cleanup_parts(session)

    def _run_ffmpeg(self, cmd: list[str], *, cwd: Path, title: str, context: str) -> bool:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                check=False,
                creationflags=_windows_creation_flags(),
            )
        except FileNotFoundError:
            self._log.error("ffmpeg not found while stitching for %s", title)
            return False
        except Exception as e:  # noqa: BLE001
            self._log.error("ffmpeg error (%s) for %s: %s", context, title, e)
            return False

        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            if stderr:
                self._log.error("ffmpeg failed (%s) for %s: %s", context, title, stderr)
            else:
                self._log.error("ffmpeg failed (%s) for %s with code %s", context, title, proc.returncode)
            return False

        return True

    def _cleanup_parts(self, session: RecordingSession) -> None:
        try:
            shutil.rmtree(session.parts_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            # Not fatal; keep going.
            pass


def _windows_creation_flags() -> int:
    # Avoid creating a console window on Windows when running as a background process.
    try:
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        return 0


# Future requirement stub:
# Local-source recording mode (capturing audio from the machine running Station Playlist Studio)
# would be implemented here, e.g., by choosing an OS-specific ffmpeg input device (WASAPI/ALSA)
# and building a different input graph. Not implemented in this version.
