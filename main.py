"""
FILENAME: main.py

SYSTEM: FRANZ — Agentic Visual Loop for Windows 11 (Python 3.13, stdlib only)

BIGGER PICTURE:
    FRANZ is a self-narrative, self-adaptive AI agent loop. Each turn:
        1. The prior VLM output (the "story") is loaded from state.
        2. The story is sent to execute.py, which parses any ACTIONS from it,
           optionally performs them (physically or in sandbox), and returns a
           screenshot plus feedback about what was executed/ignored.
        3. This file builds a VLM request with THREE messages:
             [0] system  — fixed instruction prompt
             [1] user #1 — the EXACT prior VLM output text (SST, see below)
             [2] user #2 — executor feedback text + current screenshot image
        4. The VLM responds with new text (narrative + actions).
        5. That raw response is stored VERBATIM as the new story for next turn.
        6. Repeat forever.

    Other pipeline files:
        execute.py  — parses actions from VLM text, executes or simulates them,
                       delegates to capture.py for the screenshot.
        capture.py  — produces a screenshot (real desktop or sandbox canvas),
                       draws visual marks, returns base64 PNG.
        config.py   — hot-reloadable sampling parameters (temperature, etc.).
        panel.py    — optional Wireshark-like proxy/UI; not required for pipeline.

SINGLE SOURCE OF TRUTH (SST) — ABSOLUTE RULE:
    The text received from the VLM MUST be forwarded to the next VLM call
    without ANY modification — no trimming, no cleaning, no truncation, no
    encoding change, no concatenation with other data. If the VLM produces
    malformed, empty, or nonsensical output, it is forwarded AS-IS. The
    pipeline is intentionally transparent: Python only reads the text to
    extract actions (in execute.py); it never rewrites it. This is the
    foundation of the agent's self-narrative: the model's own output is its
    memory, and the pipeline must never interfere with it.

    SST is enforced structurally: the story is always user message #1,
    executor feedback is always user message #2. They are never merged.

FILE PIPELINE:
    INPUTS:
        - state.story  — prior raw VLM output text (verbatim, from state.json)
        - execute.py   — returns: executed[], noted[], screenshot_b64
        - config.py    — TEMPERATURE, TOP_P, MAX_TOKENS (hot-reloaded each turn)
    OUTPUTS:
        - stdout       — the next raw VLM output text (verbatim, for monitoring)
        - state.json   — persisted pipeline state for crash recovery
        - dump/        — optional per-turn artifacts (JSON + PNG)

RUNTIME:
    - Windows 11, Python 3.13+
    - Stdlib only (no pip dependencies)
"""

import base64
import importlib
import json
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import config as franz_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API: Final[str] = "http://localhost:1234/v1/chat/completions"
MODEL: Final[str] = "qwen3-vl-2b-instruct-1m"

WIDTH: Final[int] = 512
HEIGHT: Final[int] = 288
VISUAL_MARKS: Final[bool] = True
LOOP_DELAY: Final[float] = 1.0
EXECUTE_ACTIONS: Final[bool] = True

SANDBOX: Final[bool] = True
SANDBOX_RESET: Final[bool] = False
PHYSICAL_EXECUTION: Final[bool] = False
DEBUG_DUMP: Final[bool] = True

EXECUTE_SCRIPT: Final[Path] = Path(__file__).parent / "execute.py"
SANDBOX_CANVAS: Final[Path] = Path(__file__).parent / "sandbox_canvas.bmp"
STATE_FILE: Final[Path] = Path(__file__).parent / "state.json"

SYSTEM_PROMPT: Final[str] = """\
You control a Windows 11 desktop using these functions:
left_click(x,y), right_click(x,y), double_left_click(x,y), drag(x1,y1,x2,y2), type(text), screenshot(), click(x,y).
Coordinates are integers in 0..1000 relative to the current screenshot (0,0 top-left; 1000,1000 bottom-right).
Marks on the screenshot show actions that were actually executed.

SANDBOX MODE NOTE:
If the image looks like a black canvas (not a real desktop), you are in sandbox mode.
In sandbox mode:
- drag draws persistent white lines
- left_click (or click) places a small white circle
- right_click places a small white rectangle
- type(text) draws white text at the most recent click location

Reply in exactly two sections:

NARRATIVE:
Briefly describe what you will do next and ask any needed questions. No coordinates here.

ACTIONS:
One function call per line. No extra text. Use screenshot() whenever you need a fresh view.
If you have nothing else to do, output screenshot().
""".strip()


# ---------------------------------------------------------------------------
# Tool config
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ToolConfig:
    left_click: bool = True
    right_click: bool = True
    double_left_click: bool = True
    drag: bool = True
    type: bool = True
    screenshot: bool = True
    click: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "left_click": self.left_click,
            "right_click": self.right_click,
            "double_left_click": self.double_left_click,
            "drag": self.drag,
            "type": self.type,
            "screenshot": self.screenshot,
            "click": self.click,
        }


TOOLS: Final[ToolConfig] = ToolConfig()


# ---------------------------------------------------------------------------
# Pipeline state (persisted to state.json between turns)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PipelineState:
    story: str = ""
    turn: int = 0


def _load_state() -> PipelineState:
    """Load pipeline state from disk.  Returns defaults on any failure."""
    try:
        o = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(o, dict):
            st = PipelineState()
            st.story = str(o.get("story", ""))
            st.turn = int(o.get("turn", 0))
            return st
    except Exception:
        pass
    return PipelineState()


def _save_state(
    st: PipelineState,
    prev_story: str,
    raw: str,
    executor_result: dict[str, object],
) -> None:
    """Persist current pipeline state.  Best-effort — never crashes the loop."""
    try:
        out = {
            "turn": st.turn,
            "story": st.story,
            "prev_story": prev_story,
            "vlm_raw": raw,
            "executed": executor_result.get("executed", []),
            "noted": executor_result.get("noted", []),
            "wants_screenshot": executor_result.get("wants_screenshot", False),
            "execute_actions": EXECUTE_ACTIONS,
            "tools": TOOLS.to_dict(),
            "timestamp": datetime.now().isoformat(),
        }
        STATE_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sampling parameters (hot-reloaded from config.py each turn)
# ---------------------------------------------------------------------------

def _sampling_dict() -> dict[str, float | int]:
    return {
        "temperature": float(franz_config.TEMPERATURE),
        "top_p": float(franz_config.TOP_P),
        "max_tokens": int(franz_config.MAX_TOKENS),
    }


# ---------------------------------------------------------------------------
# VLM inference
# ---------------------------------------------------------------------------

def _infer(screenshot_b64: str, prev_story: str, feedback: str) -> str:
    """Send a request to the VLM and return its raw text response.

    Message layout (SST guarantee):
        messages[0] — system prompt (fixed)
        messages[1] — user #1: prev_story, forwarded VERBATIM (SST)
        messages[2] — user #2: executor feedback text + screenshot image
    """
    payload: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            # --- SST message: prior VLM output, byte-for-byte unchanged ---
            {"role": "user", "content": [{"type": "text", "text": prev_story}]},
            # --- Executor feedback + fresh screenshot (separate message) ---
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": feedback},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screenshot_b64}",
                        },
                    },
                ],
            },
        ],
        **_sampling_dict(),
    }

    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API, body_bytes, {"Content-Type": "application/json"}
    )

    delay = 0.5
    last_err: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body: dict[str, object] = json.load(resp)
            return body["choices"][0]["message"]["content"]  # type: ignore[index,return-value]
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
        ) as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2.0, 8.0)

    raise RuntimeError(f"VLM request failed after retries: {last_err}")


# ---------------------------------------------------------------------------
# Executor subprocess
# ---------------------------------------------------------------------------

def _run_executor(raw: str) -> dict[str, object]:
    """Call execute.py as a subprocess and return its JSON result.

    On failure (crash, bad JSON), logs to stderr and returns a safe empty
    dict so the pipeline can continue.
    """
    executor_input = json.dumps(
        {
            "raw": raw,
            "tools": TOOLS.to_dict(),
            "execute": EXECUTE_ACTIONS,
            "physical_execution": PHYSICAL_EXECUTION,
            "sandbox": SANDBOX,
            "sandbox_reset": SANDBOX_RESET,
            "width": WIDTH,
            "height": HEIGHT,
            "marks": VISUAL_MARKS,
        }
    )
    result = subprocess.run(
        [sys.executable, str(EXECUTE_SCRIPT)],
        input=executor_input,
        capture_output=True,
        text=True,
    )

    # --- Bug #2 fix: surface executor failures instead of silent swallow ---
    if result.returncode != 0:
        sys.stderr.write(
            f"[main] execute.py failed (rc={result.returncode})\n"
        )
        if result.stderr:
            sys.stderr.write(
                f"[main] execute.py stderr:\n{result.stderr[:1000]}\n"
            )
        sys.stderr.flush()

    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        sys.stderr.write(
            f"[main] execute.py produced invalid JSON: {result.stdout[:200]!r}\n"
        )
        sys.stderr.flush()
        return {}


# ---------------------------------------------------------------------------
# Debug dump
# ---------------------------------------------------------------------------

def _dump(
    dump_dir: Path,
    turn: int,
    prev_story: str,
    raw: str,
    executor_result: dict[str, object],
) -> None:
    screenshot_b64 = str(executor_result.get("screenshot_b64", ""))
    if screenshot_b64:
        try:
            (dump_dir / f"turn_{turn:04d}.png").write_bytes(
                base64.b64decode(screenshot_b64)
            )
        except Exception:
            pass

    run_state = {
        "turn": turn,
        "story": prev_story,
        "vlm_raw": raw,
        "executed": executor_result.get("executed", []),
        "noted": executor_result.get("noted", []),
        "wants_screenshot": executor_result.get("wants_screenshot", False),
        "execute_actions": EXECUTE_ACTIONS,
        "tools": TOOLS.to_dict(),
        "timestamp": datetime.now().isoformat(),
    }
    (dump_dir / f"turn_{turn:04d}.json").write_text(
        json.dumps(run_state, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    dump_dir: Path | None = None
    if DEBUG_DUMP:
        dump_dir = Path("dump") / datetime.now().strftime("run_%Y%m%d_%H%M%S")
        dump_dir.mkdir(parents=True, exist_ok=True)

    # Ensure sandbox canvas exists before first real turn
    if SANDBOX and not SANDBOX_CANVAS.is_file():
        _run_executor("")

    time.sleep(1.0)

    state = _load_state()

    while True:
        state.turn += 1

        # Hot-reload sampling config (tolerant of syntax errors in config.py)
        try:
            importlib.reload(franz_config)
        except Exception as reload_err:
            sys.stderr.write(
                f"[main] config.py reload failed, keeping previous values: "
                f"{reload_err}\n"
            )
            sys.stderr.flush()

        # ---- prev_story is the SST: NEVER modify it ----
        prev_story = state.story

        # Run executor: parses actions from prev_story, returns feedback + screenshot
        executor_result = _run_executor(prev_story)
        screenshot_b64 = str(executor_result.get("screenshot_b64", ""))

        # Build executor feedback (separate from SST, goes into user message #2)
        executed = executor_result.get("executed", [])
        noted = executor_result.get("noted", [])
        # Bug #1 fix: use real newlines, not escaped \\n
        feedback = (
            "EXECUTOR_FEEDBACK:\n"
            "executed=" + json.dumps(executed) + "\n"
            "ignored=" + json.dumps(noted)
        )

        # Call VLM — prev_story forwarded VERBATIM as user message #1
        raw = _infer(screenshot_b64, prev_story, feedback)

        # Emit raw VLM output to stdout (for monitoring / panel consumption)
        sys.stdout.write(raw)
        sys.stdout.flush()

        # Optional debug artifacts
        if dump_dir is not None:
            _dump(dump_dir, state.turn, prev_story, raw, executor_result)

        # Store the raw VLM output AS-IS for next turn (SST)
        state.story = raw
        _save_state(state, prev_story, raw, executor_result)

        time.sleep(LOOP_DELAY)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()
    # Bug #4 fix: print full traceback instead of silent exit
    except Exception:
        traceback.print_exc()
        sys.exit(1)
