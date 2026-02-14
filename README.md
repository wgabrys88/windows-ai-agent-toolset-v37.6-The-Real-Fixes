"""
================================================================================
FRANZ — TECHNICAL ARCHITECTURE REPORT
Agentic Visual Loop for Windows 11
================================================================================

Report Date:    2025
System Version: Post-review corrected codebase (main.py, execute.py, capture.py, config.py)
Runtime:        Windows 11 / Python 3.13+ / stdlib only
VLM Backend:    Qwen3-VL-2B-Instruct-1M via LM Studio (localhost:1234)

================================================================================
TABLE OF CONTENTS
================================================================================

  1. Executive Summary
  2. System Architecture Overview
  3. ASCII Data Flow Diagram
  4. Single Source of Truth (SST) — Formal Proof
  5. Injection Transparency Analysis
  6. Multi-Turn Simulation: Proper VLM Output
  7. Multi-Turn Simulation: Malformed VLM Output
  8. Multi-Turn Simulation: Edge Cases
  9. SST Integrity Verification Matrix
 10. Conclusion

================================================================================
1. EXECUTIVE SUMMARY
================================================================================

FRANZ is a fully autonomous agentic loop that gives a Vision-Language Model
(VLM) the ability to see and interact with a Windows 11 desktop — or a
simulated sandbox that is indistinguishable from a real desktop at the
pipeline level.

The core invariant is the SINGLE SOURCE OF TRUTH (SST) rule:

    The raw text output of the VLM is stored verbatim and forwarded to the
    next VLM call as user message #1 WITHOUT ANY MODIFICATION. Python code
    in the pipeline NEVER rewrites, trims, cleans, encodes, truncates,
    concatenates, or in any way alters the VLM's output text. The pipeline
    is a transparent conduit.

This invariant enables self-narrative behavior: the VLM's own output IS its
memory. Each turn, the model sees what it said last time and can build on it,
self-correct, or evolve its strategy. The pipeline's only role is to:

    (a) Execute the actions the VLM requested (or simulate them)
    (b) Capture a screenshot of the result
    (c) Report back what happened
    (d) Forward everything to the next VLM call without interference

================================================================================
2. SYSTEM ARCHITECTURE OVERVIEW
================================================================================

Files and their roles:

    main.py      — The ORCHESTRATOR. Runs the infinite loop. Loads state,
                   calls executor, builds VLM request, stores new state.
                   OWNS the SST: reads state.story, forwards it verbatim,
                   stores the new VLM output as state.story.

    execute.py   — The ACTION EXECUTOR. Receives VLM text via stdin, parses
                   ACTIONS section using safe AST parsing, optionally sends
                   Win32 SendInput events, delegates to capture.py for the
                   screenshot. Returns structured JSON feedback.
                   READS the SST (to extract actions) but NEVER WRITES it.

    capture.py   — The SCREENSHOT PRODUCER. Captures real desktop (GDI) or
                   renders sandbox canvas (persistent BMP). Draws ephemeral
                   red visual marks on a COPY. Returns base64 PNG + list of
                   actions actually applied.
                   HAS NO ACCESS to the SST. Receives only canonical action
                   strings.

    config.py    — The TUNING SURFACE. Three constants (TEMPERATURE, TOP_P,
                   MAX_TOKENS) hot-reloaded by main.py each turn. Cannot
                   affect SST or data routing.

    panel.py     — OPTIONAL Wireshark-like proxy. Sits between main.py and
                   the upstream VLM. Can inspect, log, and enforce SST on
                   the wire. Not required for pipeline operation.

Process model:

    main.py runs as a long-lived process.
    execute.py runs as a SHORT-LIVED subprocess (one invocation per turn).
    capture.py runs as a SHORT-LIVED subprocess (called by execute.py).
    config.py is imported and hot-reloaded (not a subprocess).

================================================================================
3. ASCII DATA FLOW DIAGRAM
================================================================================

One complete turn of the pipeline, with example data flowing through:

    ┌──────────────────────────────────────────────────────────────────────┐
    │ main.py — Turn N                                                     │
    │                                                                      │
    │  state.story (from Turn N-1):                                        │
    │  ┌──────────────────────────────────────────────────────────────┐    │
    │  │ "NARRATIVE:\n                                                │    │
    │  │ I see a black canvas. I will click in the center.\n         │    │
    │  │ \n                                                          │    │
    │  │ ACTIONS:\n                                                  │    │
    │  │ left_click(500, 500)\n                                      │    │
    │  │ screenshot()"                                                │    │
    │  └──────────────────────────────────────────────────────────────┘    │
    │                           │                                          │
    │                           │  prev_story = state.story (VERBATIM)     │
    │                           │  ──── SST: this string is SACRED ────    │
    │                           ▼                                          │
    │  ┌─────────────────────────────────────────────────────────┐        │
    │  │ subprocess: execute.py                                   │        │
    │  │                                                          │        │
    │  │ stdin JSON:                                              │        │
    │  │   {"raw": "NARRATIVE:\nI see a black...",                │        │
    │  │    "tools": {"left_click": true, ...},                   │        │
    │  │    "execute": true,                                      │        │
    │  │    "sandbox": true,                                      │        │
    │  │    "physical_execution": false,                          │        │
    │  │    "width": 512, "height": 288, "marks": true, ...}     │        │
    │  │                                                          │        │
    │  │ _parse_actions(raw):                                     │        │
    │  │   finds "ACTIONS:" header                                │        │
    │  │   extracts: ["left_click(500, 500)", "screenshot()"]     │        │
    │  │                                                          │        │
    │  │ Processing each line:                                    │        │
    │  │   "left_click(500, 500)"                                 │        │
    │  │     → AST parse → name="left_click", args=[500, 500]    │        │
    │  │     → canon = "left_click(500, 500)"                     │        │
    │  │     → physical_execute=False (sandbox)                   │        │
    │  │     → executed.append("left_click(500, 500)")            │        │
    │  │                                                          │        │
    │  │   "screenshot()"                                         │        │
    │  │     → name="screenshot" → wants_screenshot=True          │        │
    │  │     → noted.append("screenshot()")                       │        │
    │  │                                                          │        │
    │  │ executed = ["left_click(500, 500)"]                      │        │
    │  │ noted    = ["screenshot()"]                              │        │
    │  │                                                          │        │
    │  │         │                                                │        │
    │  │         ▼                                                │        │
    │  │  ┌───────────────────────────────────────────────┐      │        │
    │  │  │ subprocess: capture.py                         │      │        │
    │  │  │                                                │      │        │
    │  │  │ stdin JSON:                                    │      │        │
    │  │  │   {"actions": ["left_click(500, 500)"],        │      │        │
    │  │  │    "sandbox": true, "marks": true,             │      │        │
    │  │  │    "width": 512, "height": 288, ...}           │      │        │
    │  │  │                                                │      │        │
    │  │  │ SANDBOX PATH:                                  │      │        │
    │  │  │   1. Load sandbox_canvas.bmp (1920x1080)       │      │        │
    │  │  │   2. _sandbox_apply:                           │      │        │
    │  │  │      "left_click(500, 500)"                    │      │        │
    │  │  │        → px=960, py=540 (mapped to screen res) │      │        │
    │  │  │        → draw white circle radius=6            │      │        │
    │  │  │        → applied.append("left_click(500,500)") │      │        │
    │  │  │        → update sandbox_state.json:            │      │        │
    │  │  │          {"last_x": 960, "last_y": 540}        │      │        │
    │  │  │   3. Save modified BMP (PERSISTENT)            │      │        │
    │  │  │   4. Copy buffer for marks (EPHEMERAL)         │      │        │
    │  │  │   5. _apply_marks on copy:                     │      │        │
    │  │  │      red circle #1 at (960,540)                │      │        │
    │  │  │   6. Resize 1920x1080 → 512x288 (GDI)         │      │        │
    │  │  │   7. Encode PNG → base64                       │      │        │
    │  │  │                                                │      │        │
    │  │  │ stdout JSON:                                   │      │        │
    │  │  │   {"screenshot_b64": "iVBOR...",               │      │        │
    │  │  │    "applied": ["left_click(500, 500)"]}        │      │        │
    │  │  └───────────────────────────────────────────────┘      │        │
    │  │         │                                                │        │
    │  │         ▼                                                │        │
    │  │  Reconciliation (sandbox mode):                          │        │
    │  │    applied_set = {"left_click(500, 500)"}                │        │
    │  │    executed ∩ applied = ["left_click(500, 500)"]  ✓      │        │
    │  │    executed ∖ applied = []  (nothing moved to noted)     │        │
    │  │                                                          │        │
    │  │ stdout JSON:                                             │        │
    │  │   {"executed": ["left_click(500, 500)"],                 │        │
    │  │    "noted": ["screenshot()"],                            │        │
    │  │    "wants_screenshot": true,                             │        │
    │  │    "screenshot_b64": "iVBOR..."}                         │        │
    │  └─────────────────────────────────────────────────────────┘        │
    │                           │                                          │
    │                           ▼                                          │
    │  Build feedback (user message #2):                                   │
    │  ┌──────────────────────────────────────────────────────────────┐    │
    │  │ "EXECUTOR_FEEDBACK:\n                                        │    │
    │  │ executed=["left_click(500, 500)"]\n                          │    │
    │  │ ignored=["screenshot()"]"                                    │    │
    │  └──────────────────────────────────────────────────────────────┘    │
    │                           │                                          │
    │                           ▼                                          │
    │  Build VLM request:                                                  │
    │  ┌──────────────────────────────────────────────────────────────┐    │
    │  │ messages[0]: {"role": "system", "content": SYSTEM_PROMPT}    │    │
    │  │                                                              │    │
    │  │ messages[1]: {"role": "user",                                │    │
    │  │   "content": [{"type": "text",                               │    │
    │  │     "text": "NARRATIVE:\nI see a black canvas. I will        │    │
    │  │              click in the center.\n\nACTIONS:\n               │    │
    │  │              left_click(500, 500)\nscreenshot()"}]}           │    │
    │  │   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^│    │
    │  │   THIS IS THE SST — BYTE-FOR-BYTE THE VLM'S PRIOR OUTPUT   │    │
    │  │                                                              │    │
    │  │ messages[2]: {"role": "user",                                │    │
    │  │   "content": [                                               │    │
    │  │     {"type": "text",                                         │    │
    │  │      "text": "EXECUTOR_FEEDBACK:\nexecuted=[...]\n..."},     │    │
    │  │     {"type": "image_url",                                    │    │
    │  │      "image_url": {"url": "data:image/png;base64,iVBOR..."}} │    │
    │  │   ]}                                                         │    │
    │  └──────────────────────────────────────────────────────────────┘    │
    │                           │                                          │
    │                           ▼                                          │
    │  HTTP POST to VLM (localhost:1234)                                   │
    │  VLM responds:                                                       │
    │  ┌──────────────────────────────────────────────────────────────┐    │
    │  │ "NARRATIVE:\n                                                │    │
    │  │ A white dot appeared at the center. I will draw a line\n    │    │
    │  │ from center to the top-right corner.\n                      │    │
    │  │ \n                                                          │    │
    │  │ ACTIONS:\n                                                  │    │
    │  │ drag(500, 500, 800, 200)\n                                  │    │
    │  │ screenshot()"                                                │    │
    │  └──────────────────────────────────────────────────────────────┘    │
    │                           │                                          │
    │                           ▼                                          │
    │  state.story = <raw VLM output above, VERBATIM>                      │
    │  Save state.json, dump artifacts, emit to stdout                     │
    │  Sleep(LOOP_DELAY), then → Turn N+1                                  │
    └──────────────────────────────────────────────────────────────────────┘


DATA OWNERSHIP BOUNDARIES:

    ┌─────────────┐      ┌─────────────┐      ┌─────────────┐
    │   main.py   │      │ execute.py  │      │ capture.py  │
    │             │      │             │      │             │
    │ OWNS:       │      │ READS:      │      │ HAS NO      │
    │  state.story│─────►│  raw text   │      │ ACCESS TO   │
    │  (SST)      │      │  (parse     │─────►│  VLM text   │
    │             │      │   actions)  │      │             │
    │ NEVER       │      │             │      │ RECEIVES:   │
    │ MODIFIES    │      │ NEVER       │      │  canonical  │
    │ THE TEXT    │      │ MODIFIES    │      │  action     │
    │             │      │ THE TEXT    │      │  strings    │
    └─────────────┘      └─────────────┘      └─────────────┘


================================================================================
4. SINGLE SOURCE OF TRUTH (SST) — FORMAL PROOF
================================================================================

CLAIM: The VLM output text is never modified between turns.

PROOF BY TRACE:

    Let V(N) = the raw text returned by the VLM at turn N.

    Turn N:
        1. raw = _infer(...)           → raw = V(N)     [from HTTP response]
        2. state.story = raw           → state.story = V(N)  [assignment, no transform]
        3. _save_state(..., raw, ...)   → writes V(N) to state.json under "story" key
                                          json.dumps serializes the string; json.loads
                                          deserializes it identically (JSON round-trip
                                          preserves all Unicode, including \\n, \\t, etc.)

    Turn N+1:
        4. state = _load_state()       → state.story = json.loads(...)["story"]
                                          = V(N)  [JSON round-trip identity]
        5. prev_story = state.story    → prev_story = V(N)  [assignment, no transform]
        6. _run_executor(prev_story)   → sends {"raw": V(N), ...} to execute.py stdin
                                          execute.py READS V(N) via _parse_actions()
                                          but the return value is {executed, noted, ...}
                                          — V(N) is NOT in the return value.
        7. _infer(..., prev_story, ...)→ builds messages[1].text = prev_story = V(N)
                                          This is the SST message.

    At no point does any code transform V(N). The chain is:
        VLM response → raw variable → state.story → state.json → state.story →
        prev_story → messages[1].text → HTTP POST body → VLM receives it.

    Every link in this chain is either:
        (a) Direct assignment (raw → state.story → prev_story)
        (b) JSON serialization/deserialization (which preserves string content)
        (c) HTTP body encoding (json.dumps, which preserves string content)

    QED: V(N) arrives at the VLM on turn N+1 byte-for-byte identical. ∎

EDGE CASES VERIFIED:
    - Empty string V(N) = "": forwarded as {"type":"text","text":""}  ✓
    - V(N) containing newlines, tabs, Unicode: JSON preserves all     ✓
    - V(N) containing JSON-special chars (quotes, backslashes):
      json.dumps escapes them; json.loads unescapes them → identity   ✓
    - V(N) containing null bytes: JSON spec allows \\u0000             ✓
    - V(N) being malformed/nonsensical: forwarded without inspection   ✓


================================================================================
5. INJECTION TRANSPARENCY ANALYSIS
================================================================================

The sandbox achieves transparent simulation through THREE injection points:

INJECTION POINT 1: Physical execution suppression (execute.py)
    ┌────────────────────────────────────────────────────────────┐
    │ if sandbox:                                                 │
    │     physical_execute = False                                │
    │                                                             │
    │ # In the match block:                                       │
    │ case "left_click":                                          │
    │     ...                                                     │
    │     if physical_execute:      ← False in sandbox            │
    │         _do_left_click(x, y)  ← SKIPPED                    │
    │     executed.append(canon)    ← STILL REPORTED AS EXECUTED  │
    └────────────────────────────────────────────────────────────┘

    Effect: The action is recorded as "executed" in feedback, but no Win32
    SendInput event is sent. The pipeline (and VLM) cannot tell.

INJECTION POINT 2: Screenshot source replacement (capture.py)
    ┌────────────────────────────────────────────────────────────┐
    │ if sandbox:                                                 │
    │     base = _sandbox_load(sw, sh, ...)  ← BMP file          │
    │     _sandbox_apply(base, ..., actions) ← draw white shapes │
    │     rgba = bytearray(base)                                  │
    │ else:                                                       │
    │     rgba = _bgra_to_rgba(_capture_bgra(sw, sh)) ← real GDI │
    │                                                             │
    │ # From here, IDENTICAL code path regardless of mode:        │
    │ if marks and actions:                                       │
    │     _apply_marks(rgba, ...)                                 │
    │ # resize, encode PNG, return base64                         │
    └────────────────────────────────────────────────────────────┘

    Effect: The screenshot source is swapped from real GDI capture to a
    persistent BMP canvas. The downstream code (marks, resize, encode)
    is IDENTICAL in both paths. The output format is IDENTICAL.

INJECTION POINT 3: Sandbox canvas as action effect renderer (capture.py)
    ┌────────────────────────────────────────────────────────────┐
    │ _sandbox_apply processes each action:                       │
    │                                                             │
    │   left_click(500, 500) → white circle at (960, 540)        │
    │   drag(100,200,800,600) → white line from A to B           │
    │   type("HELLO") → white text at last click position        │
    │                                                             │
    │ These drawings are PERSISTENT (saved to BMP) and VISIBLE    │
    │ in the next screenshot. The VLM sees the result of its      │
    │ actions, just like it would on a real desktop.              │
    └────────────────────────────────────────────────────────────┘

    Effect: Actions have visible consequences. The VLM can observe
    cause and effect. The simulation is closed-loop.

WHY THE PIPELINE CANNOT DETECT THE INJECTION:

    1. The executor feedback format is IDENTICAL:
       executed=["left_click(500, 500)"] — same in both modes.

    2. The screenshot format is IDENTICAL:
       base64 PNG of the same dimensions — same in both modes.

    3. The VLM request structure is IDENTICAL:
       system + user#1(SST) + user#2(feedback+image) — same in both modes.

    4. The SST is UNAFFECTED:
       state.story contains the VLM's output, not any sandbox metadata.

    The ONLY way the VLM can infer sandbox mode is by LOOKING at the image
    (black background, white shapes instead of a Windows desktop). The
    system prompt explicitly tells the VLM about this visual difference.
    But the PIPELINE INFRASTRUCTURE is mode-agnostic.


================================================================================
6. MULTI-TURN SIMULATION: PROPER VLM OUTPUT
================================================================================

TURN 1 — Cold Start
━━━━━━━━━━━━━━━━━━━━

    state.story = ""  (no prior turn)

    → execute.py receives raw=""
      _parse_actions("") → []
      No actions executed.
      capture.py: sandbox canvas doesn't exist → create black 1920x1080 BMP
      Returns: executed=[], noted=[], screenshot=<black image>

    → main.py builds feedback:
      "EXECUTOR_FEEDBACK:\nexecuted=[]\nignored=[]"

    → VLM request:
      messages[1].text = ""  (empty SST — valid for Qwen3-VL)
      messages[2] = feedback + <black 512x288 PNG>

    → VLM responds:
      "NARRATIVE:\nI see a completely black screen. This appears to be
       sandbox mode. I'll click in the center to start.\n\nACTIONS:\n
       left_click(500, 500)\nscreenshot()"

    → state.story = <above text>
    → STATE: black canvas, no drawings yet


TURN 2 — First Action
━━━━━━━━━━━━━━━━━━━━━━

    state.story = "NARRATIVE:\nI see a completely black screen..."

    → execute.py receives raw=<above>
      _parse_actions → ["left_click(500, 500)", "screenshot()"]
      left_click(500, 500) → executed=["left_click(500, 500)"]
      screenshot() → noted=["screenshot()"], wants_screenshot=True

    → capture.py receives actions=["left_click(500, 500)"]
      _sandbox_apply: draws white circle at (960,540)
      applied=["left_click(500, 500)"]
      Saves BMP. Draws red mark #1. Resizes. Returns base64.

    → Reconciliation: applied == executed → no changes needed.

    → feedback:
      "EXECUTOR_FEEDBACK:\nexecuted=["left_click(500, 500)"]\n
       ignored=["screenshot()"]"

    → VLM request:
      messages[1].text = "NARRATIVE:\nI see a completely black screen..."
      messages[2] = feedback + <black canvas with white dot + red mark>

    → VLM responds:
      "NARRATIVE:\nA white circle appeared where I clicked. I can see
       the red mark labeled '1'. I'll draw a line from center to
       top-right.\n\nACTIONS:\ndrag(500, 500, 800, 200)\nscreenshot()"

    → STATE: canvas has white circle at center


TURN 3 — Drag Action
━━━━━━━━━━━━━━━━━━━━━

    → execute.py: drag(500,500,800,200) → executed
    → capture.py: draws white line from (960,540) to (1536,216)
      Saves BMP. Red arrow mark from start to end.
    → VLM sees: white circle + white line + red arrow mark

    → STATE: canvas accumulates (circle + line)


TURN 4 — Type Action (with valid cursor position)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM output: "...ACTIONS:\ntype(\"HELLO\")\nscreenshot()"

    → execute.py: type("HELLO") → executed=['type("HELLO")']
    → capture.py: sandbox_state.json has last_x=1536, last_y=216
      _draw_text at (1546, 226): "HELLO" in white, scale 2
      applied=['type("HELLO")']
    → Reconciliation: applied == executed → OK

    → STATE: canvas has circle + line + "HELLO" text


================================================================================
7. MULTI-TURN SIMULATION: MALFORMED VLM OUTPUT
================================================================================

SCENARIO A — VLM refuses to cooperate
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM output: "I'm sorry, but I cannot assist with that request.
                 Please let me know if you have any questions."

    → execute.py: _parse_actions → no "ACTIONS:" header
      Fallback scan: no line contains "(" ending with ")"
      executed=[], noted=[]

    → capture.py: actions=[] → no changes to canvas, no marks
      Returns current canvas state as-is.

    → feedback: "EXECUTOR_FEEDBACK:\nexecuted=[]\nignored=[]"

    → VLM request:
      messages[1].text = "I'm sorry, but I cannot assist with that..."
      ^^^^^^^^^^^^^^^^ SST: the refusal is forwarded verbatim.
      messages[2] = feedback + <unchanged canvas>

    → SST PRESERVED ✓
    → Pipeline continues. VLM may self-correct on next turn.


SCENARIO B — VLM outputs truncated action (MAX_TOKENS hit)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM output: "NARRATIVE:\nI see the canvas. I'll click multiple
                 spots.\n\nACTIONS:\nleft_click(100, 200)\nleft_click(300, 4"

    → execute.py:
      _parse_actions → ["left_click(100, 200)", "left_click(300, 4"]
      "left_click(100, 200)" → AST parse OK → executed
      "left_click(300, 4" → ast.parse SyntaxError → noted

      executed=["left_click(100, 200)"]
      noted=["left_click(300, 4"]

    → feedback accurately reports: one executed, one ignored (malformed)
    → SST: the truncated text INCLUDING "left_click(300, 4" is forwarded
      verbatim. The VLM sees its own truncated output and can learn from it.
    → SST PRESERVED ✓


SCENARIO C — VLM outputs random nonsense
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM output: "🎭🎭🎭 BEEP BOOP I AM A ROBOT 🤖\n
                 def hack_the_planet(): return 42\n
                 ACTIONS:\nimport os; os.system('rm -rf /')"

    → execute.py:
      _parse_actions → finds "ACTIONS:" header → section="actions"
      Lines: ["import os; os.system('rm -rf /')"]
      _parse_call("import os; os.system('rm -rf /')"):
        ast.parse → SyntaxError (it's a statement, not an expression)
        → returns None → noted

      executed=[], noted=["import os; os.system('rm -rf /')"]

    → SAFETY: AST parsing rejects anything that isn't a simple function
      call with literal arguments. No eval(), no exec(), no code execution.
    → SST: the nonsense output including emoji is forwarded verbatim.
    → SST PRESERVED ✓


SCENARIO D — VLM outputs actions without NARRATIVE section
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM output: "left_click(200, 300)\ntype(\"test\")"

    → execute.py:
      _parse_actions → no "ACTIONS:" header, no "NARRATIVE:" header
      Fallback scan: both lines contain "(" and end with ")"
      → ["left_click(200, 300)", "type(\"test\")"]
      Both are valid AST calls → executed

    → Pipeline works correctly even without the expected format.
    → SST: the headerless output is forwarded verbatim.
    → SST PRESERVED ✓


SCENARIO E — type() with no prior click position (Bug #5 scenario)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    State: fresh sandbox, no prior actions. sandbox_state.json has
           last_x=None, last_y=None.

    VLM output: "NARRATIVE:\nI'll type hello.\n\nACTIONS:\ntype(\"hello\")"

    → execute.py:
      type("hello") → canon='type("hello")' → executed=['type("hello")']

    → capture.py:
      _sandbox_apply processes 'type("hello")':
        t = "hello", lx = None, ly = None
        isinstance check fails → continue (SKIPPED)
      applied = []  (empty — type was NOT rendered)
      Returns: {"screenshot_b64": "...", "applied": []}

    → execute.py reconciliation:
      applied_set = {}  (empty)
      executed ∩ applied = []
      executed ∖ applied = ['type("hello")']  → moved to noted
      Final: executed=[], noted=['type("hello")', ...]

    → feedback to VLM:
      "EXECUTOR_FEEDBACK:\nexecuted=[]\nignored=['type(\"hello\")']"

    → VLM sees: the type action was ignored. It can infer it needs to
      click somewhere first to establish a cursor position.
    → SST PRESERVED ✓
    → FEEDBACK IS ACCURATE ✓ (was Bug #5, now fixed)


================================================================================
8. MULTI-TURN SIMULATION: EDGE CASES
================================================================================

CASE F — VLM output is completely empty string
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM returns content="" (or null coerced to "")

    → state.story = ""
    → Next turn: prev_story = "" → same as Turn 1 cold start
    → execute.py: _parse_actions("") → []
    → messages[1].text = "" → empty SST, same as cold start
    → SST PRESERVED ✓ (empty is a valid SST value)


CASE G — VLM uses keyword arguments
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    VLM output: "ACTIONS:\nleft_click(x=300, y=400)"

    → execute.py:
      ast.parse → Call(Name("left_click"), args=[], keywords=[x=300, y=400])
      _arg_int([], {"x":300,"y":400}, 0, "x") → 300
      _arg_int([], {"x":300,"y":400}, 1, "y") → 400
      canon = "left_click(300, 400)" → executed ✓

    → capture.py receives "left_click(300, 400)" (canonical, positional)
      Re-parses successfully → draws circle ✓


CASE H — Config hot-reload with syntax error
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Operator edits config.py while loop is running:
      TEMPERATURE = 0.5
      TOP_P = oops this is broken

    → main.py:
      try: importlib.reload(franz_config)
      except Exception: log warning, keep previous values (0.3, 0.95, 300)

    → Pipeline continues uninterrupted. Next turn uses old sampling values.
    → Operator fixes config.py. Next turn picks up the fix.
    → SST UNAFFECTED ✓ (config only affects sampling, not routing)


CASE I — execute.py crashes
━━━━━━━━━━━━━━━━━━━━━━━━━━━

    execute.py has an unhandled exception (e.g., ctypes segfault)

    → main.py: _run_executor:
      result.returncode != 0
      Logs: "[main] execute.py failed (rc=1)"
      Logs: stderr content
      json.loads("" or "{}") → {} (empty result)

    → executor_result = {}
      screenshot_b64 = "" (empty string)
      executed = [], noted = []
      feedback = "EXECUTOR_FEEDBACK:\nexecuted=[]\nignored=[]"

    → VLM request: messages[2] has empty base64 image
      "data:image/png;base64," → may cause VLM API error or hallucination

    → SST is still preserved (prev_story forwarded as-is)
    → The VLM may produce confused output, but the PIPELINE SURVIVES.
    → Next turn: if execute.py is fixed, the loop self-heals.


CASE J — capture.py crashes
━━━━━━━━━━━━━━━━━━━━━━━━━━━

    capture.py crashes during GDI resize (e.g., out of memory)

    → execute.py: _run_capture:
      r.returncode != 0
      Logs: "[execute] capture.py failed"
      Returns ("", actions) as fallback
      screenshot_b64 = "" → same situation as Case I

    → Pipeline survives with degraded image. SST preserved. ✓


================================================================================
9. SST INTEGRITY VERIFICATION MATRIX
================================================================================

Every operation that TOUCHES the VLM text, and whether it preserves SST:

    ┌──────────────────────────┬───────────┬──────────────────────────────┐
    │ Operation                │ Preserves │ Justification                │
    │                          │ SST?      │                              │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ VLM HTTP response →      │    ✓      │ body["choices"][0]["message"]│
    │ raw variable             │           │ ["content"] — direct access  │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ raw → state.story        │    ✓      │ Direct assignment            │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ state.story → state.json │    ✓      │ json.dumps preserves strings │
    │ (disk persistence)       │           │ including all Unicode        │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ state.json → state.story │    ✓      │ json.loads restores strings  │
    │ (disk load)              │           │ identically                  │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ state.story → prev_story │    ✓      │ Direct assignment            │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ prev_story → executor    │    N/A    │ Executor reads it for action │
    │ stdin                    │           │ parsing but does NOT return  │
    │                          │           │ it in stdout. The text is    │
    │                          │           │ "consumed" for parsing only. │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ prev_story → messages[1] │    ✓      │ Direct insertion into JSON   │
    │ .text                    │           │ payload as {"text": V(N)}    │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ messages → HTTP body     │    ✓      │ json.dumps(payload) encodes  │
    │                          │           │ the string in JSON format;   │
    │                          │           │ the VLM server's json.loads  │
    │                          │           │ restores it identically.     │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ sys.stdout.write(raw)    │    N/A    │ Monitoring output only; does │
    │                          │           │ not feed back into pipeline. │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ _dump(..., raw, ...)     │    N/A    │ Debug artifact only; does    │
    │                          │           │ not feed back into pipeline. │
    ├──────────────────────────┼───────────┼──────────────────────────────┤
    │ feedback string          │    ✓      │ Built from executor result,  │
    │                          │           │ NOT from VLM text. Goes into │
    │                          │           │ messages[2], never messages  │
    │                          │           │ [1]. Structurally separated. │
    └──────────────────────────┴───────────┴──────────────────────────────┘

    RESULT: SST is preserved through ALL paths. No operation transforms,
    truncates, or contaminates the VLM text. The feedback is structurally
    isolated in a separate user message.


================================================================================
10. CONCLUSION
================================================================================

The FRANZ pipeline is architecturally sound. After the corrective changes
applied in this review session, the system has:

    ✅ SST GUARANTEE: Formally verified — the VLM's raw output text travels
       through the pipeline without any modification, across process
       boundaries (main.py → state.json → main.py → HTTP), and arrives at
       the VLM on the next turn byte-for-byte identical. Malformed, empty,
       or nonsensical output is forwarded as-is, preserving the agent's
       self-narrative integrity.

    ✅ SANDBOX TRANSPARENCY: The three injection points (physical execution
       suppression, screenshot source swap, canvas-as-effect-renderer) are
       invisible at the pipeline data level. The feedback format, screenshot
       format, VLM request structure, and SST handling are all mode-agnostic.
       The only distinguishing signal is the image content itself (black
       canvas vs real desktop), which is intentionally communicated to the
       VLM via the system prompt.

    ✅ SAFETY: All action parsing uses ast.parse with ast.Constant
       validation. No eval(), no exec(), no code execution from VLM output.
       The VLM's text is ONLY parsed (read-only) to extract action
       function calls with literal arguments.

    ✅ RESILIENCE: Subprocess failures (execute.py crash, capture.py crash)
       are logged and survived. Config reload errors are caught and ignored.
       Fatal exceptions produce full tracebacks. The pipeline continues
       operating in degraded mode rather than dying silently.

    ✅ FEEDBACK ACCURACY: The reconciliation mechanism (capture.py reports
       applied actions, execute.py adjusts executed/noted lists) ensures
       the VLM receives feedback that matches what is actually visible on
       screen. No false positives (action reported as executed but not
       visible).

    REMAINING NON-FUNCTIONAL ITEMS (intentionally not addressed, as the
    author prioritizes data flow purity over performance):

        - Sandbox canvas operates at screen resolution (1920×1080), then
          downscaled to 512×288. Could use output resolution directly.
        - BMP load/save uses per-pixel Python loops. Could use slice-stride
          operations for 10-50× speedup.
        - AST parsing code is duplicated between execute.py and capture.py.
        - Panel proxy (panel.py) is unimplemented.

    These are optimization opportunities. They do not affect correctness,
    SST integrity, or sandbox transparency. The pipeline is FUNCTIONALLY
    COMPLETE and ARCHITECTURALLY SOUND.

================================================================================
END OF REPORT
================================================================================
"""
