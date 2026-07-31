"""Tests for the renderer, the frame context and the streaming engine."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PIL import Image, ImageDraw

from crane_fids.config import Config, StreamConfig, VideoConfig
from crane_fids.models import Flight, FlightStatus
from crane_fids.renderer import FidsRenderer, FrameContext, Layout, Theme
from crane_fids.renderer.layers import Layer
from crane_fids.stream_engine import FFmpegStreamEngine, NullStreamEngine, StreamEngineError

NOW = datetime(2026, 7, 16, 9, 30, 15)


def _flights(count: int) -> tuple[Flight, ...]:
    return tuple(
        Flight(
            flight_number=f"CR{100 + index}",
            destination="NEW YORK",
            scheduled=NOW + timedelta(minutes=10 * index),
            gate="A12",
            status=FlightStatus.EC_BOARDING if index % 2 else FlightStatus.ON_TIME,
        )
        for index in range(count)
    )


def _context(**overrides: object) -> FrameContext:
    defaults: dict[str, object] = {
        "now": NOW,
        "frame_index": 0,
        "fps": 30,
        "flights": _flights(16),
        "ticker_text": "WELCOME TO CRANE INTERNATIONAL AIRPORT",
        "rows_per_page": 10,
        "page_seconds": 15,
    }
    defaults.update(overrides)
    return FrameContext(**defaults)  # type: ignore[arg-type]


class TestFrameContext:
    def test_paging(self) -> None:
        ctx = _context()
        assert ctx.page_count == 2
        assert ctx.page_index == 0
        assert len(ctx.page_flights) == 10
        # 15 seconds in (30 fps * 15 = frame 450) the second page is on screen.
        assert _context(frame_index=450).page_index == 1
        assert len(_context(frame_index=450).page_flights) == 6

    def test_paging_is_stable_without_flights(self) -> None:
        ctx = _context(flights=())
        assert ctx.page_count == 1
        assert ctx.page_flights == ()

    def test_blinker_phase(self) -> None:
        assert _context(frame_index=0).blink_on(1.0) is True
        assert _context(frame_index=20).blink_on(1.0) is False  # 0.66s -> off phase
        assert _context(frame_index=30).blink_on(1.0) is True


class TestLayout:
    @pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (854, 480)])
    def test_bands_fill_the_frame_without_overlap(self, size: tuple[int, int]) -> None:
        width, height = size
        layout = Layout(width=width, height=height, rows_per_page=10)
        assert 0 < layout.header_height < layout.rows_top < layout.rows_bottom <= height
        assert layout.ticker_top + layout.ticker_height == height
        assert layout.row_height > 0
        for column in layout.columns:
            left, right = layout.column_box(column)
            assert 0 <= left < right <= width


class TestFidsRenderer:
    def test_frame_size_and_format(self) -> None:
        renderer = FidsRenderer((640, 360), rows_per_page=6)
        image = renderer.render(_context())
        assert image.size == (640, 360)
        assert image.mode == "RGB"
        assert len(renderer.render_bytes(_context())) == 640 * 360 * 3

    def test_invalid_size(self) -> None:
        with pytest.raises(ValueError):
            FidsRenderer((0, 100))

    def test_from_config(self) -> None:
        renderer = FidsRenderer.from_config(Config.from_env({"WIDTH": "640", "HEIGHT": "360"}))
        assert renderer.size == (640, 360)

    def test_background_is_cached_and_frames_differ_over_time(self) -> None:
        renderer = FidsRenderer((640, 360), rows_per_page=6)
        first = renderer.render(_context(frame_index=0)).copy()
        # A blinking status must change the picture half a second later.
        later = renderer.render(_context(frame_index=20)).copy()
        assert first.tobytes() != later.tobytes()

    def test_a_broken_layer_cannot_stop_the_stream(self) -> None:
        class ExplodingLayer(Layer):
            def draw(
                self, image: Image.Image, draw: ImageDraw.ImageDraw, ctx: FrameContext
            ) -> None:
                raise RuntimeError("boom")

        theme, layout = Theme(), Layout(width=320, height=180, rows_per_page=4)
        renderer = FidsRenderer(
            (320, 180),
            theme=theme,
            layout=layout,
            layers=[ExplodingLayer(theme, layout, FidsRenderer((16, 16))._fonts)],
        )
        assert renderer.render(_context()).size == (320, 180)


class TestStreamEngine:
    def test_null_engine_counts_frames(self) -> None:
        with NullStreamEngine() as engine:
            assert engine.write(b"x") is True
            assert engine.is_running is True
        assert engine.write(b"x") is False

    def test_ffmpeg_requires_a_target(self) -> None:
        with pytest.raises(StreamEngineError):
            FFmpegStreamEngine(VideoConfig(), StreamConfig(rtmp_url=""))

    def test_command_line(self) -> None:
        engine = FFmpegStreamEngine(
            VideoConfig(width=1280, height=720, fps=25),
            StreamConfig(rtmp_url="rtmp://example.test/live2/key"),
        )
        command = engine.build_command()
        assert command[-3:] == ["-f", "flv", "rtmp://example.test/live2/key"]
        assert "rawvideo" in command and "rgb24" in command
        assert "1280x720" in command
        assert command[command.index("-g") + 1] == "50"  # fps * gop_seconds
        assert "anullsrc=channel_layout=stereo:sample_rate=44100" in command

    def test_audio_can_be_disabled(self) -> None:
        engine = FFmpegStreamEngine(
            VideoConfig(),
            StreamConfig(rtmp_url="rtmp://example.test/live2/key", audio_enabled=False),
        )
        assert "-c:a" not in engine.build_command()

    def test_wrong_frame_size_is_a_programming_error(self) -> None:
        engine = FFmpegStreamEngine(VideoConfig(), StreamConfig(rtmp_url="rtmp://example.test/x"))
        with pytest.raises(ValueError):
            engine.write(b"too-short")

    def test_stream_key_is_redacted_from_ffmpeg_stderr(self) -> None:
        url = "rtmps://a.rtmps.youtube.com/live2/u4mu-gaza-c8jc-sgjd-a2zr"
        engine = FFmpegStreamEngine(VideoConfig(), StreamConfig(rtmp_url=url))
        # FFmpeg prints the full URL, and sometimes the bare key, in its stderr.
        assert "u4mu-gaza-c8jc-sgjd-a2zr" not in engine._redact_secrets(f"ffmpeg: {url}: End of file")
        assert "u4mu-gaza-c8jc-sgjd-a2zr" not in engine._redact_secrets("key=u4mu-gaza-c8jc-sgjd-a2zr")
        assert "<stream-key-redacted>" in engine._redact_secrets(f"opening {url}")
