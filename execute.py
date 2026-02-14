"""
FILENAME: execute.py

SYSTEM: FRANZ — Agentic Visual Loop for Windows 11 (Python 3.13, stdlib only)

BIGGER PICTURE:
    This file is the ACTION EXECUTOR in the FRANZ pipeline. Each turn:
        1. main.py sends the prior VLM output text ("raw") via stdin JSON.
        2. This file parses the ACTIONS section from that text using safe AST
           parsing (no eval — only literal constants are accepted).
        3. For each valid action:
             - If physical_execution=True and sandbox=False: sends real Win32
               SendInput events (mouse moves, clicks, keyboard).
             - Otherwise: the action is recorded as "executed" but no physical
               input is sent. The sandbox canvas in capture.py will render the
               visual effect instead.
        4. The list of executed canonical actions is forwarded to capture.py,
           which produces a screenshot (real or sandbox) with optional marks.
        5. This file returns a JSON result to main.py via stdout.

    The pipeline data flow:
        main.py  ──stdin JSON──►  execute.py  ──stdin JSON──►  capture.py
                 ◄─stdout JSON──             ◄──stdout JSON──

SST GUARANTEE:
    This file NEVER modifies, stores, or re-emits the VLM text ("raw").
    It only READS it to extract action lines. The raw text is owned by
    main.py and forwarded to the VLM as-is. Nothing in this file can
    affect the SST data path.

SANDBOX TRANSPARENCY:
    When sandbox=True, physical_execution is forced to False. Actions are
    still parsed, validated, and reported as "executed" — but no Win32
    input is sent. The capture.py sandbox canvas renders the visual effect
    of each action (white circles, lines, text on a black canvas). From
    the pipeline's perspective, the actions "happened" — the screenshot
    shows the result. The pipeline cannot distinguish sandbox from real.

    One exception: if capture.py reports that an action was not actually
    applied to the canvas (e.g., type() with no prior click position),
    this file reconciles the executed/noted lists so the VLM feedback
    accurately reflects what is visible on screen.

FILE PIPELINE:
    INPUTS (stdin JSON from main.py):
        raw: str                  — prior VLM output text (read-only, for parsing)
        tools: dict[str, bool]    — tool allowlist (which actions are enabled)
        execute: bool             — master gate (False = parse but don't execute)
        physical_execution: bool  — send real Win32 input events
        sandbox: bool             — if True, forces physical_execution=False
        sandbox_reset: bool       — passed through to capture.py
        width, height: int        — output screenshot dimensions
        marks: bool               — draw red visual marks on screenshot

    OUTPUTS (stdout JSON to main.py):
        executed: list[str]       — canonical actions that were accepted AND applied
        noted: list[str]          — ignored/unparsed/gated/unapplied actions
        wants_screenshot: bool    — True if VLM requested screenshot()
        screenshot_b64: str       — base64 PNG from capture.py

RUNTIME:
    - Windows 11, Python 3.13+
    - Stdlib only (ctypes for Win32 SendInput)
"""

import ast
import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Physical input constants
# ---------------------------------------------------------------------------

_MOVE_STEPS: Final[int] = 20
_STEP_DELAY: Final[float] = 0.01
_CLICK_DELAY: Final[float] = 0.12

CAPTURE_SCRIPT: Final[Path] = Path(__file__).parent / "capture.py"

# ---------------------------------------------------------------------------
# Win32 initialization (DPI awareness + screen metrics)
# ---------------------------------------------------------------------------

_shcore: Final[ctypes.WinDLL] = ctypes.WinDLL("shcore", use_last_error=True)
_shcore.SetProcessDpiAwareness(2)
_user32: Final[ctypes.WinDLL] = ctypes.WinDLL("user32", use_last_error=True)

_screen_w: Final[int] = _user32.GetSystemMetrics(0)
_screen_h: Final[int] = _user32.GetSystemMetrics(1)

# ---------------------------------------------------------------------------
# Action language
# ---------------------------------------------------------------------------

KNOWN_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"left_click", "right_click", "double_left_click", "drag", "type", "screenshot", "focus", "click"}
)
ALIASES: Final[dict[str, str]] = {"click": "left_click"}

PHYSICAL_EXECUTION_DEFAULT: Final[bool] = False
SANDBOX_DEFAULT: Final[bool] = False

# ---------------------------------------------------------------------------
# SendInput structures
# ---------------------------------------------------------------------------

INPUT_MOUSE: Final[int] = 0
INPUT_KEYBOARD: Final[int] = 1
MOUSEEVENTF_LEFTDOWN: Final[int] = 0x0002
MOUSEEVENTF_LEFTUP: Final[int] = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final[int] = 0x0008
MOUSEEVENTF_RIGHTUP: Final[int] = 0x0010
MOUSEEVENTF_MOVE: Final[int] = 0x0001
MOUSEEVENTF_ABSOLUTE: Final[int] = 0x8000
KEYEVENTF_KEYUP: Final[int] = 0x0002
KEYEVENTF_UNICODE: Final[int] = 0x0004

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
_user32.SendInput.restype = ctypes.c_uint


# ---------------------------------------------------------------------------
# Win32 SendInput helpers
# ---------------------------------------------------------------------------

def _send_inputs(items: list[INPUT]) -> None:
    n = len(items)
    if n <= 0:
        return
    arr = (INPUT * n)(*items)
    sent = _user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError(ctypes.get_last_error())


def _send_mouse(flags: int, abs_x: int | None = None, abs_y: int | None = None) -> None:
    i = INPUT()
    i.type = INPUT_MOUSE
    dx = 0
    dy = 0
    f = flags
    if abs_x is not None and abs_y is not None:
        dx = abs_x
        dy = abs_y
        f |= MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
    i.u.mi = MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([i])


def _send_unicode_text(text: str) -> None:
    items: list[INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        code = 0x000D if ch == "\n" else ord(ch)
        down = INPUT()
        down.type = INPUT_KEYBOARD
        down.u.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
        up = INPUT()
        up.type = INPUT_KEYBOARD
        up.u.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
        items.append(down)
        items.append(up)
    _send_inputs(items)


# ---------------------------------------------------------------------------
# Coordinate mapping
# ---------------------------------------------------------------------------

def _to_px(v: int, dim: int) -> int:
    v = max(0, min(1000, v))
    return int((v / 1000) * dim)


def _to_abs_65535(x_px: int, y_px: int) -> tuple[int, int]:
    ax = int((x_px / max(1, _screen_w - 1)) * 65535)
    ay = int((y_px / max(1, _screen_h - 1)) * 65535)
    ax = max(0, min(65535, ax))
    ay = max(0, min(65535, ay))
    return ax, ay


def _cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# ---------------------------------------------------------------------------
# Physical input actions
# ---------------------------------------------------------------------------

def _smooth_move(tx_px: int, ty_px: int) -> None:
    sx, sy = _cursor_pos()
    dx, dy = tx_px - sx, ty_px - sy
    for i in range(_MOVE_STEPS + 1):
        t = i / _MOVE_STEPS
        t = t * t * (3.0 - 2.0 * t)  # smoothstep
        x = int(sx + dx * t)
        y = int(sy + dy * t)
        ax, ay = _to_abs_65535(x, y)
        _send_mouse(0, ax, ay)
        time.sleep(_STEP_DELAY)


def _mouse_click(down_flag: int, up_flag: int) -> None:
    _send_mouse(down_flag)
    time.sleep(0.02)
    _send_mouse(up_flag)


def _type_text(text: str) -> None:
    _send_unicode_text(text)


def _do_left_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)


def _do_right_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)


def _do_double_left_click(x: int, y: int) -> None:
    _do_left_click(x, y)
    time.sleep(0.06)
    _do_left_click(x, y)


def _do_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_to_px(x1, _screen_w), _to_px(y1, _screen_h))
    time.sleep(0.08)
    _send_mouse(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    _smooth_move(_to_px(x2, _screen_w), _to_px(y2, _screen_h))
    time.sleep(0.06)
    _send_mouse(MOUSEEVENTF_LEFTUP)


# ---------------------------------------------------------------------------
# VLM text → action parsing (safe AST, literals only)
# ---------------------------------------------------------------------------

def _parse_actions(raw: str) -> list[str]:
    """Extract action lines from VLM output text.

    Primary: looks for an ACTIONS: header and collects lines below it.
    Fallback: if no header found, accepts any line that looks like a
    function call (contains '(' and ends with ')').
    """
    out: list[str] = []
    section = ""
    saw_actions_header = False

    for line in raw.splitlines():
        s = line.strip()
        u = s.upper().rstrip(":")
        if u == "NARRATIVE":
            section = "narrative"
            continue
        if u == "ACTIONS":
            section = "actions"
            saw_actions_header = True
            continue
        if section == "actions" and s:
            out.append(s)

    if saw_actions_header:
        return out

    # Fallback: accept call-like lines even without ACTIONS header
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if "(" in s and s.endswith(")"):
            out.append(s)
    return out


def _parse_call(line: str) -> tuple[str, list[object], dict[str, object]] | None:
    """Parse a single action line via AST.  Returns None if invalid.

    Only accepts: FunctionName(literal_args, key=literal_value)
    Rejects any non-literal expression (no variables, no operators).
    """
    try:
        node = ast.parse(line.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name):
        return None

    name = node.func.id
    if name not in KNOWN_FUNCTIONS:
        return None
    name = ALIASES.get(name, name)

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


# ---------------------------------------------------------------------------
# Argument extraction helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Canonical form (for feedback and capture.py)
# ---------------------------------------------------------------------------

def _canon(name: str, args: list[object], kwargs: dict[str, object]) -> str:
    """Produce a canonical string representation of a parsed action.

    Note: if required args are missing, defaults to zero/empty. This
    canonical form is used for logging and for capture.py to re-parse.
    """
    if name == "type":
        t = _arg_str(args, kwargs, 0, "text")
        if t is None:
            t = ""
        return f"type({json.dumps(t)})"

    if name in ("left_click", "right_click", "double_left_click"):
        x = _arg_int(args, kwargs, 0, "x")
        y = _arg_int(args, kwargs, 1, "y")
        if x is None or y is None:
            x, y = 0, 0
        return f"{name}({int(x)}, {int(y)})"

    if name == "drag":
        x1 = _arg_int(args, kwargs, 0, "x1")
        y1 = _arg_int(args, kwargs, 1, "y1")
        x2 = _arg_int(args, kwargs, 2, "x2")
        y2 = _arg_int(args, kwargs, 3, "y2")
        if None in (x1, y1, x2, y2):
            x1, y1, x2, y2 = 0, 0, 0, 0
        return f"drag({int(x1)}, {int(y1)}, {int(x2)}, {int(y2)})"

    if name in ("screenshot", "focus"):
        return f"{name}()"

    return name + "()"


# ---------------------------------------------------------------------------
# Capture subprocess
# ---------------------------------------------------------------------------

def _run_capture(
    actions: list[str],
    width: int,
    height: int,
    marks: bool,
    sandbox: bool,
    sandbox_reset: bool,
) -> tuple[str, list[str]]:
    """Call capture.py and return (screenshot_b64, applied_actions).

    capture.py returns JSON: {"screenshot_b64": str, "applied": list[str]}
    The "applied" list tells us which actions were actually rendered on the
    canvas (relevant in sandbox mode where e.g. type() may be skipped if
    there's no prior click position).

    On failure: logs to stderr and returns ("", actions) as fallback
    (assumes all actions were applied — this is the safe default for
    non-sandbox mode where capture.py just takes a screenshot).
    """
    payload = json.dumps(
        {
            "actions": actions,
            "width": width,
            "height": height,
            "marks": marks,
            "sandbox": sandbox,
            "sandbox_reset": sandbox_reset,
        }
    )
    r = subprocess.run(
        [sys.executable, str(CAPTURE_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
    )

    # Bug #3 fix: surface capture.py failures
    if r.returncode != 0:
        sys.stderr.write(f"[execute] capture.py failed (rc={r.returncode})\n")
        if r.stderr:
            sys.stderr.write(f"[execute] capture.py stderr:\n{r.stderr[:1000]}\n")
        sys.stderr.flush()

    if not r.stdout:
        sys.stderr.write("[execute] capture.py returned empty stdout\n")
        sys.stderr.flush()
        return "", actions

    try:
        obj = json.loads(r.stdout)
        b64 = str(obj.get("screenshot_b64", ""))
        applied = obj.get("applied", actions)
        if not isinstance(applied, list):
            applied = actions
        return b64, [str(a) for a in applied]
    except json.JSONDecodeError:
        sys.stderr.write(
            f"[execute] capture.py produced invalid JSON: {r.stdout[:200]!r}\n"
        )
        sys.stderr.flush()
        return "", actions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    request = json.loads(sys.stdin.read() or "{}")
    raw = str(request.get("raw", ""))
    tools: dict[str, bool] = (
        request.get("tools", {})
        if isinstance(request.get("tools", {}), dict)
        else {}
    )
    master_execute = bool(request.get("execute", True))
    width = int(request.get("width", 0))
    height = int(request.get("height", 0))
    marks = bool(request.get("marks", True))

    sandbox = bool(request.get("sandbox", SANDBOX_DEFAULT))
    sandbox_reset = bool(request.get("sandbox_reset", False))

    physical_execute = bool(
        request.get("physical_execution", PHYSICAL_EXECUTION_DEFAULT)
    )
    if sandbox:
        physical_execute = False

    executed: list[str] = []
    noted: list[str] = []
    wants_screenshot = False

    for line in _parse_actions(raw):
        parsed = _parse_call(line)
        if parsed is None:
            noted.append(line)
            continue
        name, args, kwargs = parsed
        canon = _canon(name, args, kwargs)

        # screenshot() and focus() are noted, never "executed"
        if name == "screenshot":
            wants_screenshot = True
            noted.append(canon)
            continue
        if name == "focus":
            noted.append(canon)
            continue

        # Gated by master switch or per-tool allowlist
        if (not master_execute) or (not tools.get(name, True)):
            noted.append(canon)
            continue

        try:
            match name:
                case "left_click":
                    x = _arg_int(args, kwargs, 0, "x")
                    y = _arg_int(args, kwargs, 1, "y")
                    if x is None or y is None:
                        noted.append(canon)
                    else:
                        if physical_execute:
                            _do_left_click(x, y)
                        executed.append(canon)
                case "right_click":
                    x = _arg_int(args, kwargs, 0, "x")
                    y = _arg_int(args, kwargs, 1, "y")
                    if x is None or y is None:
                        noted.append(canon)
                    else:
                        if physical_execute:
                            _do_right_click(x, y)
                        executed.append(canon)
                case "double_left_click":
                    x = _arg_int(args, kwargs, 0, "x")
                    y = _arg_int(args, kwargs, 1, "y")
                    if x is None or y is None:
                        noted.append(canon)
                    else:
                        if physical_execute:
                            _do_double_left_click(x, y)
                        executed.append(canon)
                case "drag":
                    x1 = _arg_int(args, kwargs, 0, "x1")
                    y1 = _arg_int(args, kwargs, 1, "y1")
                    x2 = _arg_int(args, kwargs, 2, "x2")
                    y2 = _arg_int(args, kwargs, 3, "y2")
                    if None in (x1, y1, x2, y2):
                        noted.append(canon)
                    else:
                        if physical_execute:
                            _do_drag(int(x1), int(y1), int(x2), int(y2))
                        executed.append(canon)
                case "type":
                    t = _arg_str(args, kwargs, 0, "text")
                    if t is None:
                        noted.append(canon)
                    else:
                        if physical_execute:
                            _type_text(t)
                        executed.append(canon)
                case _:
                    noted.append(canon)
        except Exception:
            noted.append(canon)

    # --- Capture screenshot and reconcile executed vs actually-applied ---
    screenshot_b64, applied = _run_capture(
        executed, width, height, marks, sandbox, sandbox_reset
    )

    # Bug #5 fix: in sandbox mode, capture.py reports which actions it
    # actually rendered. If an action was "executed" here but NOT applied
    # by capture.py (e.g. type() with no cursor position), move it from
    # executed to noted so the VLM feedback accurately reflects what is
    # visible on screen.
    if sandbox:
        applied_set = set(applied)
        actually_executed = [a for a in executed if a in applied_set]
        not_applied = [a for a in executed if a not in applied_set]
        if not_applied:
            noted.extend(not_applied)
            executed = actually_executed

    sys.stdout.write(
        json.dumps(
            {
                "executed": executed,
                "noted": noted,
                "wants_screenshot": wants_screenshot,
                "screenshot_b64": screenshot_b64,
            }
        )
    )
    sys.stdout.flush()


if __name__ == "__main__":
    main()
