"""
FILENAME: config.py

SYSTEM: FRANZ — Agentic Visual Loop for Windows 11 (Python 3.13, stdlib only)

BIGGER PICTURE:
    This file holds VLM sampling parameters that main.py reads each turn.
    main.py calls `importlib.reload(config)` at the start of every loop
    iteration, so you can edit these values while the pipeline is running
    and they take effect on the very next turn — no restart required.

    If this file has a syntax error at reload time, main.py catches the
    exception, logs a warning, and keeps using the previous values. The
    pipeline is never interrupted by a bad config edit.

    The pipeline data flow (this file's role):
        config.py ──imported──► main.py ──VLM request──► LM Studio / panel.py

FILE PIPELINE:
    INPUTS:  none (constants, edited by the operator)
    OUTPUTS: sampling parameters consumed by main.py:_sampling_dict()
        TEMPERATURE — controls randomness (0.0 = deterministic, 1.0+ = creative)
        TOP_P       — nucleus sampling cutoff (0.0–1.0)
        MAX_TOKENS  — hard cap on VLM response length in tokens

SST NOTE:
    This file has NO access to VLM text or pipeline state. It cannot
    affect the SST data path. It only influences how the VLM generates
    its next response (sampling behavior, not content routing).

TUNING GUIDANCE:
    - TEMPERATURE 0.2–0.4: good for consistent action syntax and focused behavior
    - TEMPERATURE 0.6+: more exploratory, may produce novel narratives but
      also more malformed actions (the pipeline handles this gracefully)
    - MAX_TOKENS 300: sufficient for NARRATIVE + 3–5 action lines; increase
      if the VLM frequently truncates mid-action (check dump/ artifacts)
    - TOP_P 0.95: standard; lower values (0.8) make output more predictable

RUNTIME:
    - Windows 11, Python 3.13+
    - Stdlib only (this file is just constants)
"""

TEMPERATURE: float = 0.3
TOP_P: float = 0.95
MAX_TOKENS: int = 300
