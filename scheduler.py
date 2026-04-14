from __future__ import annotations

import json
import logging
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from config import AppConfig, ConfigError, Show, load_config
from recorder import Recorder, RecordingSession

# Upper bound between ticks: status.json, schedule.yaml mtime, ffmpeg health checks.
SCHEDULER_MAX_SLEEP_SECONDS = 30.0
# Avoid a busy loop when the next event is immediate or clocks align at the boundary.
SCHEDULER_MIN_SLEEP_SECONDS = 0.05


@dataclass(frozen=True)
class ShowKey:
    title: str
    day: int
    start: str
    end: str


class Scheduler:
    def __init__(self, *, config_path: Path, logger: logging.Logger):
        self._config_path = config_path
        self._log = logger

        self._config: AppConfig | None = None
        self._config_mtime: float | None = None

        self._recorder: Recorder | None = None
        self._active: dict[ShowKey, tuple[RecordingSession, datetime]] = {}
        self._scheduler_started_at: datetime | None = None

    def load_initial_config(self) -> None:
        cfg = load_config(self._config_path)
        self._config = cfg
        self._config_mtime = self._config_path.stat().st_mtime
        self._recorder = Recorder(cfg.ffmpeg_path, self._log)

        enabled_count = sum(1 for s in cfg.shows if s.enabled)
        self._log.info(
            "Loaded schedule: %s shows (%s enabled), stream_preroll_seconds=%s",
            len(cfg.shows),
            enabled_count,
            cfg.stream_preroll_seconds,
        )

    def run_forever(self) -> None:
        if self._config is None or self._recorder is None:
            self.load_initial_config()

        self._scheduler_started_at = datetime.now()
        self._log.info(
            "Scheduler loop started (adaptive sleep, max %.0fs between ticks)",
            SCHEDULER_MAX_SLEEP_SECONDS,
        )
        while True:
            self.tick()
            cfg = self._config
            if cfg is None:
                time_mod.sleep(SCHEDULER_MAX_SLEEP_SECONDS)
                continue
            delay = self._compute_sleep_seconds(cfg, datetime.now())
            time_mod.sleep(delay)

    def tick(self) -> None:
        self._reload_if_changed()
        now = datetime.now()
        cfg = self._config
        recorder = self._recorder
        if cfg is None or recorder is None:
            return

        # First, manage already-active sessions (reconnect checks and scheduled stop).
        for key, (session, end_dt) in list(self._active.items()):
            if now >= end_dt:
                recorder.stop(session)
                self._active.pop(key, None)
                continue
            recorder.tick(session, now=now, stream_url=cfg.stream_url, end_dt=end_dt)

        # Then, start any shows that should be active but aren't.
        for show in cfg.shows:
            if not show.enabled:
                continue
            if show.day != now.weekday():
                continue
            if show.end_date is not None and now.date() >= show.end_date:
                continue

            start_dt = datetime.combine(now.date(), show.start)
            end_dt = datetime.combine(now.date(), show.end)
            effective_start = start_dt - timedelta(seconds=cfg.stream_preroll_seconds)
            if not (effective_start <= now < end_dt):
                continue

            key = _show_key(show)
            if key in self._active:
                continue

            if cfg.stream_preroll_seconds > 0 and now > start_dt:
                self._log.warning(
                    "Recording %s started after nominal start %s (now=%s); preroll=%ss cannot apply retroactively. "
                    "Save schedule.yaml before effective_start (%s) so the scheduler can wake in time.",
                    show.title,
                    start_dt.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                    cfg.stream_preroll_seconds,
                    effective_start.isoformat(timespec="seconds"),
                )
            else:
                self._log.info(
                    "Recording %s: nominal_start=%s effective_start=%s preroll=%ss now=%s",
                    show.title,
                    start_dt.isoformat(timespec="seconds"),
                    effective_start.isoformat(timespec="seconds"),
                    cfg.stream_preroll_seconds,
                    now.isoformat(timespec="seconds"),
                )

            session = recorder.start(show, cfg.output_root, started_at=now, stream_url=cfg.stream_url)
            self._active[key] = (session, end_dt)

        self._write_status_json(cfg=cfg, now=now)

    def shutdown(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        for key, (session, _end_dt) in list(self._active.items()):
            try:
                recorder.stop(session)
            finally:
                self._active.pop(key, None)

    def _reload_if_changed(self) -> None:
        try:
            mtime = self._config_path.stat().st_mtime
        except FileNotFoundError:
            self._log.error("Config file missing: %s", self._config_path)
            return

        if self._config_mtime is not None and mtime <= self._config_mtime:
            return

        try:
            new_cfg = load_config(self._config_path)
        except ConfigError as e:
            # Keep last known good config; allow editing YAML without killing process.
            self._log.error("Config reload failed; keeping previous config: %s", e)
            self._config_mtime = mtime
            return

        self._config = new_cfg
        self._config_mtime = mtime
        self._recorder = Recorder(new_cfg.ffmpeg_path, self._log)

        enabled_count = sum(1 for s in new_cfg.shows if s.enabled)
        self._log.info(
            "Reloaded schedule: %s shows (%s enabled), stream_preroll_seconds=%s",
            len(new_cfg.shows),
            enabled_count,
            new_cfg.stream_preroll_seconds,
        )

    def _compute_sleep_seconds(self, cfg: AppConfig, now: datetime) -> float:
        """Sleep until the next schedule boundary, capped for ffmpeg/config/status freshness."""
        candidates: list[datetime] = []

        for _key, (_session, end_dt) in self._active.items():
            if end_dt > now:
                candidates.append(end_dt)

        for show in cfg.shows:
            if not show.enabled:
                continue
            if show.day != now.weekday():
                continue
            if show.end_date is not None and now.date() >= show.end_date:
                continue
            start_dt = datetime.combine(now.date(), show.start)
            end_dt = datetime.combine(now.date(), show.end)
            effective_start = start_dt - timedelta(seconds=cfg.stream_preroll_seconds)
            if effective_start <= now < end_dt and _show_key(show) not in self._active:
                candidates.append(now)

        preroll = cfg.stream_preroll_seconds
        for show in cfg.shows:
            if not show.enabled:
                continue
            next_start = _next_occurrence_start(show, now=now)
            if next_start is not None and next_start > now:
                effective = next_start - timedelta(seconds=preroll)
                if effective > now:
                    candidates.append(effective)
                elif effective <= now < next_start:
                    candidates.append(now)

        if not candidates:
            return SCHEDULER_MAX_SLEEP_SECONDS

        next_wake = min(candidates)
        raw = (next_wake - now).total_seconds()
        return min(
            SCHEDULER_MAX_SLEEP_SECONDS,
            max(SCHEDULER_MIN_SLEEP_SECONDS, raw),
        )

    def _write_status_json(self, *, cfg: AppConfig, now: datetime) -> None:
        status_path = self._config_path.parent / "status.json"
        tmp_path = self._config_path.parent / "status.json.tmp"
        stream_ok = _head_stream_reachable(cfg.stream_url)
        currently: list[dict[str, Any]] = []
        for _key, (session, end_dt) in self._active.items():
            reconnect_count = max(0, session.next_part_index - 2)
            currently.append(
                {
                    "title": session.show.title,
                    "started_at": session.started_at.isoformat(timespec="seconds"),
                    "ends_at": end_dt.isoformat(timespec="seconds"),
                    "output_file": str(session.final_path.resolve()),
                    "reconnect_count": reconnect_count,
                }
            )
        next_scheduled = _compute_next_scheduled(cfg.shows, now=now)
        started_raw = (
            self._scheduler_started_at.isoformat(timespec="seconds")
            if self._scheduler_started_at is not None
            else None
        )
        payload: dict[str, Any] = {
            "polling": True,
            "currently_recording": currently,
            "next_scheduled": next_scheduled,
            "stream_reachable": stream_ok,
            "last_tick": now.isoformat(timespec="seconds"),
            "scheduler_started_at": started_raw,
        }
        try:
            data = json.dumps(payload, indent=2)
            tmp_path.write_text(data + "\n", encoding="utf-8")
            tmp_path.replace(status_path)
        except OSError as e:
            self._log.warning("Failed to write status.json: %s", e)


def _head_stream_reachable(url: str, *, timeout: float = 5.0) -> bool:
    req = Request(url, method="HEAD", headers={"User-Agent": "radioarchive/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL is operator-controlled config
            code = getattr(resp, "status", None) or resp.getcode()
            return isinstance(code, int) and 200 <= code < 400
    except (URLError, OSError, TimeoutError, ValueError):
        return False


def _compute_next_scheduled(shows: tuple[Show, ...], *, now: datetime) -> dict[str, Any] | None:
    best: datetime | None = None
    best_title: str | None = None
    for show in shows:
        if not show.enabled:
            continue
        start = _next_occurrence_start(show, now=now)
        if start is None:
            continue
        if best is None or start < best:
            best = start
            best_title = show.title
    if best is None or best_title is None:
        return None
    return {"title": best_title, "starts_at": best.isoformat(timespec="seconds")}


def _next_occurrence_start(show: Show, *, now: datetime) -> datetime | None:
    if show.end_date is not None and now.date() >= show.end_date:
        return None
    delta_days = (show.day - now.weekday()) % 7
    day = now.date() + timedelta(days=delta_days)
    start_dt = datetime.combine(day, show.start)
    if start_dt <= now:
        start_dt += timedelta(days=7)
    max_iterations = 54  # ~1 year of weekly slots
    for _ in range(max_iterations):
        if show.end_date is None or start_dt.date() < show.end_date:
            return start_dt
        start_dt += timedelta(days=7)
    return None


def _show_key(show: Show) -> ShowKey:
    return ShowKey(
        title=show.title.strip().lower(),
        day=show.day,
        start=show.start.strftime("%H:%M:%S"),
        end=show.end.strftime("%H:%M:%S"),
    )

