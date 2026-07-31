"""FFmpeg streaming engine.

Owns exactly one responsibility: get RGB24 frames out of this process and into
an RTMP endpoint, forever.  It hides every detail of the FFmpeg child process
(command line, stdin back-pressure, stderr, crashes, reconnects) behind
:class:`StreamEngine`.

Failure policy: a dead FFmpeg is a *normal* event on a 24x7 stream (YouTube
rotates ingest servers, networks blip).  The engine therefore never raises on
write; it restarts the child with exponential backoff and drops frames while
the child is down, so the render loop keeps its cadence.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from types import TracebackType

from .config import StreamConfig, VideoConfig, redact_rtmp_url

__all__ = ["FFmpegStreamEngine", "NullStreamEngine", "StreamEngine", "StreamEngineError"]

_LOG = logging.getLogger(__name__)


class StreamEngineError(RuntimeError):
    """Raised for unrecoverable engine problems (e.g. FFmpeg is not installed)."""


class StreamEngine(ABC):
    """Sink for encoded-ready raw frames."""

    @abstractmethod
    def start(self) -> None:
        """Bring the sink up.  Must be idempotent."""

    @abstractmethod
    def write(self, frame: bytes) -> bool:
        """Push one RGB24 frame.

        Returns:
            ``True`` if the frame reached the sink, ``False`` if it was dropped
            (sink down / restarting).  Never raises for transient problems.
        """

    @abstractmethod
    def stop(self) -> None:
        """Shut the sink down cleanly.  Must be idempotent."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Whether the sink is currently able to accept frames."""

    def __enter__(self) -> StreamEngine:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


class NullStreamEngine(StreamEngine):
    """Dry-run sink: counts frames and discards them.

    Used when ``RTMP_URL`` is empty, which makes the whole application runnable
    (and testable) locally without a broadcast target.
    """

    def __init__(self) -> None:
        self._running = False
        self.frames_written = 0

    def start(self) -> None:
        self._running = True
        _LOG.warning("RTMP_URL is not set: running in dry-run mode, frames are discarded")

    def write(self, frame: bytes) -> bool:
        if not self._running:
            return False
        self.frames_written += 1
        return True

    def stop(self) -> None:
        self._running = False
        _LOG.info("Dry-run engine stopped after %d frames", self.frames_written)

    @property
    def is_running(self) -> bool:
        return self._running


class FFmpegStreamEngine(StreamEngine):
    """Supervised FFmpeg child process publishing FLV/H.264 over RTMP."""

    def __init__(self, video: VideoConfig, stream: StreamConfig) -> None:
        if not stream.rtmp_url:
            raise StreamEngineError("FFmpegStreamEngine requires a non-empty RTMP URL")
        self._video = video
        self._stream = stream
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = False
        self._backoff = stream.restart_backoff_initial
        self._next_start_at = 0.0
        self._restarts = 0
        self.frames_written = 0
        self.frames_dropped = 0

    # ------------------------------------------------------------------ #
    # StreamEngine
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn FFmpeg if it is not already running."""
        with self._lock:
            self._stopping = False
            self._start_locked()

    def write(self, frame: bytes) -> bool:
        """Write one frame, restarting FFmpeg if the pipe is broken."""
        expected = self._video.frame_bytes
        if len(frame) != expected:  # pragma: no cover - renderer contract violation
            raise ValueError(f"Expected {expected} bytes per frame, got {len(frame)}")

        with self._lock:
            if self._stopping:
                return False
            if not self._is_alive():
                self._handle_exit_locked()
                self._start_locked()
                if not self._is_alive():
                    self.frames_dropped += 1
                    return False

            process = self._process
            assert process is not None and process.stdin is not None
            try:
                process.stdin.write(frame)
                self.frames_written += 1
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                _LOG.warning("FFmpeg stdin write failed (%s); scheduling restart", exc)
                self._terminate_locked()
                self._schedule_restart_locked()
                self.frames_dropped += 1
                return False

    def stop(self) -> None:
        """Close stdin, let FFmpeg flush, then make sure it is gone."""
        with self._lock:
            self._stopping = True
            self._terminate_locked(graceful=True)
        _LOG.info(
            "FFmpeg engine stopped: %d frames written, %d dropped, %d restarts",
            self.frames_written,
            self.frames_dropped,
            self._restarts,
        )

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_alive()

    @property
    def restarts(self) -> int:
        """How many times FFmpeg had to be restarted since process start."""
        return self._restarts

    # ------------------------------------------------------------------ #
    # Command line
    # ------------------------------------------------------------------ #
    def build_command(self) -> list[str]:
        """Assemble the FFmpeg command line.

        Exposed (and pure) so it can be asserted on in unit tests and printed
        in the logs without starting a process.
        """
        video, stream = self._video, self._stream
        command: list[str] = [
            stream.ffmpeg_binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            stream.ffmpeg_loglevel,
            # ---- raw video coming from our stdin ----
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{video.width}x{video.height}",
            "-framerate",
            str(video.fps),
            "-thread_queue_size",
            "512",
            "-i",
            "pipe:0",
        ]
        if stream.audio_enabled:
            # YouTube drops streams without an audio track; a silent track is
            # the standard, cheapest way to satisfy the ingest.
            command += [
                "-f",
                "lavfi",
                "-thread_queue_size",
                "512",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        command += [
            "-c:v",
            "libx264",
            "-preset",
            stream.preset,
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "main",
            "-b:v",
            stream.video_bitrate,
            "-maxrate",
            stream.video_maxrate,
            "-bufsize",
            stream.video_bufsize,
            "-g",
            str(video.fps * stream.gop_seconds),
            "-keyint_min",
            str(video.fps * stream.gop_seconds),
            "-sc_threshold",
            "0",
            "-r",
            str(video.fps),
        ]
        if stream.audio_enabled:
            command += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
        command += ["-fflags", "+genpts", "-f", "flv", stream.rtmp_url]
        return command

    # ------------------------------------------------------------------ #
    # Internals (all called with ``self._lock`` held)
    # ------------------------------------------------------------------ #
    def _is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start_locked(self) -> None:
        if self._is_alive():
            return
        now = time.monotonic()
        if now < self._next_start_at:
            return  # still in backoff; frames are dropped meanwhile

        binary = self._stream.ffmpeg_binary
        if shutil.which(binary) is None:
            raise StreamEngineError(
                f"FFmpeg binary {binary!r} not found in PATH; install it or set FFMPEG_BINARY"
            )

        command = self.build_command()
        _LOG.info("Starting FFmpeg -> %s", redact_rtmp_url(self._stream.rtmp_url))
        _LOG.debug("FFmpeg command: %s", " ".join([*command[:-1], "<rtmp-url>"]))
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            _LOG.error("Could not spawn FFmpeg: %s", exc)
            self._schedule_restart_locked()
            return

        self._stderr_thread = threading.Thread(
            target=self._pump_stderr,
            args=(self._process,),
            name="ffmpeg-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self._backoff = self._stream.restart_backoff_initial
        _LOG.info("FFmpeg started (pid=%s)", self._process.pid)

    def _handle_exit_locked(self) -> None:
        """Log why a previously running FFmpeg is gone."""
        if self._process is None:
            return
        code = self._process.poll()
        if code is None:
            return
        self._restarts += 1
        _LOG.warning(
            "FFmpeg exited with code %s after %d frames (restart #%d)",
            code,
            self.frames_written,
            self._restarts,
        )
        self._process = None

    def _schedule_restart_locked(self) -> None:
        self._next_start_at = time.monotonic() + self._backoff
        _LOG.info("Next FFmpeg start attempt in %.1fs", self._backoff)
        self._backoff = min(self._backoff * 2.0, self._stream.restart_backoff_max)

    def _terminate_locked(self, *, graceful: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    process.stdin.close()
            if graceful:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _LOG.warning("FFmpeg did not exit in time; terminating")
                    process.terminate()
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                _LOG.error("FFmpeg ignored SIGTERM; killing")
                process.kill()
                process.wait(timeout=5)
        except Exception:
            _LOG.exception("Error while terminating FFmpeg")

    def _pump_stderr(self, process: subprocess.Popen[bytes]) -> None:
        """Forward FFmpeg's stderr into our logger, line by line."""
        stream = process.stderr
        if stream is None:  # pragma: no cover - defensive
            return
        try:
            for raw_line in iter(stream.readline, b""):
                line = self._redact_secrets(raw_line.decode("utf-8", errors="replace").strip())
                if not line:
                    continue
                if any(token in line.lower() for token in ("error", "failed", "invalid")):
                    _LOG.error("ffmpeg: %s", line)
                else:
                    _LOG.info("ffmpeg: %s", line)
        except Exception:
            _LOG.debug("FFmpeg stderr reader stopped", exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def _redact_secrets(self, line: str) -> str:
        """Strip the RTMP stream key from an FFmpeg log line before it is logged.

        FFmpeg prints the full ingest URL (key and all) in its own stderr, e.g.
        ``rtmps://a.rtmps.youtube.com/live2/<key>: End of file``.  Our own log
        lines are already redacted; this closes the one remaining leak so a
        stream key can never reach the logs (or a screenshot of them).
        """
        url = self._stream.rtmp_url
        if url and url in line:
            line = line.replace(url, redact_rtmp_url(url))
        key = url.rpartition("/")[2]
        if len(key) >= 4 and key in line:
            line = line.replace(key, "<stream-key-redacted>")
        return line
