"""Configuration layer.

All runtime configuration is derived from environment variables exactly once,
at start-up, and is then passed explicitly down the object graph.  No module
other than this one is allowed to read ``os.environ``: that keeps every other
component trivially testable and free of hidden global state.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = ["Config", "ConfigError", "StreamConfig", "VideoConfig", "redact_rtmp_url"]

#: Repository root, used to resolve bundled assets.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
)


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable configuration."""


def _get_str(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _get_str(env, key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get_str(env, key, "true" if default else "false").lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise ConfigError(f"{key} must be a boolean flag, got {raw!r}")


@dataclass(frozen=True, slots=True)
class VideoConfig:
    """Geometry and timing of the rendered video signal."""

    width: int = 1920
    height: int = 1080
    fps: int = 30

    def __post_init__(self) -> None:
        if self.width % 2 or self.height % 2:
            raise ConfigError("WIDTH and HEIGHT must be even numbers (H.264 requirement)")

    @property
    def size(self) -> tuple[int, int]:
        """Frame size as a ``(width, height)`` tuple."""
        return self.width, self.height

    @property
    def frame_interval(self) -> float:
        """Wall-clock seconds between two consecutive frames."""
        return 1.0 / float(self.fps)

    @property
    def frame_bytes(self) -> int:
        """Size of a single RGB24 frame in bytes."""
        return self.width * self.height * 3


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """Everything the :class:`~crane_fids.stream_engine.StreamEngine` needs."""

    rtmp_url: str
    video_bitrate: str = "4500k"
    video_maxrate: str = "4500k"
    video_bufsize: str = "9000k"
    preset: str = "veryfast"
    gop_seconds: int = 2
    audio_enabled: bool = True
    ffmpeg_binary: str = "ffmpeg"
    ffmpeg_loglevel: str = "warning"
    restart_backoff_initial: float = 2.0
    restart_backoff_max: float = 60.0

    @property
    def enabled(self) -> bool:
        """``False`` in dry-run mode, when no RTMP target was supplied."""
        return bool(self.rtmp_url)


@dataclass(frozen=True, slots=True)
class Config:
    """Root configuration object.

    Instances are immutable and are created through :meth:`from_env`.
    """

    airport_name: str
    airline_name: str
    board_title: str
    ticker_text: str
    timezone_label: str
    log_level: str
    video: VideoConfig
    stream: StreamConfig
    flight_refresh_seconds: int
    rows_per_page: int
    page_seconds: int
    font_dir: Path = field(default=PROJECT_ROOT / "assets" / "fonts")

    # ------------------------------------------------------------------ #
    # Factory
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a configuration from ``env`` (defaults to ``os.environ``).

        Raises:
            ConfigError: if any variable is present but malformed.
        """
        env = os.environ if env is None else env

        log_level = _get_str(env, "LOG_LEVEL", "INFO").upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}"
            )

        video = VideoConfig(
            width=_get_int(env, "WIDTH", 1920, minimum=640, maximum=3840),
            height=_get_int(env, "HEIGHT", 1080, minimum=360, maximum=2160),
            fps=_get_int(env, "FPS", 30, minimum=1, maximum=60),
        )

        airport_name = _get_str(env, "AIRPORT_NAME", "CRANE INTERNATIONAL AIRPORT")

        stream = StreamConfig(
            rtmp_url=_get_str(env, "RTMP_URL", ""),
            video_bitrate=_get_str(env, "VIDEO_BITRATE", "4500k"),
            video_maxrate=_get_str(env, "VIDEO_MAXRATE", "4500k"),
            video_bufsize=_get_str(env, "VIDEO_BUFSIZE", "9000k"),
            preset=_get_str(env, "FFMPEG_PRESET", "veryfast"),
            gop_seconds=_get_int(env, "GOP_SECONDS", 2, minimum=1, maximum=10),
            audio_enabled=_get_bool(env, "AUDIO_ENABLED", True),
            ffmpeg_binary=_get_str(env, "FFMPEG_BINARY", "ffmpeg"),
            ffmpeg_loglevel=_get_str(env, "FFMPEG_LOGLEVEL", "warning"),
            restart_backoff_initial=float(
                _get_int(env, "RESTART_BACKOFF", 2, minimum=1, maximum=30)
            ),
            restart_backoff_max=float(
                _get_int(env, "RESTART_BACKOFF_MAX", 60, minimum=5, maximum=600)
            ),
        )

        default_ticker = (
            f"WELCOME TO {airport_name}  •  CRANE AIRLINES WISHES YOU A PLEASANT FLIGHT  "
            "·  PLEASE KEEP YOUR BAGGAGE WITH YOU AT ALL TIMES  "
            "·  BOARDING GATES CLOSE 15 MINUTES BEFORE DEPARTURE  "
        )

        return cls(
            airport_name=airport_name,
            airline_name=_get_str(env, "AIRLINE_NAME", "CRANE AIRLINES"),
            board_title=_get_str(env, "BOARD_TITLE", "DEPARTURES"),
            ticker_text=_get_str(env, "TICKER_TEXT", default_ticker),
            timezone_label=_get_str(env, "TIMEZONE_LABEL", "LOCAL TIME"),
            log_level=log_level,
            video=video,
            stream=stream,
            flight_refresh_seconds=_get_int(
                env, "FLIGHT_REFRESH_SECONDS", 30, minimum=1, maximum=3600
            ),
            rows_per_page=_get_int(env, "ROWS_PER_PAGE", 10, minimum=3, maximum=20),
            page_seconds=_get_int(env, "PAGE_SECONDS", 15, minimum=3, maximum=300),
            font_dir=Path(_get_str(env, "FONT_DIR", str(PROJECT_ROOT / "assets" / "fonts"))),
        )

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #
    def describe(self) -> str:
        """Human readable, secret-free summary for the start-up log."""
        target = (
            "dry-run (no RTMP_URL)"
            if not self.stream.enabled
            else redact_rtmp_url(self.stream.rtmp_url)
        )
        return (
            f"airport={self.airport_name!r} video={self.video.width}x{self.video.height}"
            f"@{self.video.fps}fps rows_per_page={self.rows_per_page} "
            f"page_seconds={self.page_seconds} target={target}"
        )


def redact_rtmp_url(rtmp_url: str) -> str:
    """Hide the stream key, which is a credential, from log output."""
    head, separator, _ = rtmp_url.rpartition("/")
    if not separator:
        return "rtmp://<redacted>"
    return f"{head}/<stream-key-redacted>"
