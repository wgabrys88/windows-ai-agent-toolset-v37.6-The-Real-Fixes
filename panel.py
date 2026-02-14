"""
FILENAME: panel.py

SYSTEM: FRANZ — Agentic Visual Loop for Windows 11 (Python 3.13, stdlib only)

BIGGER PICTURE:
    This file is the TRANSPARENT REVERSE PROXY ("wireshark") in the FRANZ
    pipeline. It sits between main.py and the upstream VLM, forwarding all
    traffic byte-for-byte while independently observing, logging, and
    verifying the data flow.

    The pipeline data flow WITH panel.py:
        main.py ──HTTP POST──► panel.py (localhost:1234) ──HTTP POST──► VLM (localhost:1235)
                 ◄──HTTP resp──                           ◄──HTTP resp──

    panel.py also serves a live HTML dashboard on a SIDE PORT (localhost:8080)
    that shows each turn's data in real time via Server-Sent Events (SSE).

WHAT THIS FILE DOES:
    1. Receives the full HTTP request body from main.py (raw bytes).
    2. Parses a COPY of the JSON to extract SST message, feedback, image,
       and sampling parameters for display and verification.
    3. Forwards the ORIGINAL raw bytes to the upstream VLM — never re-serialized.
    4. Receives the full HTTP response from the VLM (raw bytes).
    5. Parses a COPY of the JSON to extract the VLM's text output.
    6. Forwards the ORIGINAL raw bytes back to main.py.
    7. Performs SST VERIFICATION: compares the current request's messages[1]
       text to the previous response's content. Logs a WARNING if they differ.
    8. Pushes the parsed turn data to all connected SSE clients for live display.
    9. Writes per-turn JSON logs to panel_log/ directory.
   10. Extracts the FULL base64 image data URI from the request payload and
       includes it in the SSE broadcast, enabling the dashboard to render a
       live screenshot of every frame the pipeline sends to the VLM.

TRANSPARENCY GUARANTEE:
    panel.py NEVER modifies the bytes flowing through it. It reads copies
    for inspection. The main.py ↔ VLM channel is byte-identical with or
    without panel.py in the path. Removing panel.py and pointing main.py
    directly at the VLM produces IDENTICAL behavior. The image data URI is
    only extracted and forwarded on the observation side-channel (SSE) —
    the proxy payload is never touched.

SST VERIFICATION:
    panel.py is the ONLY component that can verify SST from OUTSIDE the
    pipeline. It stores the VLM's response text from turn N and compares
    it to messages[1].text in the request for turn N+1. If they differ,
    it logs a SST VIOLATION warning. This is a READ-ONLY check — the
    request is still forwarded unchanged.

LIVE SCREENSHOT DISPLAY:
    When the request payload contains an image_url part with a base64 data
    URI (e.g. "data:image/png;base64,…"), panel.py captures the FULL URI
    in the field "image_data_uri" and includes it in both the per-turn
    JSON log and the SSE event broadcast. The dashboard (panel.html)
    renders this data URI directly as an <img> element, providing a real-
    time visual feed of every screenshot flowing through the pipeline.
    This is purely observational — the image bytes in transit are never
    altered.

FILES:
    panel.py        — this file (Python HTTP server + proxy + SSE)
    panel.html      — the live dashboard UI (served as static file)

PORTS:
    1234  — proxy port (main.py sends here; was the VLM's port)
    1235  — upstream VLM port (LM Studio / vLLM / etc.)
    8080  — dashboard UI + SSE endpoint

RUNTIME:
    - Windows 11, Python 3.13+
    - Stdlib only (http.server, urllib, json, threading)
"""


import http.server
import json
import queue
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_HOST: Final[str] = "127.0.0.1"
PROXY_PORT: Final[int] = 1234

UPSTREAM_URL: Final[str] = "http://127.0.0.1:1235/v1/chat/completions"

DASHBOARD_HOST: Final[str] = "127.0.0.1"
DASHBOARD_PORT: Final[int] = 8080

LOG_DIR: Final[Path] = Path(__file__).parent / "panel_log"
HTML_FILE: Final[Path] = Path(__file__).parent / "panel.html"

MAX_SSE_CLIENTS: Final[int] = 20
SSE_KEEPALIVE_SEC: Final[float] = 15.0

# ---------------------------------------------------------------------------
# Shared state (thread-safe)
# ---------------------------------------------------------------------------

_turn_counter: int = 0
_turn_lock = threading.Lock()

_last_vlm_response_text: str | None = None
_last_vlm_lock = threading.Lock()

# SSE client queues
_sse_clients: list[queue.Queue[str]] = []
_sse_lock = threading.Lock()


def _next_turn() -> int:
    global _turn_counter
    with _turn_lock:
        _turn_counter += 1
        return _turn_counter


def _set_last_vlm_response(text: str) -> None:
    global _last_vlm_response_text
    with _last_vlm_lock:
        _last_vlm_response_text = text


def _get_last_vlm_response() -> str | None:
    with _last_vlm_lock:
        return _last_vlm_response_text


def _broadcast_sse(data: str) -> None:
    """Send an SSE message to all connected dashboard clients."""
    msg = f"data: {data}\n\n"
    dead: list[queue.Queue[str]] = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _sse_clients.remove(q)
            except ValueError:
                pass


def _register_sse_client() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=200)
    with _sse_lock:
        # Evict oldest if at capacity
        while len(_sse_clients) >= MAX_SSE_CLIENTS:
            _sse_clients.pop(0)
        _sse_clients.append(q)
    return q


def _unregister_sse_client(q: queue.Queue[str]) -> None:
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log_turn(turn: int, entry: dict) -> None:
    try:
        path = LOG_DIR / f"turn_{turn:04d}.json"
        path.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request/Response parsing (READ-ONLY, on copies)
# ---------------------------------------------------------------------------

def _safe_parse_request(raw_body: bytes) -> dict:
    """Parse the request body for display. Returns a summary dict."""
    result: dict = {
        "model": "",
        "sst_text": "",
        "feedback_text": "",
        "has_image": False,
        "image_b64_prefix": "",
        "image_data_uri": "",
        "sampling": {},
        "messages_count": 0,
        "parse_error": None,
    }
    try:
        obj = json.loads(raw_body)
        result["model"] = str(obj.get("model", ""))
        messages = obj.get("messages", [])
        result["messages_count"] = len(messages)

        # Sampling params
        for key in ("temperature", "top_p", "max_tokens"):
            if key in obj:
                result["sampling"][key] = obj[key]

        # messages[1] = SST (user message #1)
        if len(messages) > 1:
            msg1 = messages[1]
            content = msg1.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        result["sst_text"] = str(part.get("text", ""))
                        break
            elif isinstance(content, str):
                result["sst_text"] = content

        # messages[2] = feedback + image (user message #2)
        if len(messages) > 2:
            msg2 = messages[2]
            content = msg2.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            result["feedback_text"] = str(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            result["has_image"] = True
                            url = str(part.get("image_url", {}).get("url", ""))
                            # Store first 80 chars for log display
                            result["image_b64_prefix"] = url[:80] + "..."
                            # Store the FULL data-URI so the dashboard can display it
                            result["image_data_uri"] = url
            elif isinstance(content, str):
                result["feedback_text"] = content

    except Exception as e:
        result["parse_error"] = str(e)

    return result


def _safe_parse_response(raw_body: bytes) -> dict:
    """Parse the VLM response for display. Returns a summary dict."""
    result: dict = {
        "vlm_text": "",
        "finish_reason": "",
        "usage": {},
        "parse_error": None,
    }
    try:
        obj = json.loads(raw_body)
        choices = obj.get("choices", [])
        if choices and isinstance(choices, list):
            choice = choices[0]
            msg = choice.get("message", {})
            result["vlm_text"] = str(msg.get("content", ""))
            result["finish_reason"] = str(choice.get("finish_reason", ""))
        usage = obj.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage
    except Exception as e:
        result["parse_error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# SST Verification
# ---------------------------------------------------------------------------

def _verify_sst(turn: int, sst_text: str) -> dict:
    """Compare the SST text in this request to the VLM output from last turn."""
    prev = _get_last_vlm_response()
    result: dict = {
        "verified": False,
        "match": False,
        "prev_available": prev is not None,
        "detail": "",
    }

    if prev is None:
        # First turn or no previous response stored
        if sst_text == "":
            result["verified"] = True
            result["match"] = True
            result["detail"] = "Turn 1: empty SST, no previous response (OK)"
        else:
            result["verified"] = True
            result["match"] = True
            result["detail"] = "First observed turn with non-empty SST (no baseline to compare)"
        return result

    result["verified"] = True
    if sst_text == prev:
        result["match"] = True
        result["detail"] = f"SST matches previous VLM response ({len(sst_text)} chars)"
    else:
        result["match"] = False
        # Find where they diverge
        min_len = min(len(sst_text), len(prev))
        diff_pos = min_len  # assume they differ at the end if one is longer
        for i in range(min_len):
            if sst_text[i] != prev[i]:
                diff_pos = i
                break
        result["detail"] = (
            f"SST VIOLATION! Texts differ at position {diff_pos}. "
            f"SST length={len(sst_text)}, prev response length={len(prev)}. "
            f"SST[{diff_pos}:{diff_pos+20}]={sst_text[diff_pos:diff_pos+20]!r}, "
            f"prev[{diff_pos}:{diff_pos+20}]={prev[diff_pos:diff_pos+20]!r}"
        )

    return result


# ---------------------------------------------------------------------------
# Proxy HTTP Handler (port 1234)
# ---------------------------------------------------------------------------

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Transparent reverse proxy that forwards requests to the upstream VLM."""

    server_version = "FranzPanel/1.0"

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default access logs (we do our own logging)
        pass

    def do_POST(self) -> None:
        turn = _next_turn()
        ts_start = time.monotonic()
        timestamp = datetime.now().isoformat()

        # --- Read the FULL request body from main.py ---
        content_length = int(self.headers.get("Content-Length", 0))
        raw_request = self.rfile.read(content_length) if content_length > 0 else b""

        # --- Parse a COPY for inspection (never touch raw_request) ---
        req_parsed = _safe_parse_request(raw_request)

        # --- SST verification ---
        sst_check = _verify_sst(turn, req_parsed["sst_text"])
        if sst_check["verified"] and not sst_check["match"]:
            sys.stderr.write(f"[panel] ⚠ SST VIOLATION on turn {turn}: {sst_check['detail']}\n")
            sys.stderr.flush()

        # --- Forward ORIGINAL raw bytes to upstream VLM ---
        upstream_req = urllib.request.Request(
            UPSTREAM_URL,
            data=raw_request,  # ORIGINAL BYTES, not re-serialized
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        raw_response = b""
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_parsed: dict = {}
        error_detail = ""

        try:
            with urllib.request.urlopen(upstream_req, timeout=120) as resp:
                resp_status = resp.status
                resp_headers = dict(resp.headers)
                raw_response = resp.read()  # ORIGINAL BYTES from VLM

        except urllib.error.HTTPError as e:
            resp_status = e.code
            raw_response = e.read() if e.fp else b""
            error_detail = f"HTTPError {e.code}: {e.reason}"
            sys.stderr.write(f"[panel] upstream error on turn {turn}: {error_detail}\n")
            sys.stderr.flush()

        except Exception as e:
            error_detail = f"{type(e).__name__}: {e}"
            raw_response = json.dumps({"error": error_detail}).encode("utf-8")
            sys.stderr.write(f"[panel] upstream exception on turn {turn}: {error_detail}\n")
            sys.stderr.flush()

        ts_end = time.monotonic()
        latency_ms = (ts_end - ts_start) * 1000.0

        # --- Parse a COPY of the response for inspection ---
        resp_parsed = _safe_parse_response(raw_response)

        # --- Store VLM response text for next turn's SST verification ---
        if resp_parsed["vlm_text"]:
            _set_last_vlm_response(resp_parsed["vlm_text"])

        # --- Forward ORIGINAL raw bytes back to main.py ---
        self.send_response(resp_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw_response)))
        self.end_headers()
        self.wfile.write(raw_response)  # ORIGINAL BYTES, not re-serialized

        # --- Build log/SSE entry ---
        entry = {
            "turn": turn,
            "timestamp": timestamp,
            "latency_ms": round(latency_ms, 1),
            "request": {
                "model": req_parsed["model"],
                "sst_text": req_parsed["sst_text"],
                "sst_text_length": len(req_parsed["sst_text"]),
                "feedback_text": req_parsed["feedback_text"],
                "has_image": req_parsed["has_image"],
                "image_data_uri": req_parsed["image_data_uri"],
                "sampling": req_parsed["sampling"],
                "messages_count": req_parsed["messages_count"],
                "body_size_bytes": len(raw_request),
                "parse_error": req_parsed["parse_error"],
            },
            "response": {
                "status": resp_status,
                "vlm_text": resp_parsed["vlm_text"],
                "vlm_text_length": len(resp_parsed["vlm_text"]),
                "finish_reason": resp_parsed["finish_reason"],
                "usage": resp_parsed["usage"],
                "body_size_bytes": len(raw_response),
                "parse_error": resp_parsed["parse_error"],
                "error": error_detail,
            },
            "sst_check": sst_check,
        }

        # --- Log to disk ---
        _log_turn(turn, entry)

        # --- Console summary ---
        sst_indicator = "✓" if sst_check.get("match", False) else "✗ VIOLATION"
        sys.stdout.write(
            f"[panel] turn={turn} "
            f"latency={latency_ms:.0f}ms "
            f"status={resp_status} "
            f"sst={sst_indicator} "
            f"vlm_len={len(resp_parsed['vlm_text'])} "
            f"finish={resp_parsed['finish_reason']}\n"
        )
        sys.stdout.flush()

        # --- Broadcast to SSE dashboard clients ---
        try:
            _broadcast_sse(json.dumps(entry, default=str))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dashboard HTTP Handler (port 8080)
# ---------------------------------------------------------------------------

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Serves the HTML dashboard and SSE event stream."""

    server_version = "FranzDashboard/1.0"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/events":
            self._serve_sse()
        elif self.path == "/health":
            self._serve_json({"status": "ok", "turn": _turn_counter})
        else:
            self.send_error(404)

    def _serve_html(self) -> None:
        try:
            html = HTML_FILE.read_bytes()
        except FileNotFoundError:
            html = b"<html><body><h1>panel.html not found</h1><p>Place panel.html next to panel.py</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html)

    def _serve_json(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        """Server-Sent Events stream for live dashboard updates."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        client_q = _register_sse_client()
        try:
            # Send initial connection event
            self.wfile.write(b"data: {\"type\":\"connected\"}\n\n")
            self.wfile.flush()

            while True:
                try:
                    msg = client_q.get(timeout=SSE_KEEPALIVE_SEC)
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive comment to prevent timeout
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _unregister_sse_client(client_q)


# ---------------------------------------------------------------------------
# Threaded HTTP server helper
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(http.server.HTTPServer):
    """HTTPServer that handles each request in a new thread."""
    daemon_threads = True
    allow_reuse_address = True

    def process_request(self, request, client_address) -> None:  # type: ignore[override]
        t = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            daemon=True,
        )
        t.start()

    def process_request_thread(self, request, client_address) -> None:  # type: ignore[override]
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _ensure_log_dir()

    # Start proxy server (port 1234)
    proxy = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    sys.stdout.write(
        f"[panel] Proxy listening on {PROXY_HOST}:{PROXY_PORT} "
        f"→ forwarding to {UPSTREAM_URL}\n"
    )

    # Start dashboard server (port 8080)
    dashboard = ThreadedHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
    dashboard_thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    dashboard_thread.start()
    sys.stdout.write(
        f"[panel] Dashboard at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/\n"
    )

    sys.stdout.write("[panel] Ready. Waiting for traffic...\n")
    sys.stdout.flush()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        sys.stdout.write("\n[panel] Shutting down...\n")
        sys.stdout.flush()
        proxy.shutdown()
        dashboard.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

