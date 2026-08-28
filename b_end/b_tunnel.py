#!/usr/bin/env python3
"""
QR Tunnel - B端隧道（云桌面端）

监听剪贴板中的 QRT: 请求，转发到内网 git 服务器，
将响应编码为二维码并在全屏窗口循环播放。

支持多QR网格同时显示（根据屏幕尺寸自适应）和选择性重传
（A端通过 QRT:MISSING 信号告知缺失页，B端后续轮次只播缺失页）。

用法:
    python b_tunnel.py [--target 192.168.21.14:8888] [--page-ms 200] [--chunk 2800]
"""

import sys
import os
import re
import time
import json
import base64
import gzip
import struct
import io
import argparse
import tempfile
import logging
import logging.handlers
import threading
from pathlib import Path
from http.client import HTTPConnection

from PIL import Image, ImageTk
import tkinter as tk
import ctypes
from ctypes import wintypes


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
if not VERSION_FILE.exists():
    VERSION_FILE = PROJECT_ROOT / "VERSION"
try:
    VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
except OSError:
    VERSION = "0.5.0-dev"
PROTOCOL_VERSION = "qrtunnel-qr-1"
FEATURES = ["multiqr", "missing", "stopped", "idle-marker", "head", "probe", "bulk", "compose"]
SUMMARY_PATH = Path(__file__).resolve().parent / "logs" / "latest-transfer-summary.json"
HISTORY_PATH = Path(__file__).resolve().parent / "logs" / "transfer-history.jsonl"
LOG_PATH = Path(__file__).resolve().parent / "logs" / "tunnel.log"
SUMMARY_LOCK = threading.RLock()
_last_summary = {"version": VERSION, "role": "B", "status": "idle"}
_last_history_key = None


# ---- config.yaml loading (flat YAML subset; no PyYAML dependency) ----
# Keep the B-end entry point self-contained for offline deployment. Supported
# values: strings, quoted strings, bools, ints and floats. CLI arguments
# override config values; missing values use built-ins.
def _coerce_config_value(value):
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.startswith(('"', "'")) and len(value) >= 2 and value[-1] == value[0]:
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def load_config(path):
    """Parse the project's flat ``key: value`` config.yaml subset."""
    result = {}
    if path is None:
        return result
    path = Path(path)
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if not value.startswith(('"', "'")):
            value = re.sub(r"\s+#.*$", "", value).strip()
        result[key] = _coerce_config_value(value)
    return result


def side_defaults(config, side):
    """Map ``a_xxx`` / ``b_xxx`` keys onto argparse destination names."""
    prefix = f"{side.lower()}_"
    return {
        key[len(prefix):]: value
        for key, value in config.items()
        if key.startswith(prefix) and len(key) > len(prefix)
    }


# ---- File logging (rotating) ----
# Mirrors every console line to logs/tunnel.log (5 MiB x 3 backups). Detached
# logger: console output stays under our control.
_FILE_LOGGER = logging.getLogger("qrtunnel.file")
_FILE_LOGGER.setLevel(logging.INFO)
if not _FILE_LOGGER.handlers:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        _FILE_LOGGER.addHandler(_fh)
        _FILE_LOGGER.propagate = False
    except OSError:
        _FILE_LOGGER = None  # file logging unavailable; console only

# Probe endpoint: a special path A-end may request at startup to learn this
# B-end's version/protocol/features without touching the intranet git server.
PROBE_PATH = "/__qrtunnel/probe"
# Version-40 QR, byte mode, error correction L carries 2953 bytes. Every data
# page reserves a 37-byte binary envelope ([type][seq][request UUID]), leaving
# 2916 bytes for the response chunk. The default 2800 and Bulk default 2900
# remain inside this bound.
QR_DATA_CAPACITY_BYTES = 2953
DATA_PAGE_HEADER_BYTES = 37
MAX_CHUNK_BYTES = QR_DATA_CAPACITY_BYTES - DATA_PAGE_HEADER_BYTES


def _write_summary(update):
    global _last_summary, _last_history_key
    with SUMMARY_LOCK:
        if update.get("status") == "in_progress":
            # A new request starts: drop per-request result fields so stale
            # values from the previous request never leak into this one.
            for key in ("http_status", "response_bytes", "elapsed_seconds",
                        "failure_reason", "terminal_reason", "qr_pages",
                        "bulk", "bulk_chunk"):
                _last_summary.pop(key, None)
        _last_summary = {**_last_summary, **update, "version": VERSION, "role": "B", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        try:
            SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SUMMARY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(_last_summary, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SUMMARY_PATH)
            terminal = _last_summary.get("status") in {"completed", "failed", "cancelled", "superseded"}
            request_id = _last_summary.get("request_id")
            history_key = (request_id, _last_summary.get("status"))
            if terminal and request_id and history_key != _last_history_key:
                with HISTORY_PATH.open("a", encoding="utf-8") as history:
                    history.write(json.dumps(_last_summary, ensure_ascii=False) + "\n")
                _last_history_key = history_key
        except OSError as exc:
            blog_event("WARN", "SUMMARY", f"write failed: {exc}")


# ---- Console logging ----

def short_id(req_id):
    return (req_id or "-").replace("-", "")[:8]


def blog_event(level, phase, message, req_id=None):
    """Print one consistent B-end log line, mirrored to logs/tunnel.log."""
    req_tag = f"[req:{short_id(req_id)}]" if req_id else "[req:--------]"
    line = f"[B][{time.strftime('%H:%M:%S')}][{level}][{phase}]{req_tag} {message}"
    print(line, flush=True)
    if _FILE_LOGGER is not None:
        try:
            _FILE_LOGGER.info(line)
        except Exception:
            pass


def configure_console_quickedit():
    """Disable QuickEdit so a console click cannot suspend the tunnel."""
    try:
        handle = _kernel32.GetStdHandle(ctypes.c_ulong(-10).value)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError("no console input handle")
        original = wintypes.DWORD()
        if not _kernel32.GetConsoleMode(handle, ctypes.byref(original)):
            raise OSError("GetConsoleMode failed")
        quick_edit = 0x0040
        extended = 0x0080
        desired = (original.value | extended) & ~quick_edit
        if not _kernel32.SetConsoleMode(handle, desired):
            raise OSError("SetConsoleMode failed")
        current = wintypes.DWORD()
        if not _kernel32.GetConsoleMode(handle, ctypes.byref(current)):
            raise OSError("GetConsoleMode verification failed")
        disabled = not bool(current.value & quick_edit)
        blog_event("INFO" if disabled else "WARN", "CONSOLE",
                   f"QuickEdit: {'DISABLED' if disabled else 'STILL ENABLED'} "
                   f"(original=0x{original.value:04x}, current=0x{current.value:04x})")
        return disabled
    except Exception as exc:
        blog_event("WARN", "CONSOLE", f"QuickEdit mode not changed: {exc}")
        return False


_instance_handle = None


def acquire_single_instance():
    """Prevent two B-end processes from competing for one clipboard/display."""
    global _instance_handle
    try:
        _kernel32.CreateMutexW.restype = ctypes.c_void_p
        _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        _kernel32.GetLastError.restype = wintypes.DWORD
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        name = "Global\\QRTunnel-B-End"
        _instance_handle = _kernel32.CreateMutexW(None, False, name)
        if not _instance_handle:
            blog_event("WARN", "START", "single-instance mutex unavailable; continuing")
            return True
        if _kernel32.GetLastError() == 183:
            blog_event("ERROR", "START", "another B-end instance is already running; exiting")
            _kernel32.CloseHandle(_instance_handle)
            _instance_handle = None
            return False
        blog_event("INFO", "START", "single-instance mutex acquired")
        return True
    except Exception as exc:
        blog_event("WARN", "START", f"single-instance check failed: {exc}; continuing")
        return True


def release_single_instance():
    global _instance_handle
    if _instance_handle:
        try:
            _kernel32.CloseHandle(_instance_handle)
        except Exception:
            pass
        _instance_handle = None


# ---- clipboard helpers (Win32 ctypes) ----

CF_UNICODETEXT = 13

# Non-empty idle sentinel written by A-end after cleanup instead of clearing the
# clipboard. B-end only recognizes it as an idle baseline and never writes it.
IDLE_MARKER = "QRT:IDLE"

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_kernel32.GetStdHandle.restype = ctypes.c_void_p
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetConsoleMode.restype = wintypes.BOOL
_kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
_kernel32.SetConsoleMode.restype = wintypes.BOOL
_kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, wintypes.DWORD]

# 64-bit handles must be declared as c_void_p, otherwise truncated to 32 bits -> NULL
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


_clipboard_lock = threading.RLock()


def _open_clipboard(retries=20, delay=0.01):
    for attempt in range(retries):
        if _user32.OpenClipboard(0):
            return
        time.sleep(delay * min(attempt + 1, 5))
    raise OSError("OpenClipboard failed after retries")


def get_clipboard_text():
    """Read Unicode text with local serialization and transient-lock retries."""
    with _clipboard_lock:
        _open_clipboard()
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                _kernel32.GlobalUnlock(handle)
        finally:
            _user32.CloseClipboard()


def get_screen_size():
    """Fresh primary-screen dimensions via Win32. Tk's winfo_screenwidth() can
    be stale after an RDP desktop resize, so query GetSystemMetrics directly."""
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    return _user32.GetSystemMetrics(SM_CXSCREEN), _user32.GetSystemMetrics(SM_CYSCREEN)


# ---- range encode/decode for MISSING signal ----

def decode_ranges(s):
    """Decode comma-separated ranges into a set of indices.
    e.g. '0-3,5,7-10' -> {0,1,2,3,5,7,8,9,10}"""
    indices = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo), int(hi)
            if lo < 0 or hi < lo or hi - lo > 10000:
                raise ValueError(f"invalid range: {part}")
            for i in range(lo, hi + 1):
                indices.add(i)
        else:
            value = int(part)
            if value < 0:
                raise ValueError(f"invalid index: {part}")
            indices.add(value)
    return indices


# ---- QR generation ----

import qrcode


def make_qr(data_bytes, version=40, ecc="L", box_size=8, verbose=True):
    """Generate a QR image from raw bytes.

    Uses zxing-cpp's native C++ encoder when available: ~3ms per v40 page vs
    ~130ms for the pure-Python qrcode library. Since the display loop composes
    one frame and converts it with a single ImageTk.PhotoImage, zxing speed
    mainly matters for cache misses during pre-render and the qrcode fallback.
    Falls back to qrcode if zxing-cpp is not installed.

    Data pages use fixed Version 40 for maximum capacity. Meta pages pass
    version=None so the encoder chooses a much smaller symbol; at the same
    display area its modules become larger and survive RDP scaling/compression
    better. zxing-cpp auto-selects the smallest version that fits, so
    version=40 data and version=None meta both behave correctly.
    """
    try:
        import zxingcpp
        ec_map = {"L": 0, "M": 1, "Q": 2, "H": 3}
        bc = zxingcpp.create_barcode(
            data_bytes, zxingcpp.BarcodeFormat.QRCode,
            ec_level=ec_map.get(ecc, 0))
        img = zxingcpp.write_barcode_to_image(
            bc, scale=box_size, add_hrt=False, add_quiet_zones=True)
        w, h = img.shape[1], img.shape[0]
        pil = Image.frombuffer("L", (w, h), bytes(img), "raw", "L", 0, 1)
        if verbose:
            blog_event("INFO", "QR",
                       f"render payload={len(data_bytes)}B via zxing-cpp, size={w}x{h}")
        return pil
    except Exception:
        pass
    qr = qrcode.QRCode(
        version=version,
        error_correction=getattr(qrcode.constants, f"ERROR_CORRECT_{ecc}"),
        box_size=box_size,
        border=4,
    )
    qr.add_data(data_bytes)
    qr.make(fit=version is None)
    if verbose:
        blog_event("INFO", "QR", f"render payload={len(data_bytes)}B, first=0x{data_bytes[0]:02x}")
    return qr.make_image(fill_color="black", back_color="white")


def make_signal_qr(text):
    """Generate a small control QR (ACK/STOPPED)."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white")


def make_ack_qr(req_id):
    """Small QR that tells A-end the request was received."""
    return make_signal_qr(f"QRT-ACK:{req_id}")


def compose_qr_frame(images, cols, rows, qr_w, qr_h):
    """Compose one screen frame from per-page QR PIL images.

    Pastes all QR pages of one frame onto a single grayscale ('L') canvas of
    cols*qr_w x rows*qr_h so the display loop needs exactly ONE
    ImageTk.PhotoImage conversion per frame instead of one per page. On slow
    cloud desktops the PhotoImage conversion is the render bottleneck
    (~220ms/page), so converting 8 pages as one image turns an ~1.8s frame
    into a single conversion of about the same total pixels.

    ``images``: up to cols*rows QR images (any PIL mode; converted by callers
    to 'L' before passing to avoid surprises). Each image is centered within
    its cell, exactly like the old per-label layout. Missing cells stay black.
    Images beyond cell capacity are ignored (matches the old per-label layout,
    where labels beyond the grid were never visible).
    """
    canvas = Image.new("L", (cols * qr_w, rows * qr_h), 0)
    for i, img in enumerate(images[: cols * rows]):
        cell_col = i % cols
        cell_row = i // cols
        x = cell_col * qr_w + (qr_w - img.width) // 2
        y = cell_row * qr_h + (qr_h - img.height) // 2
        canvas.paste(img, (x, y))
    return canvas


def show_ack(req_id, hold_ms=800):
    """Show a small centered topmost ACK QR for hold_ms.

    Clipboard A->B is one-way, so the screen is the only back-channel:
    A-end keeps re-writing the request to the clipboard until it sees this
    ACK QR (or a response page) for the same req_id.
    """
    root = tk.Tk()
    root.attributes("-topmost", True)
    root.overrideredirect(True)   # borderless, consistent with the main QR window
    root.configure(background="black")
    img = make_ack_qr(req_id)
    photo = ImageTk.PhotoImage(img, master=root)
    label = tk.Label(root, image=photo, bg="black")
    label.pack(padx=20, pady=20)
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = get_screen_size()   # fresh dims; winfo_screenwidth can be stale after RDP resize
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    blog_event("INFO", "ACK", f"showing ACK for {hold_ms}ms", req_id)
    end = time.time() + hold_ms / 1000
    while time.time() < end:
        root.update()
        time.sleep(0.05)
    root.destroy()


def show_stopped(req_id, hold_ms=500):
    """Briefly confirm that the response QR window has closed.

    A-end uses this screen ACK to release the serialized HTTP request immediately
    instead of always sleeping two seconds before the next Git request.
    """
    root = tk.Tk()
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    root.configure(background="black")
    img = make_signal_qr(f"QRT-STOPPED:{req_id}")
    photo = ImageTk.PhotoImage(img, master=root)
    label = tk.Label(root, image=photo, bg="black")
    label.pack(padx=20, pady=20)
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = get_screen_size()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    end = time.time() + hold_ms / 1000
    while time.time() < end:
        root.update()
        time.sleep(0.05)
    root.destroy()


# ---- QR display ----

class QRDisplay:
    """Fullscreen QR display using tkinter.

    Supports multi-QR grid display: multiple QR codes shown simultaneously
    in a grid layout, with the grid size auto-calculated from screen dimensions.
    Frame rendering composes all cell QRs onto one PIL canvas and converts it
    with a single ImageTk.PhotoImage (the display bottleneck on slow cloud
    desktops), instead of converting each QR page separately.
    Also supports selective retransmission: after each loop, reads the
    QRT:MISSING signal from A-end and only plays the pages A-end hasn't
    received yet.
    """

    def __init__(self, page_ms=200, loops=3, max_qr=0, min_box_size=2, max_extra_rounds=8):
        self.page_ms = page_ms
        self.loops = loops
        self.max_qr = max_qr  # 0 = auto-calculate
        self.min_box_size = min_box_size
        self.max_extra_rounds = max_extra_rounds  # backfill rounds after fixed loops

    def _calc_grid(self, sw, sh, item_count=None):
        """Calculate grid (cols, rows) and box_size for multi-QR display.

        Target: fit as many QRs as possible while keeping box_size >= min_box_size.
        QR v40 has 177+8=185 modules per side. Each module is box_size pixels.
        When item_count is given, prefer the largest QR modules among layouts that
        can fit that many items. This makes a one-page backfill a large centered QR
        instead of leaving it small in the first cell of the normal 4x2 grid.
        """
        best = None  # (cols, rows, box_size)
        max_total = self.max_qr if self.max_qr > 0 else 999
        required = max(1, item_count or 1)
        for cols in range(1, 8):
            for rows in range(1, 6):
                total = cols * rows
                if total > max_total:
                    continue
                if item_count is not None and total < required:
                    continue
                cell_w = sw // cols
                cell_h = sh // rows
                cell_min = min(cell_w, cell_h)
                # QR image size = 185 * box_size; it must fit in cell with margin
                box = max(1, int(cell_min * 0.9) // 185)
                if box < self.min_box_size:
                    continue
                if best is None:
                    best = (cols, rows, box)
                    continue
                if item_count is None:
                    # Normal full-response mode: maximize simultaneous QR count.
                    cur_total = best[0] * best[1]
                    if total > cur_total or (total == cur_total and box > best[2]):
                        best = (cols, rows, box)
                else:
                    # Sparse/backfill mode: maximize QR size, then waste fewer cells.
                    best_total = best[0] * best[1]
                    if box > best[2] or (box == best[2] and total < best_total):
                        best = (cols, rows, box)
        if best is not None:
            return best
        return (1, 1, 1)

    def _check_stop(self, req_id=None):
        """Check clipboard for stop signals from A-end.
        Returns "cancel" (client disconnected), "done" (A-end collected the
        full response, no need to keep playing), or None."""
        try:
            text = get_clipboard_text() or ""
            if text.startswith("QRT:CANCEL:"):
                if req_id and text == f"QRT:CANCEL:{req_id}":
                    return "cancel"
                return None
            if req_id and text == f"QRT:DONE:{req_id}":
                return "done"
        except Exception:
            pass
        return None

    def _check_missing(self, req_id, page_count=None):
        """Read and validate A-end's missing page list.

        Returns a set of valid page indices (0=Meta), or None when no matching
        signal is present. Malformed signals are ignored and retried next round.
        """
        try:
            text = get_clipboard_text() or ""
            prefix = f"QRT:MISSING:{req_id}:"
            if text.startswith(prefix):
                missing = decode_ranges(text[len(prefix):])
                if any(i < 0 or (page_count is not None and i >= page_count)
                       for i in missing):
                    blog_event("WARN", "DISPLAY", f"ignoring out-of-range MISSING: {sorted(missing)}", req_id)
                    return None
                return missing
        except (ValueError, TypeError) as exc:
            blog_event("WARN", "DISPLAY", f"ignoring malformed MISSING: {exc}", req_id)
        except Exception:
            pass
        return None

    def _check_new_request(self, req_id):
        """Detect a brand-new QRT:b64 request on the clipboard (different id).

        A-end only writes MISSING/DONE/CANCEL for the request B-end is currently
        playing. If it has moved on (finished that request, or the user started a
        new git operation), a fresh ``QRT:b64:`` request may be sitting on the
        clipboard. Without this check B-end would replay the old response for
        minutes (e.g. a missed DONE after a big clone) and never notice the new
        request. Returns the new request text, or None.
        """
        try:
            text = get_clipboard_text() or ""
        except Exception:
            return None
        if not text.startswith("QRT:b64:"):
            return None
        try:
            payload = json.loads(base64.b64decode(text[8:]))
        except Exception:
            return None
        new_id = payload.get("id")
        if new_id and new_id != req_id:
            return text
        return None

    def _no_missing_grace(self, req_id, page_count, retries=2, delay=0.3):
        """Confirm A-end really stopped writing MISSING for this request.

        A-end rewrites QRT:MISSING every ~500ms while it is still waiting for
        the response, so a missing signal can only mean it finished (DONE was
        processed/cleared) or it gave up (CANCEL). We re-check a couple of times
        over ~0.6s to survive a transient RDP clipboard drop before treating the
        response as complete — otherwise a huge clone would be replayed again and
        again for nothing (and block new requests).
        """
        for _ in range(retries):
            if self._check_missing(req_id, page_count) is not None:
                return False
            time.sleep(delay)
        return True

    def show_pages(self, payloads, req_id=None):
        """Display QR pages in a grid, with selective retransmission.

        Multi-QR: multiple QR codes are shown simultaneously in a grid.
        The grid size is auto-calculated from the screen dimensions — e.g.
        6 QRs (3x2) on a 1920x1080 screen, fewer on smaller screens.

        Selective retransmission: after each loop, B-end reads the
        QRT:MISSING:<req_id>:<indices> clipboard signal from A-end and
        only plays the pages A-end hasn't received yet. This avoids
        re-playing already-received pages and dramatically speeds up
        completion for large responses.

        After the fixed loops finish, B-end keeps re-playing ONLY the still-missing
        pages (backfill) until A-end reports DONE (collected everything) or CANCEL
        (client gone), or the user presses Esc — "play until complete". A stagnation
        guard (missing set unchanged between rounds) plus a round cap prevent
        infinite playback if A-end is stuck.

        Stops early on QRT:CANCEL (client gone), QRT:DONE:<req_id>
        (A-end collected everything), a manual stop (Esc key), or a brand-new
        QRT:b64 request arriving on the clipboard (so a missed DONE after a big
        clone can't make B-end replay the old response for minutes and ignore
        new git operations).
        Returns a terminal reason: ``done``, ``cancel``, ``manual``,
        ``new_request``, ``no_missing``, or ``exhausted``.
        """
        stop = None  # None / "cancel" / "done"
        stop_manual = False  # user pressed Esc
        self._pending_new_request = None  # new QRT:b64 text seen while playing

        def manual_stop(_event=None):
            nonlocal stop_manual
            stop_manual = True

        root = tk.Tk()
        root.attributes("-topmost", True)
        root.overrideredirect(True)
        root.configure(background="black")
        root.bind_all("<Escape>", manual_stop)
        root.focus_force()

        sw, sh = get_screen_size()
        root.geometry(f"{sw}x{sh}+0+0")

        # Normal full-screen capacity (used to split a long response into frames).
        normal_cols, normal_rows, normal_box = self._calc_grid(sw, sh)
        normal_capacity = normal_cols * normal_rows
        blog_event("INFO", "DISPLAY", f"screen={sw}x{sh}, grid={normal_cols}x{normal_rows}={normal_capacity}, box={normal_box}, pages={len(payloads)}, loops={self.loops}", req_id)

        # One centered label shows the whole composed frame. Every frame is a
        # single PIL canvas with all cell QRs pasted in, so a frame needs exactly
        # ONE ImageTk.PhotoImage conversion instead of one per QR page. On a slow
        # cloud desktop each per-page conversion costs ~220ms, i.e. ~1.8s per
        # 8-page frame; composing keeps the total pixel count about the same but
        # reduces that to a single conversion per frame.
        screen_label = tk.Label(root, bg="black")
        screen_label.place(relx=0.5, rely=0.5, anchor="center")

        # Page PIL cache (grayscale QR images, before the PhotoImage step).
        # Layout dimensions are part of the key because sparse retransmission
        # frames render larger QR images than normal full frames. Bounded LRU:
        # a huge clone response (thousands of pages) must not keep every page
        # in RAM. Pages play in order, so the sliding window is small; a
        # scrolled-out page re-renders in a few ms during backfill.
        page_cache = {}
        page_cache_max = 256

        # Composed-frame PhotoImage cache. Frames replay identically across
        # loops, so the expensive single conversion runs once per frame. Sized
        # for typical responses (~13 frames for a 100-page fetch); bigger
        # responses simply re-compose evicted frames.
        frame_cache = {}
        frame_cache_max = 48

        # Render timing stats, logged once at the end of playback so the real
        # desktop shows how close we are to hiding render behind page_ms.
        render_stats = {"frames": 0, "hits": 0, "compose_ms": 0.0,
                        "convert_ms": 0.0}

        def get_page_image(page_index, qr_w, qr_h):
            """Render one QR page as a grayscale PIL image (cached).

            Pure PIL work is fast; the expensive step is the single
            PhotoImage conversion per composed frame.
            """
            key = (page_index, qr_w, qr_h)
            img = page_cache.pop(key, None)
            if img is not None:
                page_cache[key] = img  # re-insert: dict order = recency (LRU)
                return img
            # Render every module at an integer pixel size. Avoid LANCZOS or
            # fractional CSS-style scaling: it blurs module edges after RDP
            # compression. Meta uses its natural smaller QR version; data pages
            # stay fixed at Version 40.
            if page_index == 0:
                img = make_qr(payloads[page_index], version=None, verbose=False,
                              box_size=1)
            else:
                img = make_qr(payloads[page_index], verbose=False, box_size=1)
            integer_scale = max(1, min(qr_w // img.width, qr_h // img.height))
            resampling = getattr(Image, "Resampling", Image)
            img = img.resize(
                (img.width * integer_scale, img.height * integer_scale),
                resampling.NEAREST,
            )
            if img.mode != "L":
                img = img.convert("L")
            page_cache[key] = img
            if len(page_cache) > page_cache_max:
                page_cache.pop(next(iter(page_cache)))  # evict least-recently used
            return img

        def get_frame_photo(frame_indices, cols, rows, qr_w, qr_h):
            """Compose one frame and convert it to a Tk PhotoImage (cached)."""
            fkey = (cols, rows, qr_w, qr_h, tuple(frame_indices))
            photo = frame_cache.pop(fkey, None)
            if photo is not None:
                frame_cache[fkey] = photo  # re-insert: dict order = recency (LRU)
                render_stats["hits"] += 1
                return photo
            t_start = time.monotonic()
            images = [get_page_image(idx, qr_w, qr_h) for idx in frame_indices]
            canvas = compose_qr_frame(images, cols, rows, qr_w, qr_h)
            t_composed = time.monotonic()
            photo = ImageTk.PhotoImage(canvas, master=root)
            t_done = time.monotonic()
            render_stats["frames"] += 1
            render_stats["compose_ms"] += (t_composed - t_start) * 1000
            render_stats["convert_ms"] += (t_done - t_composed) * 1000
            frame_cache[fkey] = photo
            if len(frame_cache) > frame_cache_max:
                frame_cache.pop(next(iter(frame_cache)))  # evict LRU
            if (t_done - t_start) * 1000 > self.page_ms:
                blog_event("WARN", "RENDER",
                           f"frame {len(frame_indices)}pg grid={cols}x{rows} "
                           f"compose+convert {(t_done - t_start) * 1000:.0f}ms "
                           f"(compose {(t_composed - t_start) * 1000:.0f}ms, "
                           f"convert {(t_done - t_composed) * 1000:.0f}ms)",
                           req_id)
            return photo

        all_indices = list(range(len(payloads)))

        def build_frames(play_indices, duplicate_meta=False):
            """Split pages into frames and optionally fill spare cells with Meta.

            Meta is required before A-end knows how many data pages to expect. On
            the first all-pages frame, otherwise-empty cells carry up to two extra
            copies at different positions. No data page is displaced, so long
            responses keep the same number of frames. Sparse retransmissions use
            one large centered QR instead of duplicate copies.
            """
            frames = [play_indices[i:i + normal_capacity]
                      for i in range(0, len(play_indices), normal_capacity)]
            if duplicate_meta and frames and 0 in frames[0]:
                spare = normal_capacity - len(frames[0])
                frames[0].extend([0] * min(2, spare))
            return frames

        def frame_layout(frame_indices):
            """Use larger centered QRs whenever a frame contains fewer pages."""
            layout_cols, layout_rows, layout_box = self._calc_grid(
                sw, sh, item_count=len(frame_indices)
            )
            layout_cell_w = sw // layout_cols
            layout_cell_h = sh // layout_rows
            return (layout_cols, layout_rows, layout_box,
                    int(layout_cell_w * 0.9), int(layout_cell_h * 0.9))

        def play_round(play_indices, round_label="", duplicate_meta=False):
            """Play one round, with optional Meta redundancy and adaptive layouts."""
            nonlocal stop, stop_manual, sw, sh, normal_cols, normal_rows, \
                normal_box, normal_capacity
            if stop or stop_manual or not play_indices:
                return

            frames = build_frames(play_indices, duplicate_meta=duplicate_meta)
            for frame_idx, frame_indices in enumerate(frames):
                if stop or stop_manual:
                    break

                cols, rows, box_size, max_qr_w, max_qr_h = frame_layout(frame_indices)

                # Compose all pages of this frame into one PhotoImage and show
                # it. Duplicate page indices intentionally reuse the same image
                # while appearing in different cells of the composed canvas.
                photo = get_frame_photo(frame_indices, cols, rows,
                                        max_qr_w, max_qr_h)
                screen_label.configure(image=photo)
                screen_label.image = photo

                root.update()
                if round_label and (frame_idx < 3 or frame_idx == len(frames) - 1):
                    blog_event("INFO", "DISPLAY", f"{round_label} frame {frame_idx + 1}/{len(frames)} pages={frame_indices} grid={cols}x{rows} box={box_size}", req_id)

                # Pre-render the next frame during this frame's hold window.
                rendered_next = False
                remaining_ms = self.page_ms
                while remaining_ms > 0 and not stop and not stop_manual:
                    if not rendered_next and frame_idx + 1 < len(frames):
                        t_render = time.monotonic()
                        next_frame = frames[frame_idx + 1]
                        ncols, nrows, _, next_w, next_h = frame_layout(next_frame)
                        get_frame_photo(next_frame, ncols, nrows, next_w, next_h)
                        rendered_next = True
                        remaining_ms -= (time.monotonic() - t_render) * 1000
                        continue

                    chunk = min(remaining_ms, 200)
                    time.sleep(chunk / 1000)
                    remaining_ms -= chunk
                    if remaining_ms > 0:
                        root.update()
                    stop = self._check_stop(req_id)

                    # A brand-new request may arrive while we replay an old
                    # response (e.g. a missed DONE after a big clone). Abort
                    # playback so run() can serve it immediately instead of
                    # ignoring it for minutes.
                    if not stop:
                        new_req = self._check_new_request(req_id)
                        if new_req:
                            self._pending_new_request = new_req
                            stop = "new_request"
                            break

                    # If viewport changes mid-frame, abort this round and let the
                    # next selective-retransmission/backfill round repartition the
                    # still-missing pages using the new capacity. Never truncate an
                    # old large frame into a smaller grid.
                    new_sw, new_sh = get_screen_size()
                    if new_sw != sw or new_sh != sh:
                        sw, sh = new_sw, new_sh
                        normal_cols, normal_rows, normal_box = self._calc_grid(sw, sh)
                        normal_capacity = normal_cols * normal_rows
                        root.geometry(f"{sw}x{sh}+0+0")
                        page_cache.clear()
                        frame_cache.clear()
                        blog_event("INFO", "DISPLAY", f"viewport resized to {sw}x{sh}; current round aborted, next round grid={normal_cols}x{normal_rows}", req_id)
                        return

        # ---- Primary loops: first round plays all pages, later rounds only missing ----
        for loop_num in range(self.loops):
            if stop or stop_manual:
                break

            # Determine which pages to play this loop
            if loop_num > 0:
                missing = self._check_missing(req_id, len(payloads))
                if missing is not None:
                    play_indices = sorted(missing)
                    blog_event("INFO", "DISPLAY", f"loop {loop_num + 1}/{self.loops}: playing {len(play_indices)} missing pages", req_id)
                elif self._no_missing_grace(req_id, len(payloads)):
                    # A-end writes MISSING every ~500ms while it waits. Its
                    # absence means the request finished (DONE) or A-end gave up
                    # (CANCEL). Stop instead of replaying the whole response.
                    stop = self._check_stop(req_id) or "no_missing"
                    blog_event("INFO", "DONE", f"loop {loop_num + 1}/{self.loops}: no MISSING; A-end no longer waiting, stopping ({stop})", req_id)
                    break
                else:
                    play_indices = all_indices
                    blog_event("INFO", "DISPLAY", f"loop {loop_num + 1}/{self.loops}: MISSING transiently absent, replaying all pages", req_id)
            else:
                play_indices = all_indices
                blog_event("INFO", "DISPLAY", "loop 1: playing all pages", req_id)

            if not play_indices:
                blog_event("INFO", "DONE", "MISSING empty; all pages received", req_id)
                break

            play_round(play_indices, f"Loop {loop_num + 1}/{self.loops}",
                       duplicate_meta=(loop_num == 0))

        # ---- Backfill: keep re-playing missing pages until done/cancel ----
        # After the fixed loops, if A-end still hasn't collected everything, keep
        # re-playing ONLY the still-missing pages until A-end reports DONE (all
        # received) or CANCEL (client gone), or the user presses Esc — "play until
        # complete". A stagnation guard (missing set unchanged between rounds) and
        # a round cap avoid infinite playback if A-end is stuck (e.g. a page that
        # can never be decoded).
        extra_rounds = 0
        prev_missing_set = None
        unchanged_rounds = 0
        while not stop and not stop_manual and extra_rounds < self.max_extra_rounds:
            missing = self._check_missing(req_id, len(payloads))
            if missing is not None:
                if len(missing) == 0:
                    blog_event("INFO", "DONE", "MISSING empty; all pages received", req_id)
                    stop = "done"
                    break
                play_indices = sorted(missing)
                blog_event("INFO", "DISPLAY", f"backfill {extra_rounds + 1}: playing {len(play_indices)} missing pages", req_id)
            else:
                # No MISSING signal — A-end finished (DONE) or is gone (CANCEL).
                # Confirm over ~0.6s (A-end rewrites MISSING every ~500ms), then
                # stop instead of replaying the entire response again and again.
                if self._no_missing_grace(req_id, len(payloads)):
                    stop = self._check_stop(req_id) or "no_missing"
                    blog_event("INFO", "DISPLAY", "backfill: no MISSING; A-end no longer waiting, stopping", req_id)
                    break
                play_indices = all_indices
                blog_event("INFO", "DISPLAY", f"backfill {extra_rounds + 1}: MISSING transiently absent, replaying all pages", req_id)

            # Allow several unchanged rounds: one repeated loss is normal over RDP.
            cur_missing_set = set(play_indices)
            if prev_missing_set == cur_missing_set:
                unchanged_rounds += 1
                if unchanged_rounds >= 3:
                    blog_event("WARN", "DISPLAY", f"missing pages unchanged for {unchanged_rounds} rounds; stopping", req_id)
                    break
            else:
                unchanged_rounds = 0
            prev_missing_set = cur_missing_set

            play_round(play_indices, f"Backfill {extra_rounds + 1}")
            extra_rounds += 1

        # ---- Final status + 2s buffer for A-end to decode the last frames ----
        if stop_manual:
            blog_event("INFO", "DISPLAY", "manual stop by Esc", req_id)
        elif stop == "cancel":
            blog_event("INFO", "CANCEL", "QR display cancelled by QRT:CANCEL", req_id)
        elif stop == "new_request":
            # A newer request is waiting on the clipboard; close immediately so
            # the main loop can serve it — no 2s decode buffer needed.
            blog_event("INFO", "DISPLAY", "aborted playback for a newer request", req_id)
        elif stop == "no_missing":
            blog_event("INFO", "DONE", "no MISSING signal; A-end stopped waiting, stopping playback", req_id)
        elif stop == "done":
            blog_event("INFO", "DONE", "A-end reported DONE; stopping QR playback", req_id)
        else:
            if extra_rounds > 0:
                blog_event("INFO", "DISPLAY", "backfill finished; holding final decode buffer", req_id)
            else:
                blog_event("INFO", "DISPLAY", "fixed loops finished; holding final decode buffer", req_id)
            for _ in range(20):
                time.sleep(0.1)
                root.update()
                stop = self._check_stop(req_id)
                if stop or stop_manual:
                    break
            blog_event("INFO", "DISPLAY", "QR window closed", req_id)

        if render_stats["frames"]:
            blog_event("INFO", "RENDER",
                       f"render stats: frames={render_stats['frames']}, "
                       f"cache hits={render_stats['hits']}, "
                       f"compose avg={render_stats['compose_ms'] / render_stats['frames']:.0f}ms, "
                       f"convert avg={render_stats['convert_ms'] / render_stats['frames']:.0f}ms, "
                       f"total={(render_stats['compose_ms'] + render_stats['convert_ms']) / 1000:.1f}s",
                       req_id)

        root.destroy()
        if stop_manual:
            return "manual"
        return stop or "exhausted"


def show_qr_html(payloads, page_ms=200):
    """Fallback: generate HTML + open in default browser."""
    img_tags = []
    for payload in payloads:
        img = make_qr(payload, verbose=False)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        img_tags.append(
            f'<img id="p{len(img_tags)}" src="data:image/png;base64,{b64}" style="display:none">'
        )

    total = len(payloads)
    pages_js = ",".join(f'document.getElementById("p{i}")' for i in range(total))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>QR Tunnel</title>
<style>
  body{{margin:0;background:#000;height:100vh;display:flex;
       justify-content:center;align-items:center;overflow:hidden}}
  img{{max-width:80vw;max-height:80vh;display:none}}
</style></head><body>
{''.join(img_tags)}
<script>
var pages=[{pages_js}], i=0, total={total}, ms={page_ms};
function show(n){{
  for(var j=0;j<total;j++) pages[j].style.display='none';
  pages[n].style.display='block';
}}
show(0);
setInterval(function(){{i=(i+1)%total; show(i)}}, ms);
</script></body></html>"""

    fd, path = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    os.startfile(path)
    return path


# ---- HTTP forwarding ----

class ForwardCancelled(Exception):
    """Raised when A-end cancels before/during intranet forwarding."""


class ForwardControl:
    """Coordinate cancellation with the worker that owns HTTPConnection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._conn = None

    def register(self, conn):
        with self._lock:
            if self._cancelled:
                conn.close()
                raise ForwardCancelled()
            self._conn = conn

    def is_cancelled(self):
        with self._lock:
            return self._cancelled

    def cancel(self):
        with self._lock:
            self._cancelled = True
            conn = self._conn
            self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def forward_request(method, path, headers, body_b64, target, control=None, req_id=None):
    """Forward HTTP request to git server, return (status, headers, body)."""
    conn = HTTPConnection(target[0], target[1], timeout=120)
    if control is not None:
        control.register(conn)
    try:
        if control is not None and control.is_cancelled():
            raise ForwardCancelled()
        body = base64.b64decode(body_b64) if body_b64 else None
        hdrs = dict(headers) if headers else {}
        hdrs.pop("Connection", None)
        hdrs.pop("Transfer-Encoding", None)
        hdrs.pop("Keep-Alive", None)
        hdrs.pop("Proxy-Connection", None)
        # Ask upstream for identity so we never forward a compressed body that
        # A-end would then mislabel after stripping Content-Encoding.
        hdrs["Accept-Encoding"] = "identity"
        blog_event("INFO", "HTTP", f"forward {method} {path} -> {target[0]}:{target[1]}, body={len(body) if body else 0}B", req_id)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        resp_body = resp.read()
        resp_headers = resp.getheaders()
        # If upstream ignored identity and returned a compressed body, decode it
        # and drop the Content-Encoding header so A-end forwards plain bytes.
        encoding = None
        for k, v in resp_headers:
            if k.lower() == "content-encoding":
                encoding = (v or "").strip().lower()
        if encoding in ("gzip", "x-gzip"):
            try:
                resp_body = gzip.decompress(resp_body)
                resp_headers = [(k, v) for k, v in resp_headers if k.lower() != "content-encoding"]
            except Exception as exc:
                blog_event("WARN", "HTTP", f"gzip decode failed, forwarding raw: {exc}", req_id)
        elif encoding == "deflate":
            try:
                import zlib
                resp_body = zlib.decompress(resp_body)
                resp_headers = [(k, v) for k, v in resp_headers if k.lower() != "content-encoding"]
            except Exception as exc:
                blog_event("WARN", "HTTP", f"deflate decode failed, forwarding raw: {exc}", req_id)
        blog_event("INFO", "HTTP", f"response HTTP {resp.status}, body={len(resp_body)}B", req_id)
        return resp.status, resp_headers, resp_body
    finally:
        conn.close()
        if control is not None:
            with control._lock:
                if control._conn is conn:
                    control._conn = None


# ---- Response encoding ----

def _compress_plan(body, chunk_bytes):
    """Return (data_bytes, use_gzip, n_chunks) for a body using the exact same
    gzip rule as encode_response. Used to size the 507 cap and the Bulk switch
    against the *compressed* page count, so compressible responses are not
    wrongly rejected pre-compression.
    """
    compressed = gzip.compress(body)
    use_gzip = len(compressed) < len(body) * 0.95
    data = compressed if use_gzip else body
    n_chunks = (len(data) + chunk_bytes - 1) // chunk_bytes
    return data, use_gzip, n_chunks


def _select_transfer_plan(body, chunk_bytes, bulk_chunk, bulk_threshold,
                          disable_bulk=False, peer_features=None):
    """Choose normal or Bulk standard-QR transport for one response.

    Returns ``(use_bulk, effective_chunk, normal_chunks, effective_chunks)``.
    The threshold uses the normal path's *compressed* page count; the cap must
    use ``effective_chunks`` after Bulk. Bulk is enabled only when the peer's
    request features explicitly contain ``bulk``; absent/legacy metadata falls
    back to the normal QR path.
    """
    _, _, normal_chunks = _compress_plan(body, chunk_bytes)
    peer_bulk = isinstance(peer_features, list) and "bulk" in peer_features
    use_bulk = not disable_bulk and peer_bulk and normal_chunks > bulk_threshold
    effective_chunk = bulk_chunk if use_bulk else chunk_bytes
    if effective_chunk == chunk_bytes:
        effective_chunks = normal_chunks
    else:
        _, _, effective_chunks = _compress_plan(body, effective_chunk)
    return use_bulk, effective_chunk, normal_chunks, effective_chunks


def encode_response(status, headers, body, req_id, chunk_bytes, bulk=False):
    """
    Encode HTTP response as QR page payloads (raw bytes). QR images are
    rendered lazily at display time — pre-rendering thousands of pages
    would exhaust memory.
    Returns [meta_payload, data_payload_1, data_payload_2, ...]
    Binary data page: [0x01][seq:4B BE][id_hex:32B ASCII][chunk]
    ``bulk=True`` marks the meta so A-end and logs can see the high-throughput
    path was used; it never changes the wire format (unknown meta fields are
    ignored by older A-end builds).
    """
    data, use_gzip, n_chunks = _compress_plan(body, chunk_bytes)
    blog_event("INFO", "ENCODE", f"compress {len(body)}B -> {len(data)}B ({'gzip' if use_gzip else 'raw'}), chunk={chunk_bytes}B", req_id)

    chunks = []
    off = 0
    while off < len(data):
        chunks.append(data[off : off + chunk_bytes])
        off += chunk_bytes

    # Meta page (JSON)
    id_hex = req_id.replace("-", "").lower()
    meta = {
        "meta": True,
        "protocol": PROTOCOL_VERSION,
        "server_version": VERSION,
        "id": req_id,
        "status": status,
        "headers": [(h[0], h[1]) for h in headers],
        "chunks": len(chunks),
        "gzip": use_gzip,
        "raw_len": len(body),
    }
    if bulk:
        meta["bulk"] = True
    meta_json = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    blog_event("INFO", "ENCODE", f"meta={len(meta_json)}B, data_chunks={len(chunks)}, total_pages={1 + len(chunks)}", req_id)
    pages = [meta_json]

    # Data pages
    for seq, chunk in enumerate(chunks, 1):
        header = struct.pack(">BI", 0x01, seq) + id_hex.encode("ascii")
        pages.append(header + chunk)

    return pages


# ---- Startup probe / capability advertisement ----

def build_probe_response():
    """Answer to A-end's startup capability probe.

    Returns (status, headers, body) advertising this B-end's role, version,
    protocol and feature set. The probe is answered locally — it is never
    forwarded to the intranet git server.
    """
    body = json.dumps({
        "probe": True,
        "role": "B",
        "version": VERSION,
        "protocol": PROTOCOL_VERSION,
        "features": FEATURES,
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"),
               ("Content-Length", str(len(body)))]
    return 200, headers, body


def is_probe_request(req):
    """True when a parsed request is our capability probe.

    Detected by the explicit ``probe`` flag (set by new A-end builds) or by the
    reserved probe path — either way it must be a GET.
    """
    if not isinstance(req, dict):
        return False
    return req.get("probe") is True or (
        req.get("method") == "GET" and req.get("path") == PROBE_PATH)


# ---- Main ----

class BTunnel:
    def __init__(self, target_host="192.168.21.14", target_port=8888,
                 page_ms=200, chunk_bytes=2800, display="tkinter", loops=3,
                 ack_ms=800, max_pages=500, max_qr=0, min_box_size=2,
                 disable_bulk=False, bulk_threshold=400, bulk_chunk=2900):
        if page_ms <= 0:
            raise ValueError("page_ms must be > 0")
        if chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be > 0")
        if chunk_bytes > MAX_CHUNK_BYTES:
            raise ValueError(f"chunk_bytes must be <= {MAX_CHUNK_BYTES} for Version-40 QR")
        if loops <= 0:
            raise ValueError("loops must be > 0")
        if ack_ms < 0:
            raise ValueError("ack_ms must be >= 0")
        if max_pages <= 0:
            raise ValueError("max_pages must be > 0")
        if bulk_threshold <= 0:
            raise ValueError("bulk_threshold must be > 0")
        if bulk_chunk <= 0:
            raise ValueError("bulk_chunk must be > 0")
        if bulk_chunk < chunk_bytes:
            raise ValueError("bulk_chunk must be >= chunk_bytes")
        if bulk_chunk > MAX_CHUNK_BYTES:
            raise ValueError(f"bulk_chunk must be <= {MAX_CHUNK_BYTES} for Version-40 QR")
        self.target = (target_host, target_port)
        self.page_ms = page_ms
        self.chunk_bytes = chunk_bytes
        self.display_mode = display
        self.ack_ms = ack_ms
        self.max_pages = max_pages
        self.disable_bulk = disable_bulk
        self.bulk_threshold = bulk_threshold
        self.bulk_chunk = bulk_chunk
        self.display = QRDisplay(page_ms=page_ms, loops=loops,
                                 max_qr=max_qr, min_box_size=min_box_size)
        self.processed = {}  # req_id -> timestamp

    def log(self, msg):
        blog_event("INFO", "MAIN", msg)

    def log_req(self, level, phase, message, req_id):
        blog_event(level, phase, message, req_id)

    def wait_clipboard(self, poll_ms=200):
        """Wait until a valid, unprocessed QRT:b64 request is present.

        B-end is strictly READ-ONLY on the clipboard. Control states
        (DONE/CANCEL/MISSING/IDLE), stale requests and unrelated user clipboard
        text are only used as the current baseline; B-end never clears or
        overwrites them. This prevents the bidirectional RDP clipboard channel
        from feeding B-end cleanup writes back into A-end and masking the next
        request.
        """
        try:
            last = get_clipboard_text() or ""
        except Exception as exc:
            blog_event("WARN", "CLIP", f"startup read retry: {exc}")
            last = ""

        def new_request(text):
            if not text.startswith("QRT:b64:"):
                return None
            req = self.parse_request(text)
            if not req:
                return None
            req_id = req.get("id")
            if not req_id or req_id in self.processed:
                return None
            return text

        present = new_request(last)
        if present:
            blog_event("INFO", "CLIP",
                       f"request already present when waiting started; bytes={len(present)}")
            return present

        poll_count = 0
        while True:
            time.sleep(poll_ms / 1000)
            poll_count += 1
            try:
                cur = get_clipboard_text() or ""
            except Exception as exc:
                if poll_count % 10 == 0:
                    blog_event("WARN", "CLIP", f"read retry: {exc}")
                continue

            present = new_request(cur)
            if present:
                blog_event("INFO", "CLIP",
                           f"new request on clipboard: bytes={len(present)}")
                return present

            # Everything other than a fresh QRT:b64 request is an idle/control
            # baseline. Remember changes but do not return them to the request
            # processor and, critically, do not write anything back.
            if cur != last:
                if cur.startswith("QRT:b64:"):
                    state = "stale/malformed request"
                elif cur.startswith("QRT:CANCEL:"):
                    state = "stale CANCEL"
                elif cur.startswith("QRT:DONE:"):
                    state = "stale DONE"
                elif cur.startswith("QRT:MISSING:"):
                    state = "stale MISSING"
                elif cur.startswith(IDLE_MARKER):
                    state = "IDLE"
                elif cur:
                    state = "non-QRT content"
                else:
                    state = "empty"
                blog_event("INFO", "CLIP", f"idle clipboard changed: {state}")
                last = cur

            # Periodic heartbeat includes B-end's actual clipboard view. During
            # a no-ACK failure this proves whether the A-end request crossed RDP.
            if poll_count % 50 == 0:
                preview = cur[:60].replace("\n", "\\n").replace("\r", "\\r")
                blog_event("INFO", "IDLE",
                           f"waiting for request ({poll_count} polls); clipboard={preview!r}")

    def parse_request(self, text):
        text = (text or "").strip()
        if not text.startswith("QRT:b64:"):
            return None
        try:
            req = json.loads(base64.b64decode(text[8:]))
        except Exception:
            return None
        if not isinstance(req, dict):
            return None
        req_id = req.get("id")
        if not isinstance(req_id, str) or not req_id:
            return None
        method = req.get("method")
        if not isinstance(method, str) or not method:
            return None
        path = req.get("path")
        if not isinstance(path, str) or not path:
            return None
        headers = req.get("headers", [])
        if not isinstance(headers, list):
            return None
        for pair in headers:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return None
            if not all(isinstance(v, str) for v in pair):
                return None
        body = req.get("body")
        if body is not None and not isinstance(body, str):
            return None
        features = req.get("features")
        if features is not None:
            if not isinstance(features, list) or not all(isinstance(v, str) for v in features):
                return None
        return req

    def cleanup(self, max_age=300):
        now = time.time()
        for rid, ts in list(self.processed.items()):
            if now - ts > max_age:
                del self.processed[rid]

    def _is_cancelled(self, req_id):
        try:
            return get_clipboard_text() == f"QRT:CANCEL:{req_id}"
        except Exception:
            return False

    def observe_completed_clipboard(self, req_id):
        """Inspect post-request clipboard state without modifying it.

        Clipboard transport is A->B only. B-end must never clear/write the
        cloud-desktop clipboard because HSRClient may synchronize that write
        back toward A-end and overwrite/control the next A->B update. Returns
        True only when a different/new clipboard value is already present.
        """
        try:
            cur = get_clipboard_text() or ""
            belongs_to_old = (
                cur == f"QRT:DONE:{req_id}"
                or cur == f"QRT:CANCEL:{req_id}"
                or cur.startswith(f"QRT:MISSING:{req_id}:")
                or cur.startswith(IDLE_MARKER)
            )
            if cur.startswith("QRT:b64:"):
                old_req = self.parse_request(cur)
                belongs_to_old = bool(old_req and old_req.get("id") == req_id)
            if cur and not belongs_to_old:
                self.log("Clipboard already contains next/new content; preserving it")
                return True
        except Exception as e:
            self.log(f"Clipboard observation error: {e}")
        return False

    def run(self):
        blog_event("INFO", "START", "B-end tunnel started")
        blog_event("INFO", "START", f"target={self.target[0]}:{self.target[1]}")
        blog_event("INFO", "START", f"chunk={self.chunk_bytes}B/page, page_ms={self.page_ms}, display={self.display_mode}")
        blog_event("INFO", "START",
                   f"bulk={'DISABLED' if self.disable_bulk else 'ENABLED'}, "
                   f"threshold={self.bulk_threshold}, bulk_chunk={self.bulk_chunk}B")
        blog_event("INFO", "IDLE", "waiting for QRT:b64 request")

        try:
            last_text = get_clipboard_text() or ""
        except Exception as exc:
            self.log(f"Initial clipboard read failed, will retry: {exc}")
            last_text = ""

        while True:
            try:
                text = self.wait_clipboard()
                self._process_request(text)
            except Exception as exc:
                import traceback
                blog_event("ERROR", "MAIN", f"unexpected error: {exc!r}; continuing", None)
                traceback.print_exc()
                time.sleep(0.5)

    def _process_request(self, text):
        """Handle one clipboard request (a QRT text) and keep the tunnel alive.

        Any error raised inside is caught by run() so a single bad request can
        never kill B-end (previously an uncaught exception in show_pages would
        exit the process, leaving the console window open and making the tunnel
        look hung / unresponsive).
        """
        if text and text.startswith("QRT:CANCEL:"):
            cancel_id = text[len("QRT:CANCEL:"):]
            self.log(f"Cancel signal received for req={cancel_id[:8]}... (no active display)")
            return

        # Stale DONE signal (for a request we already finished playing) — ignore.
        # Do NOT clear the clipboard: A-end rewrites DONE/MISSING while it waits,
        # clearing here would fight it and loop forever.
        if text and text.startswith("QRT:DONE:"):
            self.log(f"Ignoring stale DONE signal: {text[:50]}")
            return

        # Stale MISSING signal (for a request we already finished) — ignore.
        # Do NOT clear the clipboard (same reason as DONE).
        if text and text.startswith("QRT:MISSING:"):
            self.log(f"Ignoring stale MISSING signal: {text[:50]}")
            return

        req = self.parse_request(text)
        if not req:
            # Log what we got instead of a valid request
            preview = (text or "")[:100].replace('\n', '\\n').replace('\r', '\\r')
            self.log(f"Ignored non-QRT clipboard content: len={len(text or '')}, preview={preview}")
            return

        req_id = req.get("id", "")
        if req_id in self.processed:
            self.log(f"Skip duplicate {req_id[:8]}...")
            return
        is_probe = is_probe_request(req)
        request_protocol = req.get("protocol")
        request_version = req.get("client_version")
        compatibility_error = None
        if request_protocol and request_protocol != PROTOCOL_VERSION:
            compatibility_error = f"protocol mismatch: A={request_protocol}, B={PROTOCOL_VERSION}"
        elif request_version and request_version != VERSION:
            compatibility_error = f"version mismatch: A={request_version}, B={VERSION}"
        if compatibility_error:
            self.log_req("ERROR", "PROTO", compatibility_error, req_id)
        elif request_protocol or request_version:
            self.log_req("INFO", "PROTO", f"compatible peer: protocol={request_protocol or 'legacy'}, version={request_version or 'legacy'}", req_id)

        method = req.get("method", "GET")
        path = req.get("path", "/")
        headers = req.get("headers", [])
        body_b64 = req.get("body")
        retry = req.get("retry", 1)
        self.log_req("INFO", "REQ", f"{method} {path} (A attempt {retry})", req_id)
        _write_summary({
            "status": "in_progress",
            "request_id": req_id,
            "method": method,
            "path": path,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })

        # A request carrying metadata must match this endpoint exactly. Legacy
        # requests without metadata remain supported during rollout. The startup
        # capability probe is answered locally (never forwarded upstream) and is
        # exempt from the 426 gate so A-end can actually discover a mismatch.
        if is_probe_request(req):
            forward_result = {"response": build_probe_response()}
            forward_done = threading.Event()
            forward_done.set()
            forward_control = None
            worker = None
        elif compatibility_error:
            forward_result = {
                "response": (426, [("Content-Type", "text/plain; charset=utf-8")],
                              ("QR Tunnel: " + compatibility_error).encode("utf-8"))
            }
            forward_done = threading.Event()
            forward_done.set()
            forward_control = None
            worker = None
        else:
            forward_result = {}
            forward_done = threading.Event()
            forward_control = ForwardControl()

            def forward_worker():
                try:
                    forward_result["response"] = forward_request(
                        method, path, headers, body_b64, self.target, forward_control, req_id
                    )
                except ForwardCancelled:
                    forward_result["cancelled"] = True
                except Exception as exc:
                    forward_result["error"] = exc
                finally:
                    forward_done.set()

            worker = threading.Thread(target=forward_worker, daemon=False)
            worker.start()

        if worker is not None:
            self.log_req("INFO", "ACK", f"showing ACK for {self.ack_ms}ms; forwarding started", req_id)
        else:
            self.log_req("INFO", "ACK", f"showing ACK for {self.ack_ms}ms; upstream skipped", req_id)
        try:
            show_ack(req_id, self.ack_ms)
        except Exception as e:
            self.log_req("ERROR", "ACK", f"display failed: {e}", req_id)

        # Wait in short intervals so CANCEL can abort a slow intranet request.
        # Local-response requests (capability probe, 426 compatibility error)
        # set forward_done immediately and keep worker/forward_control None;
        # they must never touch either (both would raise AttributeError).
        cancelled_while_forwarding = False
        while not forward_done.wait(0.1):
            if self._is_cancelled(req_id):
                cancelled_while_forwarding = True
                if forward_control is not None:
                    forward_control.cancel()
                break
        if cancelled_while_forwarding and worker is not None:
            self.log_req("WARN", "CANCEL", "cancelled while waiting for intranet response", req_id)
            worker.join(timeout=5.0)
            if worker.is_alive():
                self.log_req("WARN", "CANCEL", "forward worker did not stop promptly; waiting", req_id)
                worker.join()
            self.observe_completed_clipboard(req_id)
            self.processed[req_id] = time.time()
            self.cleanup()
            return
        if worker is not None:
            worker.join()
            if forward_result.get("cancelled") or self._is_cancelled(req_id):
                forward_control.cancel()
                self.observe_completed_clipboard(req_id)
                self.processed[req_id] = time.time()
                self.cleanup()
                return
        if "error" in forward_result:
            self.log_req("ERROR", "HTTP", f"forward failed: {forward_result['error']}", req_id)
            status, resp_headers, resp_body = 502, [], str(forward_result["error"]).encode()
        else:
            status, resp_headers, resp_body = forward_result["response"]

        # Choose normal/Bulk from the *compressed* normal-page count, then apply
        # max_pages to the effective count after Bulk. This fixes both the old
        # pre-compression 507 bug and the ordering bug where Bulk could have
        # reduced a response under the cap but never got the chance.
        use_bulk, eff_chunk, normal_chunks, effective_chunks = _select_transfer_plan(
            resp_body, self.chunk_bytes, self.bulk_chunk,
            self.bulk_threshold, self.disable_bulk,
            peer_features=req.get("features"))
        self.log_req("INFO", "ENCODE",
                     f"HTTP {status}, body={len(resp_body)}B, "
                     f"compressed_chunks={normal_chunks}", req_id)
        if use_bulk:
            self.log_req("INFO", "BULK",
                         f"bulk mode: chunk {self.chunk_bytes}->{eff_chunk}B, "
                         f"pages {normal_chunks}->{effective_chunks} "
                         f"(threshold {self.bulk_threshold})", req_id)

        # Cap uses the final transport plan. If still too large, replace it with
        # a small 507 response on the normal path (never mark an error as Bulk).
        if effective_chunks > self.max_pages:
            self.log(f"Response too large (~{effective_chunks} pages > cap {self.max_pages}), replying 507")
            msg = (f"QR Tunnel: response too large ({len(resp_body)}B, "
                   f"~{effective_chunks} QR pages after compression/Bulk, "
                   f"cap {self.max_pages}). Use gitsync for clone / large transfers.")
            status, resp_headers, resp_body = 507, [], msg.encode("utf-8")
            use_bulk = False
            eff_chunk = self.chunk_bytes
            _, _, effective_chunks = _compress_plan(resp_body, eff_chunk)

        if effective_chunks > 800:
            try:
                sw, sh = get_screen_size()
                cols, rows, _ = self.display._calc_grid(sw, sh)
                per_frame = cols * rows
                est_min = effective_chunks / per_frame * self.page_ms / 1000 / 60
                self.log_req("INFO", "ENCODE",
                             f"large response: ~{effective_chunks} pages, grid {cols}x{rows}, "
                             f"est ~{est_min:.1f} min per full pass (page_ms={self.page_ms})", req_id)
            except Exception:
                pass

        pages = encode_response(status, resp_headers, resp_body, req_id,
                                eff_chunk, bulk=use_bulk)
        self.log_req("INFO", "ENCODE", f"prepared {len(pages)} QR pages", req_id)
        summary_update = {
            "status": "displaying",
            "request_id": req_id,
            "http_status": status,
            "response_bytes": len(resp_body),
            "qr_pages": len(pages),
        }
        if use_bulk:
            summary_update["bulk"] = True
            summary_update["bulk_chunk"] = eff_chunk
        _write_summary(summary_update)

        terminal_reason = "exhausted"
        if self.display_mode == "html":
            show_qr_html(pages, self.page_ms)
            time.sleep((len(pages) * self.page_ms) / 1000 + 1)
        else:
            terminal_reason = self.display.show_pages(pages, req_id)
            if terminal_reason == "new_request":
                # A newer request arrived while we were replaying an old
                # response. Mark this one done; the new request is already on
                # the clipboard so wait_clipboard() returns it immediately.
                self.log_req("WARN", "DISPLAY", "aborted playback: newer request on clipboard", req_id)
                self.processed[req_id] = time.time()
                self.cleanup()
                _write_summary({"status": "superseded", "request_id": req_id, "terminal_reason": terminal_reason})
                return
            if terminal_reason == "cancel":
                self.log_req("WARN", "CANCEL", "QR display cancelled; request discarded", req_id)
                self.observe_completed_clipboard(req_id)
                self.processed[req_id] = time.time()
                self.cleanup()
                _write_summary({"status": "cancelled", "request_id": req_id, "terminal_reason": terminal_reason})
                return

        # Read-only handoff: inspect whether A-end already placed the next
        # request, then show STOPPED for the old one. B-end never writes or
        # clears the clipboard; this preserves strict A->B clipboard direction.
        has_new_content = self.observe_completed_clipboard(req_id)
        if terminal_reason == "done":
            try:
                show_stopped(req_id, hold_ms=500)
            except Exception as e:
                self.log(f"STOPPED confirmation error: {e}")

        self.processed[req_id] = time.time()
        self.cleanup()
        # Probe requests complete outside the git-transfer history; use a
        # distinct status so they never land in transfer-history.jsonl.
        _write_summary({"status": "probe_completed" if is_probe else "completed",
                        "request_id": req_id, "terminal_reason": terminal_reason})
        if has_new_content:
            self.log_req("INFO", "DONE", "new clipboard content preserved; continuing", req_id)
        else:
            self.log_req("INFO", "DONE", "old control state left untouched; ready for next request", req_id)


def main():
    configure_console_quickedit()
    if not acquire_single_instance():
        return
    _write_summary({"status": "starting"})

    preparse = argparse.ArgumentParser(add_help=False)
    preparse.add_argument(
        "--config", default=None,
        help="config.yaml path (default: <project root>\\config.yaml if present)")
    known, _ = preparse.parse_known_args()
    config_path = Path(known.config) if known.config else (
        PROJECT_ROOT / "config.yaml"
        if (PROJECT_ROOT / "config.yaml").is_file() else None)
    defaults = side_defaults(load_config(config_path), "b")

    ap = argparse.ArgumentParser(description="QR Tunnel B-end (cloud desktop)")
    ap.add_argument(
        "--config", default=str(config_path) if config_path else None,
        help="config.yaml path (default: <project root>\\config.yaml if present)")
    ap.add_argument("--target", default=defaults.get("target", "192.168.21.14:8888"),
                    help="Git server (default 192.168.21.14:8888)")
    ap.add_argument("--page-ms", type=int, default=defaults.get("page_ms", 200),
                    help="ms per QR frame (default 200; multi-QR mode recommends 300-500)")
    ap.add_argument("--chunk", type=int, default=defaults.get("chunk", 2800),
                    help="bytes per QR chunk (default 2800)")
    ap.add_argument("--loops", type=int, default=defaults.get("loops", 3),
                    help="how many times to loop QR pages (default 3; with selective "
                         "retransmission, later loops only play missing pages)")
    ap.add_argument("--ack-ms", type=int, default=defaults.get("ack_ms", 800),
                    help="ms to show ACK QR after receiving a request (default 800; "
                         "intranet request runs concurrently)")
    ap.add_argument("--max-pages", type=int, default=defaults.get("max_pages", 500),
                    help="max QR pages per response, larger replies get 507 (default 500)")
    ap.add_argument("--max-qr", type=int, default=defaults.get("max_qr", 0),
                    help="max QRs per frame for multi-QR display (default 0=auto, e.g. 6 on 1920x1080)")
    ap.add_argument("--min-box-size", type=int,
                    default=defaults.get("min_box_size", 2),
                    help="minimum box_size (pixels per QR module) for multi-QR (default 2; "
                         "increase to 3 if decoding is unreliable)")
    ap.add_argument("--display", choices=["tkinter", "html"],
                    default=defaults.get("display", "tkinter"),
                    help="QR display method (default tkinter)")
    ap.add_argument("--disable-bulk", action="store_true",
                    default=defaults.get("disable_bulk", False),
                    help="disable the Bulk high-throughput path (default: enabled when pages exceed threshold)")
    ap.add_argument("--bulk-threshold", type=int,
                    default=defaults.get("bulk_threshold", 400),
                    help="compressed pages above which Bulk uses a larger chunk (default 400)")
    ap.add_argument("--bulk-chunk", type=int,
                    default=defaults.get("bulk_chunk", 2900),
                    help="bytes per QR chunk in Bulk mode, near v40-L capacity (default 2900; must be >= --chunk)")
    args = ap.parse_args()

    if args.bulk_chunk < args.chunk:
        ap.error("--bulk-chunk must be >= --chunk")

    host, port = args.target.split(":")
    tunnel = BTunnel(
        target_host=host, target_port=int(port),
        page_ms=args.page_ms, chunk_bytes=args.chunk,
        display=args.display, loops=args.loops, ack_ms=args.ack_ms,
        max_pages=args.max_pages,
        max_qr=args.max_qr, min_box_size=args.min_box_size,
        disable_bulk=args.disable_bulk,
        bulk_threshold=args.bulk_threshold, bulk_chunk=args.bulk_chunk,
    )
    try:
        tunnel.run()
    except KeyboardInterrupt:
        blog_event("INFO", "STOP", "B-end stopped")
        release_single_instance()
        sys.exit(0)
    finally:
        release_single_instance()


if __name__ == "__main__":
    main()
