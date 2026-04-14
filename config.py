from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal

import yaml

ShowFormat = Literal["192mp3", "320mp3", "wav"]


class ConfigError(Exception):
    pass


_DAY_TO_WEEKDAY = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Index 0 = Monday .. 6 = Sunday (matches datetime.weekday())
WEEKDAY_NAMES: tuple[str, ...] = tuple(d.capitalize() for d in _DAY_TO_WEEKDAY.keys())

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class Show:
    title: str
    day: int  # 0=Mon .. 6=Sun
    start: time
    end: time
    format: ShowFormat
    enabled: bool = True
    end_date: date | None = None
    stream_url: str | None = None  # accepted for future use; not implemented

    @property
    def extension(self) -> str:
        return ".wav" if self.format == "wav" else ".mp3"


@dataclass(frozen=True)
class WebSettings:
    username: str
    password: str
    port: int
    secret_key: str


@dataclass(frozen=True)
class AppConfig:
    stream_url: str
    output_root: Path
    ffmpeg_path: str = "ffmpeg"
    stream_preroll_seconds: int = 0
    shows: tuple[Show, ...] = ()
    web: WebSettings | None = None


def load_config(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except Exception as e:  # noqa: BLE001 - surface YAML parsing errors clearly
        raise ConfigError(f"Failed to parse YAML: {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("YAML root must be a mapping/object.")

    stream_url = _require_str(raw, "stream_url")
    output_root_str = _require_str(raw, "output_root")
    ffmpeg_path = _optional_str(raw, "ffmpeg_path", default="ffmpeg")
    stream_preroll_seconds = _optional_int(
        raw,
        "stream_preroll_seconds",
        default=0,
        min_value=0,
        max_value=600,
    )
    shows_raw = raw.get("shows")
    if not isinstance(shows_raw, list):
        raise ConfigError("Field 'shows' must be a list.")

    shows: list[Show] = []
    for idx, item in enumerate(shows_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"shows[{idx}] must be a mapping/object.")
        shows.append(_parse_show(item, idx))

    web = _parse_web(raw.get("web"))

    return AppConfig(
        stream_url=stream_url,
        output_root=Path(output_root_str),
        ffmpeg_path=ffmpeg_path,
        stream_preroll_seconds=stream_preroll_seconds,
        shows=tuple(shows),
        web=web,
    )


def sanitize_title(title: str) -> str:
    # Safe on Windows + Linux: avoid invalid filename chars and Windows quirks.
    invalid = set('<>:"/\\|?*')
    cleaned_chars: list[str] = []
    for ch in title:
        code = ord(ch)
        if ch in invalid or code < 32:
            cleaned_chars.append("_")
        else:
            cleaned_chars.append(ch)

    cleaned = "".join(cleaned_chars).strip()
    # collapse runs of whitespace
    cleaned = " ".join(cleaned.split())
    # Windows: no trailing dots or spaces
    cleaned = cleaned.rstrip(". ").strip()
    if not cleaned:
        cleaned = "untitled"
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _parse_show(item: dict[str, Any], idx: int) -> Show:
    title = _require_str(item, "title", prefix=f"shows[{idx}].")
    day_str = _require_str(item, "day", prefix=f"shows[{idx}].")
    day = _parse_day(day_str, prefix=f"shows[{idx}].day")
    start = _parse_time(_require_str(item, "start", prefix=f"shows[{idx}]."), prefix=f"shows[{idx}].start")
    end = _parse_time(_require_str(item, "end", prefix=f"shows[{idx}]."), prefix=f"shows[{idx}].end")
    if datetime.combine(date.today(), end) <= datetime.combine(date.today(), start):
        raise ConfigError(
            f"shows[{idx}]: 'end' must be after 'start' (cross-midnight windows are not supported)."
        )

    fmt = _require_str(item, "format", prefix=f"shows[{idx}].")
    if fmt not in ("192mp3", "320mp3", "wav"):
        raise ConfigError(
            f"shows[{idx}].format must be one of: 192mp3, 320mp3, wav (got {fmt!r})."
        )

    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"shows[{idx}].enabled must be a boolean.")

    end_date_raw = item.get("end_date")
    end_date = None
    if end_date_raw is not None:
        if not isinstance(end_date_raw, str):
            raise ConfigError(f"shows[{idx}].end_date must be a string YYYY-MM-DD.")
        end_date = _parse_date(end_date_raw, prefix=f"shows[{idx}].end_date")

    # Accepted for future support; not used yet.
    stream_url = item.get("stream_url")
    if stream_url is not None and not isinstance(stream_url, str):
        raise ConfigError(f"shows[{idx}].stream_url must be a string URL.")

    return Show(
        title=title,
        day=day,
        start=start,
        end=end,
        format=fmt,  # type: ignore[arg-type]
        enabled=enabled,
        end_date=end_date,
        stream_url=stream_url,
    )


def _require_str(obj: dict[str, Any], key: str, prefix: str = "") -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"{prefix}{key} is required and must be a non-empty string.")
    return val.strip()


def _optional_int(
    obj: dict[str, Any],
    key: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
    prefix: str = "",
) -> int:
    if key not in obj or obj[key] is None or obj[key] == "":
        return default
    val = obj[key]
    if isinstance(val, bool):
        raise ConfigError(f"{prefix}{key} must be an integer.")
    if isinstance(val, str):
        try:
            val = int(val.strip(), 10)
        except ValueError as e:
            raise ConfigError(f"{prefix}{key} must be an integer.") from e
    elif isinstance(val, int):
        pass
    elif isinstance(val, float):
        if not val.is_integer():
            raise ConfigError(f"{prefix}{key} must be a whole number.")
        val = int(val)
    else:
        raise ConfigError(f"{prefix}{key} must be an integer.")
    if not (min_value <= val <= max_value):
        raise ConfigError(f"{prefix}{key} must be between {min_value} and {max_value} (got {val}).")
    return val


def _optional_str(obj: dict[str, Any], key: str, default: str, prefix: str = "") -> str:
    val = obj.get(key, default)
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"{prefix}{key} must be a non-empty string.")
    return val.strip()


def _parse_day(day: str, prefix: str) -> int:
    d = day.strip().lower()
    if d not in _DAY_TO_WEEKDAY:
        allowed = ", ".join(k.capitalize() for k in _DAY_TO_WEEKDAY.keys())
        raise ConfigError(f"{prefix}: invalid day {day!r}. Must be one of: {allowed}.")
    return _DAY_TO_WEEKDAY[d]


def _parse_time(value: str, prefix: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M:%S").time()
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"{prefix}: invalid time {value!r}. Expected HH:MM:SS.") from e


def _parse_date(value: str, prefix: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"{prefix}: invalid date {value!r}. Expected YYYY-MM-DD.") from e


def _parse_web(raw: Any) -> WebSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("Field 'web' must be a mapping/object.")
    username = _require_str(raw, "username", prefix="web.")
    password = _require_str(raw, "password", prefix="web.")
    secret_key = _require_str(raw, "secret_key", prefix="web.")
    port_raw = raw.get("port", 8080)
    if isinstance(port_raw, bool):
        raise ConfigError("web.port must be an integer.")
    if isinstance(port_raw, str):
        try:
            port_val = int(port_raw.strip())
        except ValueError as e:
            raise ConfigError("web.port must be an integer.") from e
    elif isinstance(port_raw, int):
        port_val = port_raw
    else:
        raise ConfigError("web.port must be an integer.")
    if not (1 <= port_val <= 65535):
        raise ConfigError("web.port must be between 1 and 65535.")
    return WebSettings(username=username, password=password, port=port_val, secret_key=secret_key)
