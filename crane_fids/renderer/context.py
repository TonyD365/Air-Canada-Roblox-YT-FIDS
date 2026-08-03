"""The immutable snapshot a frame is rendered from.

:class:`FrameContext` is the *only* input of the renderer.  Because it is a
plain value object, any frame can be reproduced in a test by constructing a
context by hand -- no clock, no provider, no FFmpeg required.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from ..models import BoardKind, Flight

__all__ = ["FrameContext"]


@dataclass(frozen=True, slots=True)
class FrameContext:
    """Everything the layers may look at while drawing one frame.

    Attributes:
        now: Wall-clock instant of this frame.
        frame_index: Monotonic frame counter since process start.
        fps: Nominal frame rate, used to convert frames to seconds.
        flights: Full, ordered flight list for the board.
        airline_name: Text on the top-left of the header.
        airport_name: Secondary header text.
        board_title: e.g. ``"DEPARTURES"``.
        ticker_text: Message scrolling at the bottom.
        timezone_label: Small caption above the clock.
        rows_per_page: How many flights fit on one page.
        page_seconds: How long a page stays on screen.
        board: Which board is being rendered.
    """

    now: datetime
    frame_index: int
    fps: int
    flights: tuple[Flight, ...]
    airline_name: str = "CRANE AIRLINES"
    airport_name: str = "CRANE INTERNATIONAL AIRPORT"
    board_title: str = "DEPARTURES"
    ticker_text: str = ""
    timezone_label: str = "LOCAL TIME"
    rows_per_page: int = 10
    page_seconds: int = 15
    board: BoardKind = BoardKind.DEPARTURES

    # ------------------------------------------------------------------ #
    # Time helpers
    # ------------------------------------------------------------------ #
    @property
    def elapsed_seconds(self) -> float:
        """Seconds of stream time since the first frame."""
        return self.frame_index / float(self.fps or 1)

    def blink_on(self, hertz: float = 1.0, duty: float = 0.5) -> bool:
        """Return the state of a blinker of frequency ``hertz``.

        Driven by stream time rather than wall-clock time so that every blinker
        stays in phase even if the host clock is stepped by NTP.
        """
        if hertz <= 0:
            return True
        phase = math.fmod(self.elapsed_seconds * hertz, 1.0)
        return phase < duty

    # ------------------------------------------------------------------ #
    # Paging helpers
    # ------------------------------------------------------------------ #
    @property
    def page_count(self) -> int:
        """Number of pages needed to show every flight (at least one)."""
        if not self.flights:
            return 1
        return max(1, math.ceil(len(self.flights) / max(self.rows_per_page, 1)))

    @property
    def page_index(self) -> int:
        """Zero-based index of the page currently on screen."""
        if self.page_count <= 1:
            return 0
        cycles = int(self.elapsed_seconds // max(self.page_seconds, 1))
        return cycles % self.page_count

    @property
    def page_flights(self) -> tuple[Flight, ...]:
        """The slice of :attr:`flights` visible on the current page."""
        start = self.page_index * self.rows_per_page
        return self.flights[start : start + self.rows_per_page]
