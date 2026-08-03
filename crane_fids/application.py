"""Application orchestration.

:class:`Application` is the composition root's runtime counterpart: it owns the
frame clock and wires the three collaborators together, but implements none of
their concerns.

    FlightManager  ->  FrameContext  ->  FidsRenderer  ->  StreamEngine

Resilience contract: the loop is the last line of defence.  No exception raised
by a collaborator may terminate the process; everything is logged, paced and
retried, because the only real failure mode of a 24x7 broadcast is going dark.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import FrameType

from .config import Config
from .dynamic_provider import DynamicFlightProvider
from .flight_manager import FlightManager
from .models import BoardKind
from .renderer import FidsRenderer, FrameContext
from .stream_engine import (
    FFmpegStreamEngine,
    NullStreamEngine,
    StreamEngine,
    StreamEngineError,
)

__all__ = ["Application", "LoopStats", "build_application"]

_LOG = logging.getLogger(__name__)

#: How often the loop reports its health, in seconds.
_STATS_INTERVAL: float = 60.0
#: Beyond this lag the loop stops trying to catch up and re-bases its clock.
_MAX_LAG_SECONDS: float = 1.0


@dataclass(slots=True)
class LoopStats:
    """Rolling counters of the render loop."""

    frames_rendered: int = 0
    frames_dropped: int = 0
    frames_late: int = 0
    render_seconds: float = 0.0
    window_started_at: float = field(default_factory=time.monotonic)

    def reset_window(self) -> None:
        """Start a new statistics window."""
        self.frames_rendered = 0
        self.frames_dropped = 0
        self.frames_late = 0
        self.render_seconds = 0.0
        self.window_started_at = time.monotonic()

    def summary(self, target_fps: int) -> str:
        """Format the window for the log."""
        elapsed = max(time.monotonic() - self.window_started_at, 1e-6)
        effective_fps = self.frames_rendered / elapsed
        average_render_ms = (
            (self.render_seconds / self.frames_rendered * 1000.0) if self.frames_rendered else 0.0
        )
        return (
            f"fps={effective_fps:.2f}/{target_fps} frames={self.frames_rendered} "
            f"dropped={self.frames_dropped} late={self.frames_late} "
            f"render_avg={average_render_ms:.1f}ms"
        )


class Application:
    """Owns the frame clock and drives one full FIDS broadcast."""

    def __init__(
        self,
        config: Config,
        flight_manager: FlightManager,
        renderer: FidsRenderer,
        engine: StreamEngine,
        provider: DynamicFlightProvider | None = None,
    ) -> None:
        self._config = config
        self._flights = flight_manager
        self._renderer = renderer
        self._engine = engine
        self._provider = provider
        self._stop = threading.Event()
        self._stats = LoopStats()
        self._frame_index = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> DynamicFlightProvider | None:
        """The flight provider, if it is a :class:`DynamicFlightProvider`.

        External code can use this to update flights at runtime::

            app.provider.set_flights([...])
        """
        return self._provider

    def install_signal_handlers(self) -> None:
        """Translate SIGINT/SIGTERM into a clean loop shutdown."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                _LOG.debug("Could not install handler for %s", sig, exc_info=True)

    def request_stop(self) -> None:
        """Ask the render loop to finish the current frame and return."""
        self._stop.set()

    def run(self) -> int:
        """Run until stopped.

        Returns:
            A process exit code: ``0`` for a clean shutdown.
        """
        config = self._config
        _LOG.info("Crane Airlines FIDS starting: %s", config.describe())

        try:
            self._engine.start()
        except StreamEngineError:
            _LOG.exception("Streaming engine could not start; aborting")
            return 2

        started_at = time.monotonic()
        interval = config.video.frame_interval
        try:
            while not self._stop.is_set():
                self._tick(started_at, interval)
        except KeyboardInterrupt:  # pragma: no cover - interactive runs
            _LOG.info("Interrupted by keyboard")
        finally:
            self._engine.stop()
            _LOG.info(
                "FIDS stopped after %d frames (%s)",
                self._frame_index,
                self._stats.summary(config.video.fps),
            )
        return 0

    # ------------------------------------------------------------------ #
    # Loop internals
    # ------------------------------------------------------------------ #
    def _tick(self, started_at: float, interval: float) -> None:
        """Render and publish exactly one frame, then sleep until the next slot.

        Every step is individually guarded: a failing provider, renderer or
        encoder degrades the picture, never the process.
        """
        frame_started = time.monotonic()
        try:
            frame = self._render_frame()
        except Exception:
            _LOG.exception("Frame %d failed to render; skipping", self._frame_index)
            self._stats.frames_dropped += 1
            self._stop.wait(interval)
            self._frame_index += 1
            return

        try:
            if self._engine.write(frame):
                self._stats.frames_rendered += 1
            else:
                self._stats.frames_dropped += 1
        except Exception:
            _LOG.exception("Unexpected engine failure on frame %d", self._frame_index)
            self._stats.frames_dropped += 1

        self._stats.render_seconds += time.monotonic() - frame_started
        self._frame_index += 1
        self._maybe_log_stats()
        self._sleep_until_next_frame(started_at, interval)

    def _render_frame(self) -> bytes:
        now = datetime.now(timezone.utc)
        flights = self._flights.current_flights(now)
        ctx = FrameContext(
            now=now,
            frame_index=self._frame_index,
            fps=self._config.video.fps,
            flights=flights,
            airline_name=self._config.airline_name,
            airport_name=self._config.airport_name,
            board_title=self._config.board_title,
            ticker_text=self._config.ticker_text,
            timezone_label=self._config.timezone_label,
            rows_per_page=self._config.rows_per_page,
            page_seconds=self._config.page_seconds,
            board=self._flights.board,
        )
        return self._renderer.render_bytes(ctx)

    def _sleep_until_next_frame(self, started_at: float, interval: float) -> None:
        """Pace the loop against a monotonic schedule (no cumulative drift)."""
        target = started_at + self._frame_index * interval
        remaining = target - time.monotonic()
        if remaining > 0:
            self._stop.wait(remaining)
            return

        self._stats.frames_late += 1
        if -remaining > _MAX_LAG_SECONDS:
            # The host is too slow (or was suspended).  Chasing the backlog
            # would only make it worse, so re-base the schedule instead.
            skipped = int(-remaining / interval)
            _LOG.warning(
                "Render loop is %.2fs behind schedule; dropping %d frame slots", -remaining, skipped
            )
            self._frame_index += skipped

    def _maybe_log_stats(self) -> None:
        if time.monotonic() - self._stats.window_started_at < _STATS_INTERVAL:
            return
        extra = ""
        if isinstance(self._engine, FFmpegStreamEngine):
            extra = f" ffmpeg_restarts={self._engine.restarts}"
        _LOG.info("Health: %s%s", self._stats.summary(self._config.video.fps), extra)
        self._stats.reset_window()

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        _LOG.info("Received signal %s; shutting down", signal.Signals(signum).name)
        self.request_stop()


def build_application(
    config: Config,
    provider: DynamicFlightProvider | None = None,
) -> Application:
    """Composition root: build the object graph described by ``config``.

    Args:
        config: Application configuration.
        provider: Optional flight provider.  If omitted, a fresh
            :class:`DynamicFlightProvider` is created.  Pass your own
            instance when you need to update flights from external code.
    """
    if provider is None:
        provider = DynamicFlightProvider()
    flight_manager = FlightManager(
        provider,
        refresh_seconds=config.flight_refresh_seconds,
        board=BoardKind.DEPARTURES,
    )
    renderer = FidsRenderer.from_config(config)
    engine: StreamEngine = (
        FFmpegStreamEngine(config.video, config.stream)
        if config.stream.enabled
        else NullStreamEngine()
    )
    return Application(config, flight_manager, renderer, engine, provider=provider)
