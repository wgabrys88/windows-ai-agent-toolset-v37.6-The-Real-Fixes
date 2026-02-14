# FRANZ - Agentic Visual Loop for Windows 11

## Overview

FRANZ is a self-narrative, self-adaptive AI agent loop that gives a Vision-Language Model (VLM) the ability to see and interact with a Windows 11 desktop or a simulated sandbox canvas. The system runs an infinite loop where the VLM observes a screenshot, decides what actions to take, and those actions are executed (or simulated), producing a new screenshot for the next turn.

The core design principle is the **Single Source of Truth (SST)** rule: the raw text output of the VLM is stored verbatim and forwarded to the next VLM call without any modification. The pipeline is a transparent conduit. Python code never rewrites, trims, cleans, truncates, or concatenates the VLM output with other data. This enables the agent to maintain its own narrative memory across turns.

## Files

| File | Role |
|------|------|
| main.py | Orchestrator. Runs the infinite loop, manages state, calls the executor, builds VLM requests, stores the SST. |
| execute.py | Action executor. Parses actions from VLM text using safe AST parsing, optionally sends Win32 input events, delegates to capture.py for screenshots. |
| capture.py | Screenshot producer. Captures real desktop via GDI or renders a persistent sandbox canvas. Draws ephemeral visual marks. Returns base64 PNG. |
| config.py | Hot-reloadable sampling parameters (temperature, top_p, max_tokens). |
| panel.py | Transparent reverse proxy and live dashboard. Sits between main.py and the VLM, logging and verifying SST without affecting traffic. |
| panel.html | Dashboard UI served by panel.py. Shows real-time turn data via Server-Sent Events. |

## Architecture

```
main.py --HTTP POST--> panel.py (localhost:1234) --HTTP POST--> VLM (localhost:1235)
        <--HTTP resp--                            <--HTTP resp--

main.py --stdin JSON--> execute.py --stdin JSON--> capture.py
        <--stdout JSON--           <--stdout JSON--
```

### Data Flow Per Turn

```
1. main.py loads state.story (the VLM output from the previous turn)
2. main.py calls execute.py with the story text
3. execute.py parses ACTIONS from the text using safe AST parsing
4. execute.py optionally sends Win32 input events (physical mode)
5. execute.py calls capture.py with the list of executed actions
6. capture.py produces a screenshot (real desktop or sandbox canvas)
7. capture.py draws red visual marks on a COPY (never persisted)
8. capture.py returns base64 PNG + list of actions actually applied
9. execute.py reconciles executed vs applied, returns JSON to main.py
10. main.py builds the VLM request:
     - messages[0]: system prompt
     - messages[1]: the SST (previous VLM output, VERBATIM)
     - messages[2]: executor feedback + screenshot image
11. main.py sends the request to the VLM (via panel.py if present)
12. The VLM responds with new text
13. main.py stores the response AS-IS as the new state.story
14. Repeat
```

## Single Source of Truth (SST) Rule

This is the most important rule in the entire system.

**The raw text received from the VLM is forwarded to the next VLM call without ANY change.**

No trimming. No cleaning. No truncation. No encoding change. No concatenation with other data. If the VLM produces malformed, empty, or nonsensical output, it is forwarded as-is. The pipeline is intentionally transparent.

The SST is enforced structurally:

- The VLM output is stored in `state.story` via direct assignment
- It is persisted to `state.json` via JSON serialization (which preserves string content)
- It is loaded back via JSON deserialization (identity round-trip)
- It is placed into `messages[1].text` via direct assignment
- It is sent over HTTP via `json.dumps` (which preserves string content)
- Executor feedback is ALWAYS in `messages[2]`, never in `messages[1]`
- No code anywhere in the pipeline transforms the SST text

### SST Verification Chain

```
VLM HTTP response
  --> body["choices"][0]["message"]["content"]  (direct access)
  --> raw variable                              (direct assignment)
  --> state.story                               (direct assignment)
  --> state.json on disk                        (json.dumps preserves strings)
  --> state.story on load                       (json.loads restores strings)
  --> prev_story                                (direct assignment)
  --> messages[1].text                          (direct insertion)
  --> HTTP POST body                            (json.dumps preserves strings)
  --> VLM receives it                           (byte-identical)
```

Every link is either direct assignment or JSON serialization, both of which preserve string content exactly. The SST guarantee holds for all content including Unicode, newlines, tabs, JSON-special characters, and even null bytes.

## Sandbox Mode

When `SANDBOX=True` in main.py, the system operates on a persistent black canvas instead of the real desktop. Three injection points achieve transparent simulation:

**Injection 1: Physical execution suppression (execute.py)**

When sandbox is true, `physical_execute` is forced to False. Actions are parsed, validated, and reported as "executed" but no Win32 SendInput events are sent.

**Injection 2: Screenshot source replacement (capture.py)**

Instead of capturing the real screen via GDI, the screenshot comes from `sandbox_canvas.bmp`, a persistent black canvas that accumulates white drawings.

**Injection 3: Canvas as action effect renderer (capture.py)**

Each action produces a visible change on the canvas:

| Action | Visual Effect |
|--------|--------------|
| left_click(x, y) | White circle at (x, y) |
| right_click(x, y) | White rectangle at (x, y) |
| double_left_click(x, y) | White circle at (x, y) |
| drag(x1, y1, x2, y2) | White line from start to end |
| type("text") | White text at the last click position |
| screenshot() | No canvas change (noted, not executed) |

The pipeline cannot distinguish sandbox from real mode at the data level. The feedback format, screenshot format, VLM request structure, and SST handling are all mode-agnostic. The only distinguishing signal is the image content itself (black canvas vs real desktop), which is intentionally communicated to the VLM via the system prompt.

### Marks vs Sandbox Drawings

- **Sandbox drawings (white):** PERSISTENT. Written to `sandbox_canvas.bmp`. Accumulate across turns.
- **Red marks (numbered circles, arrows):** EPHEMERAL. Drawn on a copy of the image. Never saved to the canvas. Help the VLM see what was executed.

## Action Language

Allowed tools with canonical names:

```
left_click(x, y)
right_click(x, y)
double_left_click(x, y)
drag(x1, y1, x2, y2)
type("text")
screenshot()
```

Coordinates are integers 0 to 1000 in normalized space (0,0 is top-left, 1000,1000 is bottom-right).

The executor supports:

- Keyword arguments: `left_click(x=300, y=400)`
- The `click()` alias: `click(x, y)` is equivalent to `left_click(x, y)`
- Mixed positional and keyword: `drag(100, 200, x2=800, y2=600)`

All parsing uses `ast.parse` with `ast.Constant` validation. No `eval()`, no `exec()`, no code execution from VLM output. Only literal constants are accepted as arguments.

## Panel (Transparent Proxy and Dashboard)

`panel.py` is an optional Wireshark-style transparent reverse proxy that sits between main.py and the upstream VLM. It provides the only external verification of SST integrity.

### Setup

1. Move LM Studio (or your VLM server) to port 1235
2. Start panel.py (it listens on port 1234, where main.py sends requests)
3. Start main.py as usual (no code changes needed)
4. Open `http://127.0.0.1:8080/` in your browser for the live dashboard

### What Panel Does

- Receives the full HTTP request from main.py (raw bytes)
- Parses a COPY for inspection (never touches the original bytes)
- Forwards the ORIGINAL bytes to the upstream VLM
- Receives the full HTTP response from the VLM (raw bytes)
- Parses a COPY for inspection
- Forwards the ORIGINAL bytes back to main.py
- Performs SST verification: compares messages[1].text to the previous response
- Pushes live data to the HTML dashboard via Server-Sent Events
- Writes per-turn JSON logs to `panel_log/`

### Transparency Guarantee

Panel.py NEVER modifies the bytes flowing through it. It forwards raw bytes, not re-serialized JSON. The main.py to VLM channel is byte-identical with or without panel.py in the path. Removing panel.py and pointing main.py directly at the VLM produces identical behavior.

### Dashboard Features

- Real-time turn cards appearing as the pipeline runs
- SST text (user message number 1) with length indicator
- Feedback text (user message number 2)
- VLM response text with the note that it becomes the next SST
- Green SST badge when SST matches the previous VLM response
- Red SST badge with diff details if SST is violated
- Latency, token usage, model name, sampling parameters
- Expandable/collapsible turn cards
- Auto-expand latest, auto-scroll, newest-first ordering
- Clear display button

## Configuration

`config.py` holds three sampling parameters that main.py hot-reloads each turn via `importlib.reload`. You can edit these while the pipeline is running and they take effect on the next turn.

```
TEMPERATURE = 0.3    (0.0 = deterministic, 1.0+ = creative)
TOP_P = 0.95         (nucleus sampling cutoff, 0.0 to 1.0)
MAX_TOKENS = 300     (hard cap on VLM response length in tokens)
```

If config.py has a syntax error at reload time, main.py catches the exception, logs a warning, and keeps the previous values. The pipeline is never interrupted.

### Tuning Guidance

- TEMPERATURE 0.2 to 0.4: consistent action syntax and focused behavior
- TEMPERATURE 0.6 and above: more exploratory, may produce novel narratives but also more malformed actions
- MAX_TOKENS 300: sufficient for NARRATIVE plus 3 to 5 action lines. Increase if the VLM frequently truncates mid-action
- TOP_P 0.95: standard. Lower values like 0.8 make output more predictable

## Runtime Requirements

- Windows 11
- Python 3.13 or later
- Standard library only (no pip dependencies)
- LM Studio or compatible OpenAI API server running locally

## Quick Start

```
1. Start LM Studio with Qwen3-VL model on port 1235
2. python panel.py          (optional, for monitoring)
3. python main.py           (starts the agent loop)
4. Open http://127.0.0.1:8080 in browser (if panel.py is running)
```

Without panel.py, configure LM Studio on port 1234 instead and run main.py directly.

## Debug Artifacts

When `DEBUG_DUMP=True` (default), main.py writes per-turn artifacts to `dump/run_YYYYMMDD_HHMMSS/`:

- `turn_XXXX.json`: turn metadata (story, VLM raw output, executed/noted actions, timestamps)
- `turn_XXXX.png`: the screenshot that was sent to the VLM

## Error Handling

The pipeline is designed to survive gracefully:

- If execute.py crashes: main.py logs the error, continues with empty executor result
- If capture.py crashes: execute.py logs the error, returns empty screenshot
- If config.py has a syntax error: main.py keeps previous values, logs warning
- If the VLM is unreachable: main.py retries with exponential backoff (5 attempts)
- If the VLM returns nonsense: the pipeline forwards it as-is (SST rule)
- All fatal crashes produce full tracebacks to stderr

---

## AI Assistant Prompt

The following prompt should be provided to any AI assistant (ChatGPT, Claude, etc.) when asking for help with this codebase. Copy and paste it as the first message in a new conversation, followed by the source files.

---

SYSTEM RULES FOR WORKING ON THE FRANZ PROJECT

You are assisting with FRANZ, an agentic visual loop for Windows 11. Before making any changes, read and internalize these absolute rules.

RULE 1 - SINGLE SOURCE OF TRUTH (SST) IS SACRED

The raw text output of the VLM is stored verbatim and forwarded to the next VLM call as user message number 1 without ANY modification. No trimming, no cleaning, no truncation, no encoding change, no concatenation with other data. If the VLM produces malformed, empty, or nonsensical output, it is forwarded AS-IS. Python code in the pipeline NEVER rewrites the VLM output text. This is the foundation of the agent's self-narrative memory.

SST is enforced structurally: the VLM output is always user message number 1, executor feedback is always user message number 2. They are NEVER merged. Any proposed change that would modify, filter, slice, or concatenate the SST text is REJECTED.

RULE 2 - FEEDBACK IS ALWAYS A SEPARATE MESSAGE

Any context added by the executor (executed actions, ignored actions, debug info) goes into user message number 2. It is NEVER concatenated into user message number 1 (the SST). The VLM request always has exactly three messages: system, user number 1 (SST), user number 2 (feedback plus image).

RULE 3 - SAFE PARSING ONLY

All action parsing uses ast.parse with ast.Constant validation. No eval, no exec, no code execution from VLM output. Only function calls with literal constant arguments are accepted. Any proposed change that introduces eval or exec or any form of dynamic code execution from VLM text is REJECTED.

RULE 4 - SANDBOX TRANSPARENCY

The sandbox must be invisible at the pipeline data level. The feedback format, screenshot format, and VLM request structure must be identical in sandbox and real mode. The pipeline code path must be the same. Only the image source and physical execution differ. Any proposed change that makes the pipeline behave differently based on sandbox mode at the data routing level is REJECTED.

RULE 5 - PANEL TRANSPARENCY

panel.py is a transparent reverse proxy. It NEVER modifies the bytes flowing through it. It forwards raw bytes, not re-serialized JSON. Any proposed change to panel.py that re-serializes the request or response JSON instead of forwarding original bytes is REJECTED.

RULE 6 - STDLIB ONLY

The project uses Python 3.13 standard library only. No pip dependencies unless explicitly approved by the user. ctypes is used for Win32 API access.

RULE 7 - CONSISTENCY ACROSS FILES

Any change to action syntax must be reflected in execute.py (parsing), capture.py (rendering and marks), and the system prompt in main.py. All three must agree on function names, argument types, and coordinate space.

RULE 8 - ALWAYS SCAN ALL FILES FIRST

Before proposing changes, read all provided source files completely. Understand the data flow. Identify which file owns which data. Never assume SST is handled elsewhere.

RULE 9 - OUTPUT REQUIREMENTS

When modifying any file, include a brief pipeline impact note explaining what input the file consumes and what output it produces. Provide complete corrected files or unified diffs that apply cleanly. If adding features, include a docstring explaining usage and data flow.

RULE 10 - THE VLM IS FREE

The VLM output is the agent's own narrative. The pipeline must never interfere with it. If the VLM outputs garbage, the pipeline forwards garbage. If the VLM outputs a refusal, the pipeline forwards the refusal. If the VLM outputs something brilliant, the pipeline forwards it. The pipeline is a transparent conduit, not a filter.

END OF RULES

After pasting this prompt, provide all source files and describe what you need help with.

---

## Technical Architecture Report

### System Components

```
main.py      ORCHESTRATOR      Owns the SST, runs the loop
execute.py   ACTION EXECUTOR   Reads SST for parsing, never writes it
capture.py   SCREENSHOT MAKER  Has no access to SST, receives canonical actions only
config.py    TUNING SURFACE    Cannot affect SST or data routing
panel.py     EXTERNAL OBSERVER Forwards raw bytes, verifies SST from outside
panel.html   DASHBOARD UI      Pure display, no pipeline interaction
```

### Process Model

```
main.py          long-lived process, runs the infinite loop
execute.py       short-lived subprocess, one invocation per turn
capture.py       short-lived subprocess, called by execute.py
config.py        imported module, hot-reloaded each turn
panel.py         long-lived process, independent of the pipeline
```

### Data Ownership Boundaries

```
main.py          OWNS state.story (the SST). Reads it, forwards it, stores new value.
                 NEVER modifies the text between receiving it and forwarding it.

execute.py       READS the raw VLM text to extract action lines.
                 NEVER modifies, stores, or re-emits the VLM text.
                 Returns only structured feedback (executed, noted, screenshot).

capture.py       HAS NO ACCESS to the VLM text.
                 Receives only canonical action strings.
                 Returns only screenshot data and applied action list.

panel.py         OBSERVES the raw bytes on the wire.
                 NEVER modifies them. Parses copies for display.
                 Independently verifies SST by comparing turns.
```

### Detailed Data Flow With Example Data

Turn N begins. The state contains the VLM output from turn N-1:

```
state.story = "NARRATIVE:\nI see a black canvas. I will click in the center.\n\nACTIONS:\nleft_click(500, 500)\nscreenshot()"
```

Step 1: main.py sets prev_story to state.story (direct assignment, no transform).

Step 2: main.py calls execute.py with the story text via subprocess stdin:

```json
{"raw": "NARRATIVE:\nI see a black canvas...", "tools": {"left_click": true, ...}, "execute": true, "sandbox": true, "physical_execution": false, "width": 512, "height": 288, "marks": true}
```

Step 3: execute.py parses the ACTIONS section:

```
_parse_actions finds "ACTIONS:" header
Extracts: ["left_click(500, 500)", "screenshot()"]

"left_click(500, 500)" -> ast.parse -> name="left_click", args=[500, 500]
  -> canonical: "left_click(500, 500)"
  -> physical_execute=False (sandbox) -> no Win32 input sent
  -> executed.append("left_click(500, 500)")

"screenshot()" -> name="screenshot"
  -> wants_screenshot=True
  -> noted.append("screenshot()")
```

Step 4: execute.py calls capture.py with executed actions:

```json
{"actions": ["left_click(500, 500)"], "sandbox": true, "marks": true, "width": 512, "height": 288}
```

Step 5: capture.py processes the action on the sandbox canvas:

```
Load sandbox_canvas.bmp (1920x1080 persistent black canvas)
_sandbox_apply: "left_click(500, 500)"
  -> px=960, py=540 (mapped to screen resolution)
  -> draw white circle radius 6 at (960, 540)
  -> update sandbox_state.json: {"last_x": 960, "last_y": 540}
  -> applied.append("left_click(500, 500)")
Save modified BMP (persistent)
Copy buffer for marks (ephemeral)
Draw red mark number 1 at (960, 540) on the copy
Resize 1920x1080 to 512x288 via GDI StretchBlt
Encode PNG, return base64
```

Step 6: capture.py returns JSON to execute.py:

```json
{"screenshot_b64": "iVBOR...", "applied": ["left_click(500, 500)"]}
```

Step 7: execute.py reconciles and returns to main.py:

```
applied_set = {"left_click(500, 500)"}
executed intersect applied = ["left_click(500, 500)"] (match, no changes needed)
```

```json
{"executed": ["left_click(500, 500)"], "noted": ["screenshot()"], "wants_screenshot": true, "screenshot_b64": "iVBOR..."}
```

Step 8: main.py builds the feedback string (user message number 2):

```
EXECUTOR_FEEDBACK:
executed=["left_click(500, 500)"]
ignored=["screenshot()"]
```

Step 9: main.py builds the VLM request:

```
messages[0]: system prompt (fixed)
messages[1]: {"type": "text", "text": "NARRATIVE:\nI see a black canvas..."}  <- SST, VERBATIM
messages[2]: {"type": "text", "text": "EXECUTOR_FEEDBACK:\n..."} + {"type": "image_url", ...}
```

Step 10: If panel.py is running, it intercepts the request on port 1234, logs it, verifies SST matches the previous response, and forwards the original raw bytes to the VLM on port 1235.

Step 11: The VLM responds with new text. Panel.py logs the response and forwards original raw bytes back to main.py.

Step 12: main.py stores the response as-is:

```
state.story = "NARRATIVE:\nA white dot appeared at the center..."
```

Turn N+1 begins with this new story as the SST.

### SST Verification by Panel

Panel.py stores the VLM response text from each turn. On the next turn, it extracts messages[1].text from the request and compares it to the stored response. If they differ, it logs a SST VIOLATION with the exact position and content of the divergence. The request is still forwarded unchanged. This is a read-only check.

### Edge Case Handling

**VLM refuses to cooperate:** The refusal text is forwarded as SST. No actions are parsed. The canvas is unchanged. The pipeline continues.

**VLM output is truncated mid-action:** Complete actions are executed. Truncated actions fail AST parsing and are noted. The truncated text is forwarded as SST.

**VLM outputs dangerous-looking code:** AST parsing rejects anything that is not a simple function call with literal arguments. No code execution occurs. The text is forwarded as SST.

**VLM output is completely empty:** Empty string is a valid SST value. It is forwarded as an empty text block in messages[1]. The pipeline behaves like a cold start.

**type() with no prior click position:** capture.py skips the draw (no cursor position). The action is NOT in the applied list. execute.py moves it from executed to noted. The VLM feedback accurately reports it as ignored.

**execute.py crashes:** main.py logs the error, continues with empty executor result and empty screenshot. The SST is still forwarded. The pipeline survives in degraded mode.

**capture.py crashes:** execute.py logs the error, returns empty screenshot. Same degraded survival.

**config.py has a syntax error:** main.py catches the reload exception, keeps previous values, logs a warning. The pipeline continues with the old sampling parameters.

## License

This project is provided as-is for research and experimentation purposes.
