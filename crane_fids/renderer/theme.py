"""Look-and-feel: colours, and a resolution independent layout.

Every geometric value used by the layers is derived here from the frame size
via ratios, so the very same code renders a correct board at 1280x720 or
1920x1080.  No layer is allowed to contain a magic pixel number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = ["Column", "Layout", "Theme"]

RGB = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Theme:
    """Colour palette of an airport board: black canvas, white type, accents."""

    background: RGB = (8, 9, 12)
    header_background: RGB = (14, 16, 22)
    header_accent: RGB = (255, 214, 0)
    header_text: RGB = (255, 255, 255)
    board_title: RGB = (255, 214, 0)
    column_header_background: RGB = (22, 25, 33)
    column_header_text: RGB = (150, 158, 170)
    row_background_odd: RGB = (8, 9, 12)
    row_background_even: RGB = (16, 18, 24)
    row_separator: RGB = (32, 36, 46)
    primary_text: RGB = (255, 255, 255)
    highlight_text: RGB = (255, 214, 0)
    muted_text: RGB = (150, 158, 170)
    remark_text: RGB = (255, 138, 0)
    ticker_background: RGB = (14, 16, 22)
    ticker_text: RGB = (255, 214, 0)
    clock_text: RGB = (255, 255, 255)
    clock_seconds: RGB = (255, 214, 0)
    page_indicator_active: RGB = (255, 214, 0)
    page_indicator_idle: RGB = (60, 66, 80)


@dataclass(frozen=True, slots=True)
class Column:
    """A board column.

    Attributes:
        key: Logical identifier (``"time"``, ``"flight"`` ...).
        title: Text drawn in the column header.
        x_ratio: Left edge, as a fraction of the frame width.
        width_ratio: Width, as a fraction of the frame width.
        align: ``"left"``, ``"center"`` or ``"right"``.
    """

    key: str
    title: str
    x_ratio: float
    width_ratio: float
    align: str = "left"


_DEFAULT_COLUMNS: Final[tuple[Column, ...]] = (
    Column("time", "TIME", 0.031, 0.100, "left"),
    Column("flight", "FLIGHT", 0.141, 0.115, "left"),
    Column("destination", "DESTINATION", 0.266, 0.200, "left"),
    Column("departure", "DEPARTURE", 0.476, 0.200, "left"),
    Column("gate", "GATE", 0.686, 0.094, "center"),
    Column("status", "STATUS", 0.790, 0.180, "right"),
)


@dataclass(frozen=True, slots=True)
class Layout:
    """Absolute pixel geometry for one frame size."""

    width: int
    height: int
    columns: tuple[Column, ...] = _DEFAULT_COLUMNS
    rows_per_page: int = 10

    # -- vertical bands ------------------------------------------------- #
    header_height_ratio: float = 0.102
    column_header_height_ratio: float = 0.056
    ticker_height_ratio: float = 0.065
    margin_ratio: float = 0.031

    # -- derived, filled in __post_init__ ------------------------------- #
    header_height: int = field(init=False)
    column_header_top: int = field(init=False)
    column_header_height: int = field(init=False)
    rows_top: int = field(init=False)
    rows_bottom: int = field(init=False)
    row_height: int = field(init=False)
    ticker_top: int = field(init=False)
    ticker_height: int = field(init=False)
    margin: int = field(init=False)

    def __post_init__(self) -> None:
        header_height = int(self.height * self.header_height_ratio)
        column_header_height = int(self.height * self.column_header_height_ratio)
        ticker_height = int(self.height * self.ticker_height_ratio)
        rows_top = header_height + column_header_height
        rows_bottom = self.height - ticker_height
        row_height = (rows_bottom - rows_top) // max(self.rows_per_page, 1)

        object.__setattr__(self, "header_height", header_height)
        object.__setattr__(self, "column_header_top", header_height)
        object.__setattr__(self, "column_header_height", column_header_height)
        object.__setattr__(self, "rows_top", rows_top)
        object.__setattr__(self, "rows_bottom", rows_bottom)
        object.__setattr__(self, "row_height", row_height)
        object.__setattr__(self, "ticker_top", rows_bottom)
        object.__setattr__(self, "ticker_height", ticker_height)
        object.__setattr__(self, "margin", int(self.width * self.margin_ratio))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def column_box(self, column: Column) -> tuple[int, int]:
        """Return ``(left, right)`` pixel bounds of ``column``."""
        left = int(self.width * column.x_ratio)
        right = left + int(self.width * column.width_ratio)
        return left, right

    def row_box(self, index: int) -> tuple[int, int]:
        """Return ``(top, bottom)`` pixel bounds of row ``index`` on a page."""
        top = self.rows_top + index * self.row_height
        return top, top + self.row_height

    def anchor_x(self, column: Column) -> tuple[int, str]:
        """Return the ``(x, anchor)`` pair Pillow needs for ``column``.

        The anchors are vertically centred (``*m``), so callers pass the row
        centre line as ``y`` and never deal with baselines or ascenders.
        """
        left, right = self.column_box(column)
        if column.align == "right":
            return right, "rm"
        if column.align == "center":
            return (left + right) // 2, "mm"
        return left, "lm"

    # -- font sizes ------------------------------------------------------ #
    @property
    def airline_font_size(self) -> int:
        """Font size of the airline name in the header."""
        return int(self.header_height * 0.44)

    @property
    def board_title_font_size(self) -> int:
        """Font size of the ``DEPARTURES`` title."""
        return int(self.header_height * 0.42)

    @property
    def subtitle_font_size(self) -> int:
        """Font size of the airport name / secondary header text."""
        return int(self.header_height * 0.19)

    @property
    def clock_font_size(self) -> int:
        """Font size of the ``HH:MM`` clock."""
        return int(self.header_height * 0.46)

    @property
    def clock_seconds_font_size(self) -> int:
        """Font size of the blinking seconds."""
        return int(self.header_height * 0.26)

    @property
    def column_header_font_size(self) -> int:
        """Font size of the column titles."""
        return int(self.column_header_height * 0.46)

    @property
    def row_font_size(self) -> int:
        """Font size of a flight row."""
        return int(self.row_height * 0.46)

    @property
    def remark_font_size(self) -> int:
        """Font size of the secondary remark under a destination."""
        return int(self.row_height * 0.24)

    @property
    def ticker_font_size(self) -> int:
        """Font size of the scrolling welcome message."""
        return int(self.ticker_height * 0.46)
