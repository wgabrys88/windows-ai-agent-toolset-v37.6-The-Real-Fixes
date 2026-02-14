"""
FILENAME: capture.py

SYSTEM: FRANZ — Agentic Visual Loop for Windows 11 (Python 3.13, stdlib only)

BIGGER PICTURE:
    This file is the SCREENSHOT PRODUCER in the FRANZ pipeline. It is called
    by execute.py (as a subprocess) after actions have been parsed and
    validated. Its job is to produce a screenshot image that shows the current
    state of the world — either the real Windows desktop, or a persistent
    sandbox canvas — and return it as base64 PNG.

    The pipeline data flow:
        main.py  ──stdin JSON──►  execute.py  ──stdin JSON──►  capture.py
                 ◄─stdout JSON──             ◄──stdout JSON──

    capture.py receives a list of CANONICAL action strings (already validated
    by execute.py). In sandbox mode, it renders the visual effect of each
    action onto a persistent black canvas. In real mode, it captures the
    actual screen via Win32 GDI. In both modes, it optionally draws red
    "marks" (numbered overlays) on a COPY of the image so the VLM can see
    what was executed — marks are NEVER persisted to the sandbox canvas.

SANDBOX TRANSPARENCY:
    The sandbox canvas is a persistent BMP file (sandbox_canvas.bmp) that
    accumulates white drawings across turns. A companion JSON file
    (sandbox_state.json) tracks the last click position for type() actions.
    From the pipeline's perspective, the sandbox is indistinguishable from a
    real desktop: actions produce visible changes, and the screenshot reflects
    the current state. The pipeline cannot tell the difference.

    Important: not all actions can always be applied. For example, type()
    requires a prior click position. If the position is unknown, the action
    is silently skipped on the canvas. This file reports back which actions
    were actually applied via the "applied" field in its JSON output, so
    that execute.py can reconcile its executed/noted lists and give the VLM
    accurate feedback.

MARKS vs SANDBOX DRAWINGS:
    - Sandbox drawings (white): PERSISTENT — written to sandbox_canvas.bmp
    - Red marks (numbered circles, arrows): EPHEMERAL — drawn on a copy,
      never saved. They help the VLM see what happened but don't accumulate.

SST GUARANTEE:
    This file has NO access to the VLM text. It receives only canonical
    action strings and image parameters. It cannot affect the SST data path.

FILE PIPELINE:
    INPUTS (stdin JSON from execute.py):
        actions: list[str]        — canonical executed action strings
        width, height: int        — output screenshot dimensions (0 = screen size)
        marks: bool               — draw red visual marks overlay
        sandbox: bool             — use persistent canvas instead of real capture
        sandbox_reset: bool       — wipe canvas and state before this turn

    OUTPUTS (stdout JSON to execute.py):
        screenshot_b64: str       — base64-encoded PNG of the current view
        applied: list[str]        — actions that were actually rendered on canvas
                                    (in real mode, equals the input actions list;
                                     in sandbox mode, may be a subset if e.g.
                                     type() was skipped due to no cursor position)

    SIDE EFFECTS (sandbox mode only):
        sandbox_canvas.bmp        — persistent pixel data (atomic write)
        sandbox_state.json        — persistent last_x/last_y (atomic write)

RUNTIME:
    - Windows 11, Python 3.13+
    - Stdlib only (ctypes for Win32 GDI screen capture and resize)
"""

from __future__ import annotations

import ast
import base64
import ctypes
import ctypes.wintypes
import json
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Color = tuple[int, int, int, int]
Point = tuple[int, int]

# ---------------------------------------------------------------------------
# GDI constants
# ---------------------------------------------------------------------------

_SRCCOPY: Final[int] = 0x00CC0020
_CAPTUREBLT: Final[int] = 0x40000000
_BI_RGB: Final[int] = 0
_DIB_RGB: Final[int] = 0
_HALFTONE: Final[int] = 4

# ---------------------------------------------------------------------------
# Visual mark colors (ephemeral overlay, never persisted)
# ---------------------------------------------------------------------------

MARK_FILL: Final[Color] = (255, 0, 0, 180)
MARK_OUTLINE: Final[Color] = (255, 255, 255, 230)
MARK_TEXT: Final[Color] = (255, 255, 255, 255)
TRAIL_COLOR: Final[Color] = (255, 0, 0, 120)

# ---------------------------------------------------------------------------
# Sandbox colors
# ---------------------------------------------------------------------------

SANDBOX_WHITE: Final[Color] = (255, 255, 255, 255)
BLACK: Final[Color] = (0, 0, 0, 255)

# ---------------------------------------------------------------------------
# Sandbox persistence paths
# ---------------------------------------------------------------------------

SANDBOX_DEFAULT: Final[bool] = False
SANDBOX_RESET_DEFAULT: Final[bool] = False
SANDBOX_CANVAS: Final[Path] = Path(__file__).with_name("sandbox_canvas.bmp")
SANDBOX_STATE: Final[Path] = Path(__file__).with_name("sandbox_state.json")

# ---------------------------------------------------------------------------
# Win32 initialization
# ---------------------------------------------------------------------------

_shcore: Final = ctypes.WinDLL("shcore", use_last_error=True)
_shcore.SetProcessDpiAwareness(2)
_user32: Final = ctypes.WinDLL("user32", use_last_error=True)
_gdi32: Final = ctypes.WinDLL("gdi32", use_last_error=True)

_screen_w: Final[int] = _user32.GetSystemMetrics(0)
_screen_h: Final[int] = _user32.GetSystemMetrics(1)


# ---------------------------------------------------------------------------
# GDI bitmap structures
# ---------------------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = _BI_RGB
    return bmi


# ---------------------------------------------------------------------------
# Screen capture (real desktop mode)
# ---------------------------------------------------------------------------


def _capture_bgra(w: int, h: int) -> bytes:
    sdc = _user32.GetDC(0)
    memdc = _gdi32.CreateCompatibleDC(sdc)
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(
        sdc, ctypes.byref(_make_bmi(w, h)), _DIB_RGB, ctypes.byref(bits), None, 0
    )
    old = _gdi32.SelectObject(memdc, hbmp)
    try:
        _gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, _SRCCOPY | _CAPTUREBLT)
        raw = bytes((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value))
    finally:
        _gdi32.SelectObject(memdc, old)
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(0, sdc)
    return raw


def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    sdc = _user32.GetDC(0)
    src_dc = _gdi32.CreateCompatibleDC(sdc)
    dst_dc = _gdi32.CreateCompatibleDC(sdc)
    src_bmp = _gdi32.CreateCompatibleBitmap(sdc, sw, sh)
    old_src = _gdi32.SelectObject(src_dc, src_bmp)
    dst_bits = ctypes.c_void_p()
    dst_bmp = _gdi32.CreateDIBSection(
        sdc, ctypes.byref(_make_bmi(dw, dh)), _DIB_RGB, ctypes.byref(dst_bits), None, 0
    )
    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)
    try:
        _gdi32.SetDIBits(sdc, src_bmp, 0, sh, src, ctypes.byref(_make_bmi(sw, sh)), _DIB_RGB)
        _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
        _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
        _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)
        out = bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dst_bits.value))
    finally:
        _gdi32.SelectObject(dst_dc, old_dst)
        _gdi32.SelectObject(src_dc, old_src)
        _gdi32.DeleteObject(dst_bmp)
        _gdi32.DeleteObject(src_bmp)
        _gdi32.DeleteDC(dst_dc)
        _gdi32.DeleteDC(src_dc)
        _user32.ReleaseDC(0, sdc)
    return out


# ---------------------------------------------------------------------------
# Pixel format conversion
# ---------------------------------------------------------------------------


def _bgra_to_rgba(bgra: bytes) -> bytearray:
    n = len(bgra)
    out = bytearray(n)
    out[0::4] = bgra[2::4]
    out[1::4] = bgra[1::4]
    out[2::4] = bgra[0::4]
    out[3::4] = b"\xff" * (n // 4)
    return out


def _rgba_to_bgra(rgba: bytes) -> bytes:
    n = len(rgba)
    out = bytearray(n)
    out[0::4] = rgba[2::4]
    out[1::4] = rgba[1::4]
    out[2::4] = rgba[0::4]
    out[3::4] = b"\xff" * (n // 4)
    return bytes(out)


# ---------------------------------------------------------------------------
# PNG encoder (minimal valid PNG: IHDR + IDAT + IEND)
# ---------------------------------------------------------------------------


def _encode_png(rgba: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter byte: None
        raw.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)

    def _chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# Canvas: software rasterizer for drawing primitives
# ---------------------------------------------------------------------------


class Canvas:
    __slots__ = ("buf", "w", "h")

    def __init__(self, buf: bytearray, w: int, h: int) -> None:
        self.buf = buf
        self.w = w
        self.h = h

    def put(self, x: int, y: int, c: Color) -> None:
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        i = (y * self.w + x) << 2
        sa = c[3]
        if sa >= 255:
            self.buf[i] = c[0]
            self.buf[i + 1] = c[1]
            self.buf[i + 2] = c[2]
            self.buf[i + 3] = 255
            return
        da = 255 - sa
        self.buf[i] = (c[0] * sa + self.buf[i] * da) // 255
        self.buf[i + 1] = (c[1] * sa + self.buf[i + 1] * da) // 255
        self.buf[i + 2] = (c[2] * sa + self.buf[i + 2] * da) // 255
        self.buf[i + 3] = 255

    def put_opaque(self, x: int, y: int, c: Color) -> None:
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        i = (y * self.w + x) << 2
        self.buf[i] = c[0]
        self.buf[i + 1] = c[1]
        self.buf[i + 2] = c[2]
        self.buf[i + 3] = 255

    def put_thick_opaque(self, x: int, y: int, c: Color, t: int) -> None:
        half = t >> 1
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                self.put_opaque(x + dx, y + dy, c)

    def put_thick(self, x: int, y: int, c: Color, t: int) -> None:
        half = t >> 1
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                self.put(x + dx, y + dy, c)

    def line(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx - dy
        x, y = x1, y1
        while True:
            self.put_thick(x, y, c, t)
            if x == x2 and y == y2:
                break
            e2 = err << 1
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy

    def line_opaque(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx - dy
        x, y = x1, y1
        while True:
            self.put_thick_opaque(x, y, c, t)
            if x == x2 and y == y2:
                break
            e2 = err << 1
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy

    def circle_opaque(self, cx: int, cy: int, r: int, c: Color) -> None:
        r2 = r * r
        for oy in range(-r, r + 1):
            for ox in range(-r, r + 1):
                if ox * ox + oy * oy <= r2:
                    self.put_opaque(cx + ox, cy + oy, c)

    def rect_opaque(self, x: int, y: int, w: int, h: int, c: Color) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.put_opaque(xx, yy, c)

    def circle(self, cx: int, cy: int, r: int, c: Color, filled: bool, thickness: int) -> None:
        r2o = r * r
        r2i = max(0, (r - thickness)) ** 2
        for oy in range(-r, r + 1):
            for ox in range(-r, r + 1):
                d2 = ox * ox + oy * oy
                if filled:
                    if d2 <= r2o:
                        self.put(cx + ox, cy + oy, c)
                else:
                    if r2i <= d2 <= r2o:
                        self.put(cx + ox, cy + oy, c)

    def rect(self, x: int, y: int, w: int, h: int, c: Color, t: int) -> None:
        self.line(x, y, x + w, y, c, t)
        self.line(x + w, y, x + w, y + h, c, t)
        self.line(x + w, y + h, x, y + h, c, t)
        self.line(x, y + h, x, y, c, t)

    def fill_polygon(self, pts: list[Point], c: Color) -> None:
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        lo = max(0, min(ys))
        hi = min(self.h - 1, max(ys))
        n = len(pts)
        for y in range(lo, hi + 1):
            nodes: list[int] = []
            j = n - 1
            for i in range(n):
                yi, yj = pts[i][1], pts[j][1]
                if (yi < y <= yj) or (yj < y <= yi):
                    nodes.append(int(pts[i][0] + (y - yi) / (yj - yi) * (pts[j][0] - pts[i][0])))
                j = i
            nodes.sort()
            for k in range(0, len(nodes) - 1, 2):
                x0 = max(0, nodes[k])
                x1 = min(self.w - 1, nodes[k + 1])
                for x in range(x0, x1 + 1):
                    self.put(x, y, c)

    def arrow(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        self.line(x1, y1, x2, y2, c, t)
        ang = math.atan2(y2 - y1, x2 - x1)
        ha = math.radians(25.0)
        ln = 28.0
        lx = int(x2 - ln * math.cos(ang - ha))
        ly = int(y2 - ln * math.sin(ang - ha))
        rx = int(x2 - ln * math.cos(ang + ha))
        ry = int(y2 - ln * math.sin(ang + ha))
        self.fill_polygon([(x2, y2), (lx, ly), (rx, ry)], c)


# ---------------------------------------------------------------------------
# 5x7 bitmap font (used for sandbox text and mark numbers)
# ---------------------------------------------------------------------------

_FONT_5X7: Final[dict[str, list[int]]] = {
    " ": [0, 0, 0, 0, 0, 0, 0],
    "0": [0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110],
    "1": [0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "2": [0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111],
    "3": [0b01110, 0b10001, 0b00001, 0b00110, 0b00001, 0b10001, 0b01110],
    "4": [0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010],
    "5": [0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110],
    "6": [0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110],
    "7": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000],
    "8": [0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110],
    "9": [0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100],
    "A": [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "B": [0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110],
    "C": [0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110],
    "D": [0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110],
    "E": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
    "F": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000],
    "G": [0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01110],
    "H": [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "I": [0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "J": [0b00111, 0b00010, 0b00010, 0b00010, 0b10010, 0b10010, 0b01100],
    "K": [0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001],
    "L": [0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111],
    "M": [0b10001, 0b11011, 0b10101, 0b10001, 0b10001, 0b10001, 0b10001],
    "N": [0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001],
    "O": [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "P": [0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000],
    "Q": [0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101],
    "R": [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
    "S": [0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110],
    "T": [0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
    "U": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "V": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100],
    "W": [0b10001, 0b10001, 0b10001, 0b10001, 0b10101, 0b11011, 0b10001],
    "X": [0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001],
    "Y": [0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100],
    "Z": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111],
    ".": [0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00100, 0b00100],
    ",": [0b00000, 0b00000, 0b00000, 0b00000, 0b00100, 0b00100, 0b01000],
    "!": [0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00000, 0b00100],
    "?": [0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b00000, 0b00100],
    "-": [0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000],
    ":": [0b00000, 0b00100, 0b00100, 0b00000, 0b00100, 0b00100, 0b00000],
    "/": [0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b00000, 0b00000],
}

# Digit glyphs derived from the font (eliminates the old _DIGITS duplication)
_DIGITS: Final[list[list[int]]] = [_FONT_5X7[str(d)] for d in range(10)]


def _draw_text(cv: Canvas, x: int, y: int, text: str, c: Color, scale: int) -> None:
    px = x
    py = y
    for ch in text:
        if ch == "\n":
            py += 8 * scale
            px = x
            continue
        up = ch.upper()
        pat = _FONT_5X7.get(up)
        if pat is None:
            # unknown char: draw a small filled box
            cv.rect_opaque(px, py, 5 * scale, 7 * scale, c)
            px += 6 * scale
            continue
        for row in range(7):
            bits = pat[row]
            for col in range(5):
                if bits & (1 << (4 - col)):
                    for sy in range(scale):
                        for sx in range(scale):
                            cv.put_opaque(px + col * scale + sx, py + row * scale + sy, c)
        px += 6 * scale


# ---------------------------------------------------------------------------
# Mark number rendering (outlined digits for visual marks)
# ---------------------------------------------------------------------------


def _render_digit(cv: Canvas, cx: int, cy: int, d: int, fill: Color, outline: Color, scale: int) -> None:
    gw = 5 * scale
    gh = 7 * scale
    ox = cx - gw // 2
    oy = cy - gh // 2
    g = _DIGITS[d]
    # Outline pass (8-neighbor offset)
    for ddy in (-1, 0, 1):
        for ddx in (-1, 0, 1):
            if ddx == 0 and ddy == 0:
                continue
            for ri, row in enumerate(g):
                for ci in range(5):
                    if row & (1 << (4 - ci)):
                        for sy in range(scale):
                            for sx in range(scale):
                                cv.put_opaque(
                                    ox + ci * scale + sx + ddx * 2,
                                    oy + ri * scale + sy + ddy * 2,
                                    outline,
                                )
    # Fill pass
    for ri, row in enumerate(g):
        for ci in range(5):
            if row & (1 << (4 - ci)):
                for sy in range(scale):
                    for sx in range(scale):
                        cv.put_opaque(ox + ci * scale + sx, oy + ri * scale + sy, fill)


def _render_number(cv: Canvas, cx: int, cy: int, n: int, fill: Color, outline: Color, scale: int) -> None:
    s = str(n)
    gw = 5 * scale
    gap = 1 * scale
    tw = len(s) * gw + (len(s) - 1) * gap
    start = cx - tw // 2 + gw // 2
    for i, ch in enumerate(s):
        _render_digit(cv, start + i * (gw + gap), cy, int(ch), fill, outline, scale)


# ---------------------------------------------------------------------------
# Action parsing (same safe AST approach as execute.py)
# ---------------------------------------------------------------------------


def _parse_action(line: str) -> tuple[str, list[object], dict[str, object]] | None:
    s = line.strip()
    if not s:
        return None
    try:
        node = ast.parse(s, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name):
        return None
    name = node.func.id

    args: list[object] = []
    for a in node.args:
        if not isinstance(a, ast.Constant):
            return None
        args.append(a.value)

    kwargs: dict[str, object] = {}
    for kw in node.keywords:
        if kw.arg is None:
            return None
        if not isinstance(kw.value, ast.Constant):
            return None
        kwargs[kw.arg] = kw.value.value

    return name, args, kwargs


def _arg_int(args: list[object], kwargs: dict[str, object], idx: int, key: str) -> int | None:
    if idx < len(args):
        try:
            return int(args[idx])  # type: ignore[arg-type]
        except Exception:
            return None
    if key in kwargs:
        try:
            return int(kwargs[key])  # type: ignore[arg-type]
        except Exception:
            return None
    return None


def _arg_str(args: list[object], kwargs: dict[str, object], idx: int, key: str) -> str | None:
    if idx < len(args):
        try:
            return str(args[idx])
        except Exception:
            return None
    if key in kwargs:
        try:
            return str(kwargs[key])
        except Exception:
            return None
    return None


def _norm(v: int, extent: int) -> int:
    v = max(0, min(1000, v))
    return int((v / 1000.0) * extent)


# ---------------------------------------------------------------------------
# BMP file I/O (sandbox persistence)
# ---------------------------------------------------------------------------


def _bmp_write_black(path: Path, w: int, h: int) -> None:
    stride = ((w * 3 + 3) // 4) * 4
    size_image = stride * h
    file_size = 54 + size_image
    fh = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    ih = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, size_image, 2835, 2835, 0, 0)
    pad = b"\x00" * (stride - w * 3)
    row = b"\x00" * (w * 3) + pad
    data = fh + ih + row * h
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _bmp_load_rgba(path: Path, w: int, h: int) -> bytearray:
    try:
        data = path.read_bytes()
        if len(data) < 54 or data[0:2] != b"BM":
            return bytearray()
        off = struct.unpack_from("<I", data, 10)[0]
        hs = struct.unpack_from("<I", data, 14)[0]
        if hs < 40:
            return bytearray()
        bw, bh = struct.unpack_from("<ii", data, 18)
        planes, bpp = struct.unpack_from("<HH", data, 26)
        comp = struct.unpack_from("<I", data, 30)[0]
        if planes != 1 or comp != 0 or bpp not in (24, 32):
            return bytearray()
        ah = -bh if bh < 0 else bh
        if bw != w or ah != h:
            return bytearray()
        bytespp = bpp // 8
        stride = ((w * bytespp + 3) // 4) * 4
        need = off + stride * h
        if len(data) < need:
            return bytearray()
        out = bytearray(w * h * 4)
        top_down = bh < 0
        for y in range(h):
            sy = y if top_down else (h - 1 - y)
            row = data[off + sy * stride : off + (sy + 1) * stride]
            di = y * w * 4
            if bpp == 24:
                for x in range(w):
                    i = x * 3
                    out[di + (x * 4)] = row[i + 2]
                    out[di + (x * 4) + 1] = row[i + 1]
                    out[di + (x * 4) + 2] = row[i]
                    out[di + (x * 4) + 3] = 255
            else:
                for x in range(w):
                    i = x * 4
                    out[di + (x * 4)] = row[i + 2]
                    out[di + (x * 4) + 1] = row[i + 1]
                    out[di + (x * 4) + 2] = row[i]
                    out[di + (x * 4) + 3] = 255
        return out
    except Exception:
        return bytearray()


def _bmp_save_rgba(path: Path, buf: bytes, w: int, h: int) -> None:
    stride = ((w * 3 + 3) // 4) * 4
    size_image = stride * h
    file_size = 54 + size_image
    fh = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    ih = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, size_image, 2835, 2835, 0, 0)
    pad = b"\x00" * (stride - w * 3)
    out = bytearray()
    out.extend(fh)
    out.extend(ih)
    for y in range(h - 1, -1, -1):
        row = buf[y * w * 4 : (y + 1) * w * 4]
        for x in range(w):
            i = x * 4
            out.append(row[i + 2])
            out.append(row[i + 1])
            out.append(row[i])
        out.extend(pad)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(bytes(out))
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sandbox state persistence
# ---------------------------------------------------------------------------


def _sandbox_state_load(reset: bool) -> dict[str, int | None]:
    if reset:
        return {"last_x": None, "last_y": None}
    try:
        o = json.loads(SANDBOX_STATE.read_text(encoding="utf-8"))
        lx = o.get("last_x")
        ly = o.get("last_y")
        if isinstance(lx, int) and isinstance(ly, int):
            return {"last_x": lx, "last_y": ly}
    except Exception:
        pass
    return {"last_x": None, "last_y": None}


def _sandbox_state_save(st: dict[str, int | None]) -> None:
    tmp = SANDBOX_STATE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(SANDBOX_STATE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sandbox canvas management
# ---------------------------------------------------------------------------


def _sandbox_load(w: int, h: int, reset: bool) -> bytearray:
    if reset:
        _bmp_write_black(SANDBOX_CANVAS, w, h)
        _sandbox_state_save({"last_x": None, "last_y": None})
    if not SANDBOX_CANVAS.is_file():
        _bmp_write_black(SANDBOX_CANVAS, w, h)
    buf = _bmp_load_rgba(SANDBOX_CANVAS, w, h)
    if not buf:
        _bmp_write_black(SANDBOX_CANVAS, w, h)
        return bytearray(b"\x00\x00\x00\xff" * (w * h))
    return buf


def _sandbox_save(buf: bytearray, w: int, h: int) -> None:
    _bmp_save_rgba(SANDBOX_CANVAS, bytes(buf), w, h)


# ---------------------------------------------------------------------------
# Sandbox action application (persistent white drawings)
# ---------------------------------------------------------------------------


def _sandbox_apply(
    buf: bytearray, w: int, h: int, actions: list[str], sandbox_reset: bool
) -> tuple[bool, list[str]]:
    """Apply actions to the sandbox canvas.

    Returns (dirty, applied) where:
        dirty: True if any pixels were changed
        applied: list of action strings that were actually rendered
                 (subset of input — e.g. type() is skipped if no cursor pos)
    """
    cv = Canvas(buf, w, h)
    dirty = False
    applied: list[str] = []
    st = _sandbox_state_load(sandbox_reset)

    def set_last(px: int, py: int) -> None:
        st["last_x"] = px
        st["last_y"] = py

    for line in actions:
        parsed = _parse_action(line)
        if parsed is None:
            continue
        name, args, kwargs = parsed
        if name == "click":
            name = "left_click"

        if name == "drag":
            x1 = _arg_int(args, kwargs, 0, "x1")
            y1 = _arg_int(args, kwargs, 1, "y1")
            x2 = _arg_int(args, kwargs, 2, "x2")
            y2 = _arg_int(args, kwargs, 3, "y2")
            if None in (x1, y1, x2, y2):
                continue
            px1 = _norm(int(x1), w)
            py1 = _norm(int(y1), h)
            px2 = _norm(int(x2), w)
            py2 = _norm(int(y2), h)
            cv.line_opaque(px1, py1, px2, py2, SANDBOX_WHITE, 4)
            set_last(px2, py2)
            dirty = True
            applied.append(line)
            continue

        if name == "left_click" or name == "double_left_click":
            x = _arg_int(args, kwargs, 0, "x")
            y = _arg_int(args, kwargs, 1, "y")
            if x is None or y is None:
                continue
            px = _norm(int(x), w)
            py = _norm(int(y), h)
            cv.circle_opaque(px, py, 6, SANDBOX_WHITE)
            set_last(px, py)
            dirty = True
            applied.append(line)
            continue

        if name == "right_click":
            x = _arg_int(args, kwargs, 0, "x")
            y = _arg_int(args, kwargs, 1, "y")
            if x is None or y is None:
                continue
            px = _norm(int(x), w)
            py = _norm(int(y), h)
            cv.rect_opaque(px - 6, py - 4, 12, 8, SANDBOX_WHITE)
            set_last(px, py)
            dirty = True
            applied.append(line)
            continue

        if name == "type":
            t = _arg_str(args, kwargs, 0, "text")
            if t is None:
                continue
            lx = st.get("last_x")
            ly = st.get("last_y")
            if not isinstance(lx, int) or not isinstance(ly, int):
                # No cursor position — cannot draw text, skip silently.
                # This action will NOT appear in 'applied', so execute.py
                # will move it from executed to noted.
                continue
            # Offset so text is readable next to the marker
            _draw_text(cv, lx + 10, ly + 10, t, SANDBOX_WHITE, 2)
            dirty = True
            applied.append(line)
            continue

    if dirty:
        _sandbox_state_save(st)
    return dirty, applied


# ---------------------------------------------------------------------------
# Visual marks (ephemeral red overlay, drawn on a COPY, never persisted)
# ---------------------------------------------------------------------------


def _apply_marks(buf: bytearray, w: int, h: int, actions: list[str]) -> None:
    cv = Canvas(buf, w, h)
    px: int | None = None
    py: int | None = None
    n = 1
    for line in actions:
        parsed = _parse_action(line)
        if parsed is None:
            continue
        name, args, kwargs = parsed
        if name == "click":
            name = "left_click"

        match name:
            case "left_click":
                x0 = _arg_int(args, kwargs, 0, "x")
                y0 = _arg_int(args, kwargs, 1, "y")
                if x0 is None or y0 is None:
                    continue
                x, y = _norm(int(x0), w), _norm(int(y0), h)
                if px is not None and py is not None and (abs(x - px) + abs(y - py) > 30):
                    cv.line(px, py, x, y, TRAIL_COLOR, 4)
                cv.circle(x, y, 32, MARK_OUTLINE, True, 3)
                cv.circle(x, y, 28, MARK_FILL, True, 3)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, 4)
                px, py = x, y
                n += 1
            case "right_click":
                x0 = _arg_int(args, kwargs, 0, "x")
                y0 = _arg_int(args, kwargs, 1, "y")
                if x0 is None or y0 is None:
                    continue
                x, y = _norm(int(x0), w), _norm(int(y0), h)
                if px is not None and py is not None and (abs(x - px) + abs(y - py) > 30):
                    cv.line(px, py, x, y, TRAIL_COLOR, 4)
                cv.circle(x, y, 32, MARK_OUTLINE, True, 3)
                cv.circle(x, y, 28, MARK_FILL, True, 3)
                cv.rect(x + 20, y - 36, 16, 16, MARK_TEXT, 3)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, 4)
                px, py = x, y
                n += 1
            case "double_left_click":
                x0 = _arg_int(args, kwargs, 0, "x")
                y0 = _arg_int(args, kwargs, 1, "y")
                if x0 is None or y0 is None:
                    continue
                x, y = _norm(int(x0), w), _norm(int(y0), h)
                if px is not None and py is not None and (abs(x - px) + abs(y - py) > 30):
                    cv.line(px, py, x, y, TRAIL_COLOR, 4)
                cv.circle(x, y, 32, MARK_OUTLINE, True, 3)
                cv.circle(x, y, 28, MARK_FILL, True, 3)
                cv.circle(x, y, 42, MARK_OUTLINE, False, 3)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, 4)
                px, py = x, y
                n += 1
            case "drag":
                x10 = _arg_int(args, kwargs, 0, "x1")
                y10 = _arg_int(args, kwargs, 1, "y1")
                x20 = _arg_int(args, kwargs, 2, "x2")
                y20 = _arg_int(args, kwargs, 3, "y2")
                if None in (x10, y10, x20, y20):
                    continue
                x1, y1 = _norm(int(x10), w), _norm(int(y10), h)
                x2, y2 = _norm(int(x20), w), _norm(int(y20), h)
                if px is not None and py is not None and (abs(x1 - px) + abs(y1 - py) > 30):
                    cv.line(px, py, x1, y1, TRAIL_COLOR, 4)
                cv.circle(x1, y1, 20, MARK_OUTLINE, True, 3)
                cv.circle(x1, y1, 16, MARK_FILL, True, 3)
                _render_number(cv, x1, y1, n, MARK_TEXT, BLACK, 3)
                cv.arrow(x1, y1, x2, y2, MARK_FILL, 6)
                cv.circle(x2, y2, 20, MARK_OUTLINE, False, 4)
                cv.circle(x2, y2, 16, MARK_FILL, False, 3)
                px, py = x2, y2
                n += 1
            case "type":
                if px is None or py is None:
                    continue
                pad = 30
                cv.rect(px - pad, py - pad // 2, pad * 2, pad, MARK_FILL, 4)
                cv.rect(px - pad - 2, py - pad // 2 - 2, pad * 2 + 4, pad + 4, MARK_OUTLINE, 2)
                _render_number(cv, px, py, n, MARK_TEXT, BLACK, 3)
                n += 1
            case _:
                continue


# ---------------------------------------------------------------------------
# Main capture entry point
# ---------------------------------------------------------------------------


def capture(
    actions: list[str],
    width: int,
    height: int,
    marks: bool,
    sandbox: bool,
    sandbox_reset: bool,
) -> tuple[str, list[str]]:
    """Produce a screenshot and return (base64_png, applied_actions).

    In sandbox mode, 'applied_actions' is the subset of 'actions' that were
    actually rendered on the canvas. In real mode, it equals 'actions'
    (all actions are assumed applied since they were sent via SendInput).
    """
    sw, sh = _screen_w, _screen_h
    applied = list(actions)  # default: all applied (real mode)

    if sandbox:
        base = _sandbox_load(sw, sh, sandbox_reset)
        dirty, applied = _sandbox_apply(base, sw, sh, actions, sandbox_reset)
        if dirty:
            _sandbox_save(base, sw, sh)
        rgba = bytearray(base)  # COPY — marks go on the copy, not on base
    else:
        rgba = _bgra_to_rgba(_capture_bgra(sw, sh))

    if marks and actions:
        _apply_marks(rgba, sw, sh, actions)

    dw = sw if width <= 0 else width
    dh = sh if height <= 0 else height
    if (dw, dh) != (sw, sh):
        bgra = _rgba_to_bgra(bytes(rgba))
        bgra2 = _resize_bgra(bgra, sw, sh, dw, dh)
        rgba = _bgra_to_rgba(bgra2)

    png = _encode_png(bytes(rgba), dw, dh)
    b64 = base64.b64encode(png).decode("ascii")
    return b64, applied


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    actions = req.get("actions", [])
    if not isinstance(actions, list):
        actions = []
    actions = [str(a) for a in actions]
    width = int(req.get("width", 0))
    height = int(req.get("height", 0))
    marks = bool(req.get("marks", True))
    sandbox = bool(req.get("sandbox", SANDBOX_DEFAULT))
    sandbox_reset = bool(req.get("sandbox_reset", SANDBOX_RESET_DEFAULT))

    b64, applied = capture(actions, width, height, marks, sandbox, sandbox_reset)

    # Output JSON (protocol change: was raw base64, now structured JSON
    # so execute.py can read the 'applied' list for reconciliation)
    sys.stdout.write(json.dumps({"screenshot_b64": b64, "applied": applied}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
