#!/usr/bin/env python3
"""
QR Tunnel - A端代理（Win10 ARM 虚拟机端）

在 Windows 上启动 HTTP 代理，监听 127.0.0.1:9999。
将 HTTP 请求写入剪贴板（QRT: 前缀），通过 RDP 剪贴板共享传给云桌面。
同时循环截屏，捕获云桌面显示的 QR 码，解码后组装响应返回。

用法:
    python a_proxy.py [--listen 127.0.0.1:9999] [--display-index -1] [--chunk 2800]

IDEA 配置远程仓库:
    URL: http://10.211.55.4:9999
    (Win10 ARM 虚拟机的 IP，A端代理监听地址)
"""

import sys
import os
import re
import json
import base64
import gzip
import struct
import time
import uuid
import threading
import argparse
import logging
import logging.handlers
import ctypes
from ctypes import wintypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import mss
import numpy as np
from PIL import Image
import zxingcpp


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
if not VERSION_FILE.exists():
    VERSION_FILE = PROJECT_ROOT / "VERSION"
try:
    VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
except OSError:
    VERSION = "0.5.0-dev"
PROTOCOL_VERSION = "qrtunnel-qr-1"
FEATURES = ["multiqr", "missing", "stopped", "idle-marker", "head", "probe", "bulk"]
SUMMARY_PATH = Path(__file__).resolve().parent / "logs" / "latest-transfer-summary.json"
HISTORY_PATH = Path(__file__).resolve().parent / "logs" / "transfer-history.jsonl"
LOG_PATH = Path(__file__).resolve().parent / "logs" / "tunnel.log"
SUMMARY_LOCK = threading.RLock()
_last_summary = {"version": VERSION, "role": "A", "status": "idle"}
_last_history_key = None


# ---- config.yaml loading (flat YAML subset; no PyYAML dependency) ----
# The A-end embeddable Python may not have pip, so keep configuration loading
# self-contained. Supported values: strings, quoted strings, bools, ints and
# floats. CLI arguments override config values; missing values use built-ins.
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
# Every console line is mirrored to logs/tunnel.log with a timestamp. Rotation
# keeps the file bounded: 5 MiB x 3 backups, so the log never grows unbounded
# on long-running sessions. The handler is deliberately detached (no
# propagation) so console output stays under our control.
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

# ---- Peer capability negotiation (populated by the startup probe) ----
# None = not yet probed. A successful probe fills this with the B-end's
# version/protocol/features; a legacy peer that does not understand the probe
# fills it with a "legacy" marker so later phases (Bulk etc.) can fall back.
_peer_capability = None  # dict or None
PROBE_PATH = "/__qrtunnel/probe"


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
        _last_summary = {**_last_summary, **update, "version": VERSION, "role": "A", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        try:
            SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SUMMARY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(_last_summary, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SUMMARY_PATH)
            terminal = _last_summary.get("status") in {"completed", "failed", "cancelled"}
            request_id = _last_summary.get("request_id")
            history_key = (request_id, _last_summary.get("status"))
            if terminal and request_id and history_key != _last_history_key:
                with HISTORY_PATH.open("a", encoding="utf-8") as history:
                    history.write(json.dumps(_last_summary, ensure_ascii=False) + "\n")
                _last_history_key = history_key
        except OSError as exc:
            log_event("WARN", "SUMMARY", f"write failed: {exc}")


def _summary_snapshot():
    with SUMMARY_LOCK:
        return dict(_last_summary)


# ---- Console logging ----

def short_id(req_id):
    """Return a stable short request ID for readable console logs."""
    return (req_id or "-").replace("-", "")[:8]


def log_event(level, phase, message, req_id=None):
    """Print one consistent A-end log line, mirrored to logs/tunnel.log.

    Format: [A][HH:MM:SS][LEVEL][PHASE][req:XXXXXXXX] message
    """
    req_tag = f"[req:{short_id(req_id)}]" if req_id else "[req:--------]"
    line = f"[A][{time.strftime('%H:%M:%S')}][{level}][{phase}]{req_tag} {message}"
    print(line, flush=True)
    if _FILE_LOGGER is not None:
        try:
            _FILE_LOGGER.info(line)
        except Exception:
            pass


def configure_console_quickedit():
    """Disable Windows console QuickEdit so accidental clicks cannot pause Python."""
    try:
        std_input_handle = ctypes.c_ulong(-10).value
        handle = _kernel32.GetStdHandle(std_input_handle)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError("no console input handle")
        original = wintypes.DWORD()
        if not _kernel32.GetConsoleMode(handle, ctypes.byref(original)):
            raise OSError("GetConsoleMode failed")
        enable_extended_flags = 0x0080
        enable_quick_edit_mode = 0x0040
        desired = (original.value | enable_extended_flags) & ~enable_quick_edit_mode
        if not _kernel32.SetConsoleMode(handle, desired):
            raise OSError("SetConsoleMode failed")
        current = wintypes.DWORD()
        if not _kernel32.GetConsoleMode(handle, ctypes.byref(current)):
            raise OSError("GetConsoleMode verification failed")
        disabled = not bool(current.value & enable_quick_edit_mode)
        log_event("INFO" if disabled else "WARN", "CONSOLE",
                  f"QuickEdit: {'DISABLED' if disabled else 'STILL ENABLED'} "
                  f"(original=0x{original.value:04x}, current=0x{current.value:04x})")
        return disabled
    except Exception as exc:
        log_event("WARN", "CONSOLE", f"QuickEdit mode not changed: {exc}")
        return False


_instance_handle = None


def acquire_single_instance():
    """Prevent two A-end processes from racing the clipboard and capture loop."""
    global _instance_handle
    try:
        _kernel32.CreateMutexW.restype = ctypes.c_void_p
        _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        _kernel32.GetLastError.restype = wintypes.DWORD
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        name = "Global\\QRTunnel-A-End"
        _instance_handle = _kernel32.CreateMutexW(None, False, name)
        if not _instance_handle:
            log_event("WARN", "START", "single-instance mutex unavailable; continuing")
            return True
        if _kernel32.GetLastError() == 183:
            log_event("ERROR", "START", "another A-end instance is already running; exiting")
            _kernel32.CloseHandle(_instance_handle)
            _instance_handle = None
            return False
        log_event("INFO", "START", "single-instance mutex acquired")
        return True
    except Exception as exc:
        log_event("WARN", "START", f"single-instance check failed: {exc}; continuing")
        return True


def release_single_instance():
    global _instance_handle
    if _instance_handle:
        try:
            _kernel32.CloseHandle(_instance_handle)
        except Exception:
            pass
        _instance_handle = None


# ---- Range encode helper for MISSING signal ----

def encode_ranges(indices):
    """Encode a sorted list of indices as comma-separated ranges.
    e.g. [1,2,3,5,6,7,10] -> '1-3,5-7,10'"""
    if not indices:
        return ""
    parts = []
    start = indices[0]
    end = indices[0]
    for i in indices[1:]:
        if i == end + 1:
            end = i
        else:
            if start == end:
                parts.append(str(start))
            else:
                parts.append(f"{start}-{end}")
            start = i
            end = i
    if start == end:
        parts.append(str(start))
    else:
        parts.append(f"{start}-{end}")
    return ",".join(parts)


# ---- Win32 clipboard helpers (ctypes) ----

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

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
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.restype = ctypes.c_void_p
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.SetFocus.restype = wintypes.HWND
_user32.SetFocus.argtypes = [wintypes.HWND]
_user32.keybd_event.restype = None
_user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.DWORD]
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetCurrentThreadId.argtypes = []


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


def set_clipboard_text(text):
    """Write Unicode text without losing the previous clipboard on allocation failure."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise RuntimeError("GlobalAlloc failed")
    transferred = False
    try:
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            raise RuntimeError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            _kernel32.GlobalUnlock(handle)
        with _clipboard_lock:
            last_error = None
            for attempt in range(3):
                try:
                    _open_clipboard()
                    try:
                        if not _user32.EmptyClipboard():
                            raise OSError("EmptyClipboard failed")
                        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                            raise OSError("SetClipboardData failed")
                        transferred = True  # clipboard now owns the allocation
                        break
                    finally:
                        _user32.CloseClipboard()
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.02 * (attempt + 1))
            if not transferred:
                raise last_error or OSError("clipboard write failed")
    finally:
        if not transferred:
            _kernel32.GlobalFree(handle)


def try_set_clipboard_text(text, label="clipboard", attempts=3):
    """Best-effort control-plane clipboard write; never raises to HTTP handler."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            set_clipboard_text(text)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.05 * attempt)
    log_event("ERROR", "CLIP", f"{label} write failed after {attempts} attempts: {last_error}")
    return False


# hwnd of the cloud desktop / RDP window, set at startup for focus-stealing
TARGET_HWND = None
_last_focus_warning = 0.0
_scan_keywords = None
_last_rescan = 0.0
_last_focus_fail_log = 0.0
_last_alt_sent = 0.0  # throttle the synthetic Alt key (it can trigger a system sound)

VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

# Non-empty "idle" marker written to the clipboard when there is nothing in
# flight. RDP clipboard redirection (HSRClient) can silently stall the channel
# after an EMPTY clipboard write — the remote never sees subsequent updates
# until the channel re-syncs. Writing a tiny non-empty sentinel instead keeps
# the channel alive; B-end treats "QRT:IDLE" as a baseline, not a request.
IDLE_MARKER = "QRT:IDLE"


def _rescan_target_window():
    """Re-discover the RDP window (e.g. HSRClient switched to fullscreen and
    may use a different top-level window handle)."""
    global TARGET_HWND, _last_rescan
    now = time.time()
    if now - _last_rescan < 3:
        return
    _last_rescan = now
    try:
        found = find_target_window(_scan_keywords, verbose=False)
    except Exception:
        found = None
    if found:
        hwnd, _ = found
        if hwnd != TARGET_HWND:
            TARGET_HWND = hwnd
            log_event("INFO", "FOCUS", f"rescanned RDP window, new hwnd={TARGET_HWND:#x}")


def start_window_monitor(interval=2.0):
    """Background thread that periodically re-scans for the RDP window.

    Fixes the "A-end started before HSRClient" case: A-end may boot with no
    HSRClient window yet, so TARGET_HWND stays None (or a wrong fallback). Once
    HSRClient appears (later), find_target_window prefers it and this thread
    updates TARGET_HWND automatically — no A-end restart needed. Also re-pins a
    window that switched fullscreen/minimized top-level handles.
    """
    def _loop():
        while True:
            time.sleep(interval)
            try:
                _rescan_target_window()
            except Exception:
                pass
    t = threading.Thread(target=_loop, daemon=True, name="qrtunnel-window-monitor")
    t.start()
    return t


def bring_rdp_to_foreground():
    """Force the cloud desktop window to the foreground using several layers.

    RDP (HSRClient) only synchronizes the clipboard while it owns the real
    foreground focus. Plain SetForegroundWindow from a background process is
    refused in many cases by Windows (especially for exclusive/fullscreen RDP
    windows), so we escalate: input-queue attach -> BringWindowToTop -> SetFocus
    -> SetForegroundWindow -> Alt-key unlock, and re-scan the window if the
    handle is stale.
    """
    global TARGET_HWND, _last_focus_warning, _last_focus_fail_log, _last_alt_sent
    if not TARGET_HWND or not _user32.IsWindow(TARGET_HWND):
        _rescan_target_window()
    if not TARGET_HWND:
        if time.time() - _last_focus_warning > 30:
            _last_focus_warning = time.time()
            log_event("WARN", "FOCUS", "RDP window handle not found; clipboard sync may fail")
        return
    try:
        fg = _user32.GetForegroundWindow()
        if fg == TARGET_HWND:
            return
        _user32.ShowWindow(TARGET_HWND, 9)  # SW_RESTORE in case minimized
        # A minimized window restores asynchronously (with an animation). If we
        # immediately run the foreground escalation below, the focus calls land
        # on a window that is still hidden/restoring and Windows refuses them.
        # Give a minimized window a beat to finish restoring, then re-assert the
        # restore before escalating. Non-minimized windows skip this entirely.
        if _user32.IsIconic(TARGET_HWND):
            time.sleep(0.2)
            _user32.ShowWindow(TARGET_HWND, 9)

        # ---- Layer 1: attach the input queue of the current foreground thread ----
        fg_tid = wintypes.DWORD()
        tgt_tid = wintypes.DWORD()
        if fg:
            _user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_tid))
        _user32.GetWindowThreadProcessId(TARGET_HWND, ctypes.byref(tgt_tid))
        attached = False
        if fg and fg_tid.value and tgt_tid.value:
            attached = bool(_user32.AttachThreadInput(fg_tid.value, tgt_tid.value, True))
        try:
            _user32.BringWindowToTop(TARGET_HWND)
            _user32.SetForegroundWindow(TARGET_HWND)
            _user32.SetFocus(TARGET_HWND)
        finally:
            if attached:
                _user32.AttachThreadInput(fg_tid.value, tgt_tid.value, False)

        # ---- Layer 2: Alt-key unlock, then retry foreground ----
        # Throttled to 1 per 2s: the synthetic Alt is only a focus unlock and
        # the silent Layer-1 calls run every tick anyway; sending it on every
        # ~500ms MISSING write injects a keystroke into the RDP session that
        # HSRClient may turn into a system/keyboard sound each time.
        if _user32.GetForegroundWindow() != TARGET_HWND:
            now = time.time()
            if now - _last_alt_sent >= 2.0:
                _last_alt_sent = now
                _user32.keybd_event(VK_MENU, 0, 0, None)          # press Alt
                _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)  # release Alt
            _user32.BringWindowToTop(TARGET_HWND)
            _user32.SetForegroundWindow(TARGET_HWND)

        if _user32.GetForegroundWindow() != TARGET_HWND:
            _rescan_target_window()
            if TARGET_HWND:
                _user32.BringWindowToTop(TARGET_HWND)
                _user32.SetForegroundWindow(TARGET_HWND)

        if _user32.GetForegroundWindow() != TARGET_HWND:
            now = time.time()
            if now - _last_focus_fail_log > 10:
                _last_focus_fail_log = now
                log_event("WARN", "FOCUS", "could not give RDP window foreground; clipboard sync may fail")
    except Exception as e:
        log_event("WARN", "FOCUS", f"could not focus RDP window: {e}")


# ---- Window detection (find cloud desktop / RDP window) ----

class WindowPlacementInfo(ctypes.Structure):
    """Win32 WINDOWPLACEMENT (subset) for reading a minimized window's normal size."""
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def _score_candidate(cand):
    """Score a window candidate as the RDP render surface.

    Account-agnostic heuristics (window titles like ``wangchu2-1`` change with
    the cloud-desktop account; the process identity and generic shapes do not):
      * process is HSRClient (or its receiver) — most reliable
      * title matches the client's generic ``user-<n>`` pattern
      * Qt/HwndWrapper class hints render vs. host surface
      * larger area usually means the actual desktop surface
    """
    _, title, class_name, proc_name, _, _, w, h = cand
    score = 0
    tl, cl, pl = title.lower(), class_name.lower(), proc_name.lower()
    # Process identity (account-independent).
    if "hsrclient" in pl:
        score += 80
    elif "cmss" in pl or "receiver" in pl or "wsg" in pl:
        score += 60
    # Generic title pattern "user-<number>" (e.g. wangchu2-1, zhangsan-2).
    trimmed = title.strip()
    if re.match(r"^[\w.\-@]+\-\d+$", trimmed):
        score += 55
    elif re.search(r"-\d+$", trimmed) and not re.search(r"[\\/:*?\"<>|]", trimmed):
        score += 25
    # Class hints: Qt render window vs host wrapper.
    if "qwindow" in cl or "qwidget" in cl:
        score += 35
    elif "hwndwrapper" in cl:
        score += 20
    # Branding hints (Chinese client strings).
    if "中移" in tl or "云桌面" in tl or "桌面" in tl:
        score += 15
    # Larger area usually means the live desktop surface.
    score += min(w * h / 2000000.0, 1.0) * 20
    return score


def find_target_window(keywords=None, diagnose=False, verbose=True):
    """
    Find the cloud-desktop (RDP) window regardless of the logged-in account.

    Primary match: any top-level window whose process is HSRClient, picking the
    largest (the live desktop surface, whether windowed, fullscreen, or
    minimized). Minimized windows report a 0-sized GetWindowRect, so their
    restored size from GetWindowPlacement is used instead. If no HSRClient
    process is visible, falls back to trusted keyword matching only. It never
    blindly chooses an arbitrary large window: a false positive (e.g.
    TextInputHost) is worse than returning None and re-scanning later.
    """
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowPlacement.restype = wintypes.BOOL
    _user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WindowPlacementInfo)]

    hsr_results = []
    kw_results = []
    all_candidates = []
    fallback_keywords = keywords or [
        "中移在线", "云桌面", "Remote Desktop", "mstsc", "rdp", "CmDesktop",
        "WSG", "HSR", "hsr", "remote", "desktop", "桌面",
    ]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lParam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        title_buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value
        class_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, class_buf, 256)
        class_name = class_buf.value

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = _get_process_name(pid.value)

        # Skip known system/shell windows that are never the RDP render surface.
        # TextInputHost (Windows touch keyboard / input experience) is
        # fullscreen 1920x1080 and would otherwise be chosen by the area-only
        # fallback as the "RDP window", silently breaking clipboard sync.
        proc_base = proc_name.lower().rsplit("\\", 1)[-1]
        if proc_base in (
            "textinputhost.exe", "searchapp.exe", "searchhost.exe",
            "startmenuexperiencehost.exe", "shellexperiencehost.exe",
            "applicationframehost.exe", "sihost.exe", "dwm.exe",
        ):
            return True
        if class_name in ("Shell_TrayWnd", "Progman", "WorkerW"):
            return True

        if _user32.IsIconic(hwnd):
            # Minimized: GetWindowRect is meaningless; use the restored rect.
            placement = WindowPlacementInfo()
            placement.length = ctypes.sizeof(placement)
            if _user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                norm = placement.rcNormalPosition
                left, top, right, bottom = norm.left, norm.top, norm.right, norm.bottom
            else:
                left = top = right = bottom = 0
        else:
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom

        w = right - left
        h = bottom - top
        if w < 200 or h < 150:
            return True
        cand = (hwnd, title, class_name, proc_name, left, top, w, h)
        all_candidates.append(cand)

        # Primary: HSRClient process, any large window (windowed/fullscreen/minimized).
        if "hsrclient" in proc_name.lower():
            hsr_results.append(cand)

        # Fallback: keyword match on title + class.
        match_text = (title + " " + class_name).lower()
        for kw in fallback_keywords:
            if kw.lower() in match_text:
                kw_results.append(cand)
                break

        return True

    _user32.EnumWindows(enum_proc, 0)

    if keywords:
        # Explicit user override: only trust actual keyword matches. Falling
        # back to an arbitrary large window would silently pin the wrong HWND.
        results = kw_results
        results.sort(key=lambda c: (c[6] * c[7], _score_candidate(c)), reverse=True)
    elif hsr_results:
        results = hsr_results
        results.sort(key=lambda c: (c[6] * c[7], _score_candidate(c)), reverse=True)
    elif kw_results:
        results = kw_results
        results.sort(key=lambda c: (_score_candidate(c), c[6] * c[7]), reverse=True)
    else:
        # No reliable identity signal. Return None and let the periodic monitor
        # re-scan after HSRClient appears; never choose by size/score alone.
        results = []

    if not results:
        if diagnose:
            all_candidates.sort(key=lambda c: c[6] * c[7], reverse=True)
            for entry in all_candidates[:15]:
                _, t, cls, proc, _, _, w, h = entry
                log_event("INFO", "START",
                          f"candidate window title={t!r}, class={cls!r}, proc={proc!r}, size={w}x{h}")
        return None

    best = results[0]
    hwnd, title, class_name, proc_name, left, top, w, h = best
    score = _score_candidate(best)
    if verbose:
        log_event("INFO", "START",
                  f"matched window title={title!r}, class={class_name!r}, proc={proc_name!r}, "
                  f"size={w}x{h}, score={score:.0f}")
    return hwnd, {"left": left, "top": top, "width": w, "height": h}


def _get_process_name(pid):
    """Get process executable path from PID.

    Tries normal query access first, then PROCESS_QUERY_LIMITED_INFORMATION
    with QueryFullProcessImageNameW (works even for elevated/protected
    processes when the full path read fails). Cached per PID.
    """
    if not hasattr(_get_process_name, '_cache'):
        _get_process_name._cache = {}
    if pid in _get_process_name._cache:
        return _get_process_name._cache[pid]
    name = ""
    _kernel32_proc = ctypes.windll.kernel32
    try:
        _kernel32_proc.OpenProcess.restype = ctypes.c_void_p
        _kernel32_proc.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _kernel32_proc.CloseHandle.restype = wintypes.BOOL
        _kernel32_proc.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = _kernel32_proc.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
        if handle:
            try:
                _psapi = ctypes.windll.psapi
                _psapi.GetModuleFileNameExW.restype = wintypes.DWORD
                _psapi.GetModuleFileNameExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                        wintypes.LPWSTR, wintypes.DWORD]
                buf = ctypes.create_unicode_buffer(512)
                if _psapi.GetModuleFileNameExW(handle, 0, buf, 512):
                    name = buf.value
            finally:
                _kernel32_proc.CloseHandle(handle)
    except Exception:
        name = ""
    if not name:
        try:
            _kernel32_proc.QueryFullProcessImageNameW.restype = wintypes.BOOL
            _kernel32_proc.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            handle = _kernel32_proc.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    size = wintypes.DWORD(512)
                    if _kernel32_proc.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        name = buf.value
                finally:
                    _kernel32_proc.CloseHandle(handle)
        except Exception:
            name = ""
    _get_process_name._cache[pid] = name
    return name


# ---- Screen capture ----

class ScreenCapturer:
    """Capture screen region using mss or BitBlt fallback."""

    def __init__(self, capture_region=None):
        mss_cls = getattr(mss, "MSS", None)
        self.sct = mss_cls() if mss_cls else mss.mss()
        self.capture_region = capture_region
        self._bitblt_ok = None  # None=untested, True=works, False=failed
        self._init_bitblt()

    def _init_bitblt(self):
        """Initialize BitBlt ctypes declarations."""
        try:
            self._gdi32 = ctypes.windll.gdi32
            self._user32_dc = ctypes.windll.user32
            # CreateDIBSection
            self._gdi32.CreateDIBSection.restype = ctypes.c_void_p
            self._gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
            # BitBlt
            self._gdi32.BitBlt.restype = wintypes.BOOL
            self._gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
            # SelectObject / DeleteObject / DeleteDC
            self._gdi32.SelectObject.restype = ctypes.c_void_p
            self._gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self._gdi32.DeleteObject.restype = wintypes.BOOL
            self._gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
            self._gdi32.DeleteDC.restype = wintypes.BOOL
            self._gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
            # GetDC / ReleaseDC
            self._user32_dc.GetDC.restype = ctypes.c_void_p
            self._user32_dc.GetDC.argtypes = [wintypes.HWND]
            self._user32_dc.ReleaseDC.restype = ctypes.c_int
            self._user32_dc.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
            # CreateCompatibleDC / CreateCompatibleBitmap
            self._gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
            self._gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
            self._gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
            self._gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            # GetDIBits
            self._gdi32.GetDIBits.restype = ctypes.c_int
            self._gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                                               ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        except Exception as e:
            log_event("ERROR", "CAPTURE", f"BitBlt init failed: {e}")
            self._bitblt_ok = False

    def capture_bitblt(self, region):
        """Capture using Windows BitBlt (PrintWindow for GPU-composed windows)."""
        if self._bitblt_ok is False:
            return None

        left = region["left"]
        top = region["top"]
        w = region["width"]
        h = region["height"]

        # Find the RDP window hwnd again (it may have changed)
        found = find_target_window()
        if not found:
            return None
        hwnd, _ = found

        hdc = self._user32_dc.GetDC(0)
        if not hdc:
            return None
        try:
            mem_dc = self._gdi32.CreateCompatibleDC(hdc)
            if not mem_dc:
                return None

            # BITMAPINFO for 32-bit BGRA
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB

            ppx = ctypes.c_void_p()
            hBitmap = self._gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(ppx), None, 0)
            if not hBitmap or not ppx:
                self._gdi32.DeleteDC(mem_dc)
                return None

            old = self._gdi32.SelectObject(mem_dc, hBitmap)

            # Use PrintWindow (PW_RENDERFULLCONTENT = 2) for GPU-composed windows
            PW_RENDERFULLCONTENT = 2
            result = self._user32_dc.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
            if not result:
                # Fallback: try BitBlt from screen DC
                result = self._gdi32.BitBlt(mem_dc, 0, 0, w, h, hdc, left, top, 0x00CC0020)  # SRCCOPY

            if not result:
                self._gdi32.SelectObject(mem_dc, old)
                self._gdi32.DeleteObject(hBitmap)
                self._gdi32.DeleteDC(mem_dc)
                return None

            # Read pixels from the DIB section
            buf_size = w * h * 4
            pixel_data = (ctypes.c_ubyte * buf_size).from_address(ppx.value)

            # Convert BGRA -> RGB
            arr = np.frombuffer(pixel_data, dtype=np.uint8).reshape(h, w, 4)
            rgb = arr[:, :, 2::-1].copy()  # BGRA -> RGB

            self._gdi32.SelectObject(mem_dc, old)
            self._gdi32.DeleteObject(hBitmap)
            self._gdi32.DeleteDC(mem_dc)

            self._bitblt_ok = True
            return Image.fromarray(rgb, "RGB")
        except Exception as e:
            if self._bitblt_ok is None:
                log_event("ERROR", "CAPTURE", f"BitBlt failed: {e}")
                self._bitblt_ok = False
            return None
        finally:
            self._user32_dc.ReleaseDC(0, hdc)

    def capture(self, monitor_region=None):
        """
        Capture screen. If monitor_region is given, capture that region.
        If no region, capture the full primary display.
        Returns PIL Image or None.
        """
        region = monitor_region or self.capture_region

        # Try mss first
        try:
            if region:
                monitor = {
                    "left": region["left"],
                    "top": region["top"],
                    "width": region["width"],
                    "height": region["height"],
                }
            else:
                # Capture full primary display (monitor 1)
                monitor = self.sct.monitors[1] if len(self.sct.monitors) > 1 else self.sct.monitors[0]
            shot = self.sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            # Check if image is all black (GPU-composed window returns black)
            arr = np.array(img)
            if arr.max() == 0:
                # All black - try BitBlt
                if region:
                    img2 = self.capture_bitblt(region)
                else:
                    # Full screen BitBlt
                    img2 = self.capture_bitblt({"left": 0, "top": 0,
                                                "width": shot.size[0], "height": shot.size[1]})
                if img2 is not None:
                    return img2
                # BitBlt also failed, return the black image for debugging
                return img
            return img
        except Exception as e:
            log_event("WARN", "CAPTURE", f"mss failed, trying BitBlt: {e}")

        # Fallback to BitBlt
        img = self.capture_bitblt(region)
        if img is not None:
            return img
        return None


# ---- QR decoding ----

_last_decode_warn = 0.0


def decode_qrs(img):
    """Decode all QR codes from a PIL image. Returns list of (data, rect)."""
    global _last_decode_warn
    results = []
    try:
        arr = np.array(img.convert("RGB"))
        barcodes = zxingcpp.read_barcodes(arr, formats=[zxingcpp.BarcodeFormat.QRCode])
        for b in barcodes:
            rect = b.position
            raw = bytes(b.bytes)
            # The raw binary payload is authoritative; b.text is only a debug
            # comparison and always differs for binary data pages. Rate-limit
            # the diagnostic so it does not spam every 30ms capture.
            text_data = b.text.encode("utf-8", errors="surrogateescape")
            if raw != text_data and time.time() - _last_decode_warn > 10:
                _last_decode_warn = time.time()
                log_event("WARN", "DECODE", f"raw/text byte mismatch: raw={len(raw)}, text={len(text_data)}")
            results.append((raw, rect))
    except Exception as e:
        log_event("ERROR", "DECODE", f"zxing-cpp failed: {e}")
        import traceback
        traceback.print_exc()
    return results


def parse_data_page(data):
    """Parse [0x01][seq:4B BE][id_hex:32B][chunk], rejecting malformed IDs."""
    if len(data) < 37 or data[0:1] != b"\x01":
        return None
    try:
        seq = struct.unpack(">I", data[1:5])[0]
        id_hex = data[5:37].decode("ascii")
    except (struct.error, UnicodeDecodeError):
        return None
    if len(id_hex) != 32 or any(c not in "0123456789abcdefABCDEF" for c in id_hex):
        return None
    return (seq, id_hex.lower(), data[37:])


def parse_meta_page(data):
    """Parse JSON meta page. Returns dict or None."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or not obj.get("meta"):
        return None
    if not isinstance(obj.get("id"), str) or not obj.get("id"):
        return None
    if not isinstance(obj.get("chunks"), int):
        return None
    if not isinstance(obj.get("status"), int):
        return None
    if "bulk" in obj and not isinstance(obj.get("bulk"), bool):
        return None
    return obj


# ---- Request tracking ----

class RequestTracker:
    """Thread-safe pending-request store shared by HTTP and capture threads."""

    def __init__(self, timeout=300):
        self.pending = {}
        self.timeout = timeout
        self._done = set()
        self._cancelled = set()
        self._acked = set()
        self._stopped = set()
        self._lock = threading.RLock()

    def mark_acked(self, req_id):
        with self._lock:
            if req_id in self._acked or req_id not in self.pending:
                return False
            self._acked.add(req_id)
            return True

    def has_ack(self, req_id):
        with self._lock:
            if req_id in self._acked or req_id in self._done:
                return True
            p = self.pending.get(req_id)
            return bool(p and (p["meta"] is not None or p["chunks"]))

    def mark_stopped(self, req_id):
        with self._lock:
            if req_id in self._done:
                self._stopped.add(req_id)

    def has_stopped(self, req_id):
        with self._lock:
            return req_id in self._stopped

    def is_pending(self, req_id):
        with self._lock:
            return req_id in self.pending

    def is_done(self, req_id):
        with self._lock:
            return req_id in self._done

    def add(self, req_id):
        with self._lock:
            self.pending[req_id] = {
                "meta": None, "chunks": {}, "expire": time.time() + self.timeout
            }

    def add_meta(self, req_id, meta):
        with self._lock:
            if req_id in self._done or req_id in self._cancelled:
                return False
            if req_id in self.pending:
                is_new = self.pending[req_id]["meta"] is None
                self.pending[req_id]["meta"] = meta
                expected = meta.get("chunks", 0)
                self.pending[req_id]["chunks"] = {
                    seq: chunk for seq, chunk in self.pending[req_id]["chunks"].items()
                    if 1 <= seq <= expected
                }
                return is_new
            return False

    def add_chunk(self, id_hex, seq, chunk):
        req_id = f"{id_hex[:8]}-{id_hex[8:12]}-{id_hex[12:16]}-{id_hex[16:20]}-{id_hex[20:]}"
        with self._lock:
            if req_id in self._done or req_id in self._cancelled:
                return False
            p = self.pending.get(req_id)
            if not p:
                return False
            meta = p["meta"]
            if meta is not None:
                expected = meta.get("chunks", 0)
                if seq < 1 or seq > expected:
                    return False
            is_new = seq not in p["chunks"]
            p["chunks"][seq] = chunk
            return is_new

    def progress_count(self, req_id):
        with self._lock:
            p = self.pending.get(req_id)
            return 0 if not p else (1 if p["meta"] else 0) + len(p["chunks"])

    def pop_complete_response(self, req_id):
        """Atomically validate and remove a complete response, then decode it."""
        with self._lock:
            p = self.pending.get(req_id)
            if not p or not p["meta"]:
                return None
            meta = dict(p["meta"])
            expected = meta.get("chunks", 0)
            expected_keys = set(range(1, expected + 1))
            if set(p["chunks"].keys()) != expected_keys:
                return None
            chunks = [p["chunks"][i] for i in range(1, expected + 1)]
            self.pending.pop(req_id, None)

        body = b"".join(chunks)
        if meta.get("gzip"):
            try:
                body = gzip.decompress(body)
            except Exception as exc:
                raise ValueError(f"gzip response decode failed: {exc}") from exc
        raw_len = meta.get("raw_len")
        if raw_len is not None and len(body) != raw_len:
            raise ValueError(f"response length mismatch: got {len(body)}, expected {raw_len}")
        return meta.get("status", 200), meta.get("headers", []), body, meta

    def mark_done(self, req_id):
        with self._lock:
            self._done.add(req_id)
            self.pending.pop(req_id, None)

    def cancel(self, req_id):
        with self._lock:
            self._cancelled.add(req_id)
            self.pending.pop(req_id, None)

    def get_missing_indices(self, req_id):
        with self._lock:
            p = self.pending.get(req_id)
            if not p:
                return None
            if p["meta"] is None:
                return [0]
            expected = p["meta"].get("chunks", 0)
            return [i for i in range(1, expected + 1) if i not in p["chunks"]]

    def get_collection_status(self, req_id):
        with self._lock:
            p = self.pending.get(req_id)
            if not p:
                if req_id in self._done:
                    return "DONE (already processed)"
                if req_id in self._cancelled:
                    return "CANCELLED"
                return "NOT_FOUND"
            if p["meta"] is not None:
                expected = p["meta"].get("chunks", 0)
                received = len(p["chunks"])
                missing = set(range(1, expected + 1)) - set(p["chunks"].keys())
                return f"meta=YES, chunks={received}/{expected}, missing={sorted(missing) if missing else 'none'}"
            return f"meta=NO, chunks={len(p['chunks'])}"

    def cleanup(self):
        with self._lock:
            now = time.time()
            for rid in list(self.pending.keys()):
                if self.pending[rid]["expire"] < now:
                    del self.pending[rid]
            for values in (self._cancelled, self._acked, self._stopped):
                if len(values) > 100:
                    values.clear()


# ---- HTTP Handler ----

tracker = RequestTracker()
# Serialize requests: only one request in flight at a time,
# because clipboard and QR display are single-channel
_request_lock = threading.Lock()


# ---- Startup probe / capability negotiation ----

def parse_probe_response(status, headers, body):
    """Parse a probe response body into a capability dict, or None.

    The B-end answers ``/__qrtunnel/probe`` with
    ``{"probe": true, "role": "B", "version": ..., "protocol": ...,
      "features": [...], ...}``. A legacy B-end forwards the probe path to the
    git server, whose non-JSON/404 answer must be treated as "no probe support"
    rather than a hard error.
    """
    if status != 200:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("probe") is not True:
        return None
    if not isinstance(obj.get("version"), str) or not obj.get("version"):
        return None
    if not isinstance(obj.get("protocol"), str) or not obj.get("protocol"):
        return None
    features = obj.get("features")
    if features is not None and not isinstance(features, list):
        return None
    return obj


def _probe_match_ok(cap):
    """Check the probed peer is compatible with this A-end build.

    Protocol must match exactly. Version must match exactly too (a dev build
    expects the same dev build on the peer during rollout); a version skew is
    reported as a warning, not fatal, so probes keep working across builds.
    """
    protocol_ok = cap.get("protocol") == PROTOCOL_VERSION
    version_ok = cap.get("version") == VERSION
    if protocol_ok and version_ok:
        return True, "ok"
    if not protocol_ok:
        return False, f"protocol mismatch: B={cap.get('protocol')}, A={PROTOCOL_VERSION}"
    return False, f"version mismatch: B={cap.get('version')}, A={VERSION}"


def wait_for_target_window(timeout=None, poll_interval=0.5, log_interval=30.0):
    """Wait until a trusted HSRClient window has been discovered.

    The proxy may start before HSRClient. In that case an immediate startup
    probe can never cross the RDP clipboard and would incorrectly mark a new B
    peer as legacy. This daemon-side wait does not block the HTTP server: it
    periodically asks the window monitor to re-scan, then lets the probe run
    only after TARGET_HWND points to a live, trusted match. ``timeout=None``
    waits indefinitely; ``--no-probe`` remains the explicit opt-out.
    """
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    next_log = 0.0
    while True:
        hwnd = TARGET_HWND
        if hwnd and _user32.IsWindow(hwnd):
            return True
        _rescan_target_window()
        hwnd = TARGET_HWND
        if hwnd and _user32.IsWindow(hwnd):
            return True
        now = time.monotonic()
        if now >= next_log:
            log_event("INFO", "PROBE", "waiting for trusted HSRClient window before startup probe")
            next_log = now + max(1.0, log_interval)
        if deadline is not None and now >= deadline:
            return False
        time.sleep(max(0.05, poll_interval))


def run_probe(ack_wait=3.0, max_attempts=4, wait_timeout=30.0):
    """Send one internal probe request through the QR channel and record the
    B-end capability. Returns the parsed capability dict (or a legacy marker).

    The probe reuses the exact same clipboard-send + ACK + QR-capture flow as a
    normal request, but never touches an IDEA client and never writes to the
    transfer history (it is not a git transfer).
    """
    global _peer_capability
    req_id = str(uuid.uuid4())
    req = {
        "id": req_id,
        "method": "GET",
        "path": PROBE_PATH,
        "headers": [],
        "body": None,
        "protocol": PROTOCOL_VERSION,
        "client_version": VERSION,
        "features": FEATURES,
        "probe": True,
    }
    log_event("INFO", "PROBE", f"starting startup probe (id={short_id(req_id)})", req_id)

    # Serialize against normal requests so the probe never collides with a git
    # request on the single clipboard/QR channel.
    with _request_lock:
        tracker.add(req_id)
        _write_summary({"status": "probe", "request_id": req_id, "method": "GET", "path": PROBE_PATH})
        acked = False
        for attempt in range(1, max_attempts + 1):
            req["retry"] = attempt
            req_json = json.dumps(req, ensure_ascii=False)
            clipboard_text = f"QRT:b64:{base64.b64encode(req_json.encode('utf-8')).decode('ascii')}"
            bring_rdp_to_foreground()
            time.sleep(0.3)
            if not try_set_clipboard_text(clipboard_text, "probe request"):
                log_event("WARN", "PROBE", f"probe clipboard write failed; retrying", req_id)
                continue
            bring_rdp_to_foreground()
            time.sleep(0.15)
            try_set_clipboard_text(clipboard_text, "probe request")
            bring_rdp_to_foreground()
            deadline = time.time() + ack_wait
            while time.time() < deadline:
                if tracker.has_ack(req_id):
                    acked = True
                    break
                time.sleep(0.1)
            if acked:
                log_event("INFO", "PROBE", f"B-end acknowledged probe on attempt {attempt}", req_id)
                break
            log_event("WARN", "PROBE", f"no ACK after {ack_wait:.1f}s; retrying probe", req_id)

        if not acked:
            tracker.cancel(req_id)
            try_set_clipboard_text(IDLE_MARKER, "idle marker")
            _write_summary({"status": "probe_failed", "request_id": req_id,
                            "failure_reason": "clipboard_ack_timeout"})
            _peer_capability = {"probe": False, "legacy": True,
                                "reason": "clipboard_ack_timeout"}
            log_event("ERROR", "PROBE", "probe did not reach B-end; assuming legacy peer", req_id)
            return _peer_capability

        # Wait for the probe response QR. Rolling deadline like _wait_response.
        deadline = time.time() + wait_timeout
        last_progress = -1
        result = None
        while time.time() < deadline:
            resp = tracker.pop_complete_response(req_id)
            if resp is not None:
                result = resp
                break
            time.sleep(0.05)
            progress = tracker.progress_count(req_id)
            if progress > last_progress:
                last_progress = progress
                deadline = time.time() + wait_timeout
        tracker.mark_done(req_id)
        try_set_clipboard_text(IDLE_MARKER, "idle marker")

        if result is None:
            _write_summary({"status": "probe_failed", "request_id": req_id,
                            "failure_reason": "probe_timeout"})
            _peer_capability = {"probe": False, "legacy": True, "reason": "probe_timeout"}
            log_event("ERROR", "PROBE", "probe response timed out; assuming legacy peer", req_id)
            return _peer_capability

        status, headers, body, _meta = result
        cap = parse_probe_response(status, headers, body)
        if cap is None:
            _write_summary({"status": "probe_failed", "request_id": req_id,
                            "failure_reason": "legacy_peer", "http_status": status})
            _peer_capability = {"probe": False, "legacy": True, "reason": "legacy_peer",
                                "http_status": status}
            log_event("WARN", "PROBE",
                      f"B-end did not answer the probe (HTTP {status}); assuming legacy peer", req_id)
            return _peer_capability

        ok, why = _probe_match_ok(cap)
        cap["ok"] = ok
        cap["compat_reason"] = why
        features = cap.get("features") or []
        log_event("INFO", "PROBE",
                  f"B-end peer: version={cap.get('version')}, protocol={cap.get('protocol')}, "
                  f"features={features}, compat={'OK' if ok else why}", req_id)
        if not ok:
            log_event("WARN", "PROBE", f"peer not fully compatible: {why}", req_id)
        _write_summary({"status": "probe_ok" if ok else "probe_warn",
                        "request_id": req_id,
                        "peer_version": cap.get("version"),
                        "peer_protocol": cap.get("protocol"),
                        "peer_features": features,
                        "compat_reason": why if not ok else None})
        _peer_capability = cap
        return cap


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "QRTunnel-Proxy/1.0"

    def log_message(self, format, *args):
        log_event("INFO", "HTTP", format % args)

    def _send_response(self, status, headers, body, head_only=False):
        # Headers to skip: we control length/encoding ourselves
        skip = {"transfer-encoding", "content-length", "connection", "keep-alive",
                "content-encoding"}
        # For HEAD, preserve the upstream representation length when present so
        # the client sees the same Content-Length a GET would return.
        upstream_length = None
        for k, v in headers:
            if k.lower() == "content-length":
                try:
                    upstream_length = int(v)
                except (TypeError, ValueError):
                    upstream_length = None
        self.send_response(status)
        for k, v in headers:
            if k.lower() in skip:
                continue
            self.send_header(k, v)
        content_length = upstream_length if (head_only and upstream_length is not None) else len(body)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _build_request_json(self):
        req_id = str(uuid.uuid4())
        headers = []
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                headers.append([k, v])

        c_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(c_len) if c_len > 0 else None
        body_b64 = base64.b64encode(body).decode("ascii") if body else None

        req = {
            "id": req_id,
            "method": self.command,
            "path": self.path,
            "headers": headers,
            "body": body_b64,
            "protocol": PROTOCOL_VERSION,
            "client_version": VERSION,
            "features": FEATURES,
        }
        if body:
            log_event("INFO", "HTTP", f"request body {len(body)}B raw / {len(body_b64)}B base64", req_id)
        return req_id, req

    def _wait_response(self, req_id, timeout=120):
        # Rolling deadline: every newly collected page (meta or chunk) pushes the
        # deadline out again, so a large multi-page response never times out while
        # it is still making progress. We only give up after `timeout` seconds
        # with nothing new arriving.
        deadline = time.time() + timeout
        last_progress = -1
        poll_count = 0
        last_status_print = 0
        last_missing_signal = 0.0
        while time.time() < deadline:
            try:
                response = tracker.pop_complete_response(req_id)
            except ValueError as exc:
                log_event("ERROR", "RECV", f"corrupt QR response: {exc}", req_id)
                return "CORRUPT_RESPONSE"
            if response is not None:
                return response
            time.sleep(0.05)
            poll_count += 1
            progress = tracker.progress_count(req_id)
            if progress > last_progress:
                last_progress = progress
                deadline = time.time() + timeout
            # Check client connection every ~500ms (every 10 polls) for faster cancel detection
            if poll_count % 10 == 0 and self._client_disconnected():
                log_event("WARN", "CANCEL", "IDEA client disconnected; stopping B-end")
                return "CLIENT_DISCONNECTED"
            # Write MISSING signal every ~500ms so B-end can skip received pages
            now = time.time()
            if now - last_missing_signal > 0.5:
                last_missing_signal = now
                self._write_missing_signal(req_id)
            # Print collection status every 5s to help diagnose missing chunks
            now = time.time()
            if now - last_status_print > 5.0:
                last_status_print = now
                status = tracker.get_collection_status(req_id)
                if status:
                    log_event("INFO", "RECV", f"collection: {status}", req_id)
        # Timeout - print final status before giving up
        status = tracker.get_collection_status(req_id)
        log_event("ERROR", "TIMEOUT", f"no new QR page for {timeout}s; final={status}", req_id)
        return None

    def _client_disconnected(self):
        """Check if the HTTP client (IDEA) has closed the TCP connection.

        Uses select() to check both readable (EOF/data) and exception (reset)
        states. This catches both graceful close (FIN → readable, recv returns
        0) and hard reset (RST → exception set or ConnectionResetError).
        """
        try:
            import select
            import socket as _socket
            sock = self.connection
            if sock is None:
                return True
            # Check readable, writable, and exception sets
            r, _, e = select.select([sock], [], [sock], 0)
            if e:
                # Socket in error state — connection reset
                return True
            if r:
                try:
                    buf = sock.recv(1, _socket.MSG_PEEK)
                    if len(buf) == 0:
                        return True  # EOF = client disconnected gracefully
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    return True  # Connection reset / aborted
            return False
        except Exception:
            return False

    def _write_missing_signal(self, req_id):
        """Tell B-end which pages are still missing, so it can skip already-
        received pages in subsequent loops. This is the selective retransmission
        mechanism: A-end writes QRT:MISSING:<req_id>:<indices> to clipboard,
        and B-end reads it between loops to only play missing pages.

        Page indices are 0-based in the payloads array (0 = meta page,
        1 = first data page, etc.). Uses range encoding for compactness.
        """
        missing = tracker.get_missing_indices(req_id)
        if missing is None:
            return
        # If meta has not arrived, get_missing_indices returns [0]. Once it has,
        # the list contains only missing data page numbers 1..N.

        if missing:
            missing_str = encode_ranges(missing)
            try:
                # Focus the RDP window before writing so the clipboard update is
                # observed by the foreground client (same ordering as the request
                # send path).
                bring_rdp_to_foreground()
                if not try_set_clipboard_text(f"QRT:MISSING:{req_id}:{missing_str}", "MISSING"):
                    log_event("WARN", "CLIP", f"MISSING write failed; will retry next tick", req_id)
            except Exception as exc:
                # A transient clipboard lock must not abort the HTTP request.
                log_event("WARN", "CLIP", f"MISSING write will retry next tick: {exc}", req_id)
        # If no missing pages, the main flow will send QRT:DONE

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionAbortedError:
            pass
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__qrtunnel/health":
            self._send_health()
            return
        self._handle_request()

    def do_HEAD(self):
        if self.path.split("?", 1)[0] == "/__qrtunnel/health":
            self._send_health()
            return
        self._handle_request(head_only=True)

    def _send_health(self):
        payload = {
            "version": VERSION,
            "protocol": PROTOCOL_VERSION,
            "role": "A",
            "features": FEATURES,
            "summary": _summary_snapshot(),
            "target_window": bool(TARGET_HWND),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self):
        self._handle_request()

    def do_PUT(self):
        self._handle_request()

    def do_DELETE(self):
        self._handle_request()

    def _handle_request(self, head_only=False):
        # Serialize: only one request at a time (clipboard + QR is single-channel)
        with _request_lock:
            req_id, req = self._build_request_json()
            tracker.add(req_id)
            request_started = time.time()
            _write_summary({
                "status": "in_progress",
                "request_id": req_id,
                "method": req["method"],
                "path": req["path"],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })

            # Send with retry: RDP clipboard sync (one-way A->B) silently drops
            # writes sometimes, especially on back-to-back requests. B-end shows
            # an ACK QR on receipt; if we don't see it on screen within ack_wait,
            # rewrite the clipboard. The "retry" field changes the content each
            # attempt so RDP treats it as a fresh clipboard update; B-end dedupes
            # by req_id so repeated deliveries are harmless.
            max_attempts = 8
            ack_wait = 3.0
            acked = False
            for attempt in range(1, max_attempts + 1):
                req["retry"] = attempt
                req_json = json.dumps(req, ensure_ascii=False)
                clipboard_text = f"QRT:b64:{base64.b64encode(req_json.encode('utf-8')).decode('ascii')}"
                # HSRClient must be ACTIVE before the clipboard changes: RDP only
                # pushes clipboard updates it observes as the foreground client.
                # A programmatic restore/activation is asynchronous (a few hundred
                # ms), so: focus first, settle, write, then write once more while
                # the window is really active to guarantee an event lands.
                bring_rdp_to_foreground()
                time.sleep(0.3)
                if not try_set_clipboard_text(clipboard_text, "request"):
                    log_event("WARN", "SEND", "clipboard write failed; retrying", req_id)
                    continue
                bring_rdp_to_foreground()
                time.sleep(0.15)
                second_write_ok = try_set_clipboard_text(clipboard_text, "request")
                bring_rdp_to_foreground()
                try:
                    local_clip_ok = get_clipboard_text() == clipboard_text
                except Exception:
                    local_clip_ok = False
                foreground_ok = bool(
                    TARGET_HWND and _user32.GetForegroundWindow() == TARGET_HWND
                )
                log_event(
                    "INFO", "SEND",
                    f"{req['method']} {req['path']} attempt {attempt}/{max_attempts}; "
                    f"local_clip={'OK' if local_clip_ok else 'MISMATCH'}, "
                    f"foreground={'YES' if foreground_ok else 'NO'}, "
                    f"second_write={'OK' if second_write_ok else 'FAIL'}",
                    req_id,
                )
                deadline = time.time() + ack_wait
                while time.time() < deadline:
                    if tracker.has_ack(req_id):
                        acked = True
                        break
                    time.sleep(0.1)
                if acked:
                    log_event("INFO", "ACK", f"B-end acknowledged on attempt {attempt}", req_id)
                    break
                log_event("WARN", "ACK", f"no ACK after {ack_wait:.1f}s; rewriting request (attempt {attempt})", req_id)

            if not acked:
                log_event("ERROR", "SEND", f"B-end did not acknowledge after {max_attempts} attempts", req_id)
                tracker.mark_done(req_id)
                try_set_clipboard_text(IDLE_MARKER, "idle marker")
                _write_summary({"status": "failed", "request_id": req_id, "failure_reason": "clipboard_ack_timeout", "elapsed_seconds": round(time.time() - request_started, 3)})
                self._send_response(502, [], b"QR Tunnel: B-end did not acknowledge request "
                                             b"(clipboard sync failed after retries)", head_only=head_only)
                return

            # Wait for response via QR screen capture
            resp = self._wait_response(req_id)

            if resp == "CLIENT_DISCONNECTED":
                # Client disconnected — tell B-end to stop, then clean up
                log_event("INFO", "CANCEL", "cancel signal sent; waiting for B-end to stop", req_id)
                try_set_clipboard_text(f"QRT:CANCEL:{req_id}", "CANCEL")
                bring_rdp_to_foreground()
                time.sleep(1.5)  # Wait for RDP to sync cancel signal
                tracker.cancel(req_id)
                time.sleep(0.5)  # Wait for B-end to see cancel and stop QR
                try_set_clipboard_text(IDLE_MARKER, "idle marker")
                _write_summary({"status": "cancelled", "request_id": req_id, "failure_reason": "client_disconnected", "elapsed_seconds": round(time.time() - request_started, 3)})
                return

            if resp == "CORRUPT_RESPONSE":
                try_set_clipboard_text(f"QRT:CANCEL:{req_id}", "CANCEL")
                bring_rdp_to_foreground()
                tracker.mark_done(req_id)
                log_event("ERROR", "SEND", "returning HTTP 502: corrupt QR response", req_id)
                _write_summary({"status": "failed", "request_id": req_id, "failure_reason": "corrupt_response", "elapsed_seconds": round(time.time() - request_started, 3)})
                self._send_response(502, [], b"QR Tunnel: corrupt response data", head_only=head_only)
                return

            if resp is None:
                # Timeout — tell B-end to stop playing before giving up. Without
                # the CANCEL signal B-end would keep playing all its loops
                # (page_ms*loops*pages seconds) unaware the A-end gave up.
                log_event("INFO", "CANCEL", "cancel signal sent; waiting for B-end to stop", req_id)
                try_set_clipboard_text(f"QRT:CANCEL:{req_id}", "CANCEL")
                bring_rdp_to_foreground()
                time.sleep(1.5)  # Wait for RDP to sync cancel signal
                tracker.mark_done(req_id)
                time.sleep(0.5)  # Wait for B-end to see cancel and stop QR
                try_set_clipboard_text(IDLE_MARKER, "idle marker")
                _write_summary({"status": "failed", "request_id": req_id, "failure_reason": "response_timeout", "elapsed_seconds": round(time.time() - request_started, 3)})
                self._send_response(504, [], b"QR Tunnel: response timeout", head_only=head_only)
                log_event("ERROR", "TIMEOUT", "returning HTTP 504: response timed out", req_id)
                return

            status, headers, body, resp_meta = resp
            bulk_flag = bool(resp_meta.get("bulk"))
            # Tell B-end we have the full response so it can stop playing early.
            # Keep clipboard churn minimal: one DONE is enough because B-end also
            # stops when MISSING disappears and aborts old playback for a newer
            # request. The clipboard is never emptied afterward; it transitions
            # to the non-empty QRT:IDLE marker after STOPPED/fallback.
            try_set_clipboard_text(f"QRT:DONE:{req_id}", "DONE")
            bring_rdp_to_foreground()
            if bulk_flag:
                log_event("INFO", "BULK", f"response received via BULK path: HTTP {status}, body={len(body)}B", req_id)
            log_event("INFO", "DONE", f"response complete: HTTP {status}, body={len(body)}B; DONE sent", req_id)
            try:
                self._send_response(status, headers, body, head_only=head_only)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                log_event("WARN", "CANCEL", "client disconnected while sending response", req_id)
                tracker.mark_done(req_id)
                try_set_clipboard_text(IDLE_MARKER, "idle marker")
                _write_summary({"status": "failed", "request_id": req_id, "failure_reason": "client_disconnected_on_send", "elapsed_seconds": round(time.time() - request_started, 3)})
                return

            # Mark this request as done so capture loop ignores remaining QR frames
            tracker.mark_done(req_id)

            # Prefer explicit two-phase confirmation: new B-end clears old DONE
            # before showing STOPPED as its final action. For compatibility with
            # old B-end builds (no STOPPED), retain a conservative 2.2s fallback.
            log_event("INFO", "STOP", "waiting for B-end STOPPED confirmation (fallback 2.2s)", req_id)
            stop_deadline = time.time() + 2.2
            while time.time() < stop_deadline:
                if tracker.has_stopped(req_id):
                    break
                time.sleep(0.05)
            if tracker.has_stopped(req_id):
                log_event("INFO", "STOP", "B-end STOPPED confirmed; next request may start", req_id)
            else:
                log_event("WARN", "STOP", "STOPPED not seen; using compatibility fallback", req_id)

            # Leave a non-empty idle marker so the RDP clipboard channel never
            # sees an empty clipboard (which can stall HSRClient's sync). B-end
            # treats QRT:IDLE as a baseline, not a request.
            try_set_clipboard_text(IDLE_MARKER, "idle marker")
            log_event("INFO", "DONE", "request finished; clipboard set to idle", req_id)
            summary_update = {
                "status": "completed",
                "request_id": req_id,
                "http_status": status,
                "response_bytes": len(body),
                "elapsed_seconds": round(time.time() - request_started, 3),
            }
            if bulk_flag:
                summary_update["bulk"] = True
            _write_summary(summary_update)


# ---- Screen capture loop (runs in background thread) ----

class CaptureLoop:
    """Background thread: continuously captures screen and decodes QR codes."""

    def __init__(self, capturer, capture_region=None, poll_interval=0.03):
        self.capturer = capturer
        self.capture_region = capture_region
        self.poll_interval = poll_interval
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

    def _loop(self):
        consecutive_fail = 0
        first_capture = True
        capture_count = 0
        last_qr_time = time.time()
        while self.running:
            try:
                img = self.capturer.capture(self.capture_region)
                capture_count += 1

                if first_capture:
                    first_capture = False
                    if img is not None:
                        arr = np.array(img)
                        log_event("INFO", "INIT", f"first capture OK: size={img.size}, pixel={arr.min()}..{arr.max()}")
                    else:
                        log_event("ERROR", "CAPTURE", f"first capture returned None; region={self.capture_region}")

                if img is None:
                    consecutive_fail += 1
                    if consecutive_fail % 50 == 0:
                        log_event("WARN", "CAPTURE", f"capture returned None x{consecutive_fail}; region={self.capture_region}")
                    time.sleep(0.1)
                    continue

                # Low-frequency capture heartbeat (~60s at the default cadence)
                if capture_count % 2000 == 0:
                    arr = np.array(img)
                    log_event("INFO", "CAPTURE", f"capture #{capture_count}: size={img.size}, pixel={arr.min()}..{arr.max()}")

                qrs = decode_qrs(img)
                if qrs:
                    last_qr_time = time.time()
                for data, rect in qrs:
                    self._process_qr(data)
                consecutive_fail = 0
            except Exception as e:
                log_event("ERROR", "CAPTURE", f"capture loop error: {e}")
                import traceback
                traceback.print_exc()
            time.sleep(self.poll_interval)

        # Final cleanup
        tracker.cleanup()

    def _process_qr(self, data):
        if not data:
            return

        # ACK QR from B-end: it received the clipboard request, stop re-writing
        if data.startswith(b"QRT-ACK:"):
            req_id = data[len(b"QRT-ACK:"):].decode("ascii", errors="ignore").strip()
            if req_id and tracker.mark_acked(req_id):
                log_event("INFO", "ACK", "received ACK QR", req_id)
            return

        # B-end closed the response QR window after seeing QRT:DONE. This lets
        # the request handler release its serialization lock without a fixed 2s sleep.
        if data.startswith(b"QRT-STOPPED:"):
            req_id = data[len(b"QRT-STOPPED:"):].decode("ascii", errors="ignore").strip()
            if req_id and not tracker.has_stopped(req_id):
                log_event("INFO", "STOP", "received STOPPED QR", req_id)
                tracker.mark_stopped(req_id)
            return

        # Try binary data page first
        parsed = parse_data_page(data)
        if parsed:
            seq, id_hex, chunk = parsed
            # Convert id_hex to req_id for done check
            req_id = f"{id_hex[:8]}-{id_hex[8:12]}-{id_hex[12:16]}-{id_hex[16:20]}-{id_hex[20:]}"
            if tracker.is_done(req_id):
                return  # Stale QR from already-processed request, ignore silently
            is_new = tracker.add_chunk(id_hex, seq, chunk)
            if is_new and (seq == 1 or seq % 10 == 0):
                log_event("INFO", "RECV", f"data progress: latest_seq={seq}, page_bytes={len(chunk)}", req_id)
            return

        # Try JSON meta page
        meta = parse_meta_page(data)
        if meta and meta.get("id"):
            if tracker.is_done(meta["id"]):
                return  # Stale QR from already-processed request, ignore silently
            # Ensure tracker has an entry only for the currently requested ID.
            if not tracker.is_pending(meta["id"]):
                return
            meta_protocol = meta.get("protocol")
            if meta_protocol and meta_protocol != PROTOCOL_VERSION:
                log_event("ERROR", "PROTO", f"response protocol mismatch: B={meta_protocol}, A={PROTOCOL_VERSION}", meta["id"])
                tracker.mark_done(meta["id"])
                return
            is_new = tracker.add_meta(meta["id"], meta)
            if is_new:
                log_event("INFO", "RECV", f"meta page: chunks={meta.get('chunks')} HTTP={meta.get('status')}", meta["id"])
            return

        # Neither meta nor data page - log for debugging
        first_byte = f"{data[0]:02x}" if data else "N/A"
        preview = data[:60].hex() if len(data) > 60 else data.hex()
        log_event("WARN", "RECV", f"unrecognized QR: bytes={len(data)}, first=0x{first_byte}, preview={preview[:32]}")


# ---- Main ----

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
    defaults = side_defaults(load_config(config_path), "a")

    ap = argparse.ArgumentParser(description="QR Tunnel A-end proxy (Win10 ARM)")
    ap.add_argument(
        "--config", default=str(config_path) if config_path else None,
        help="config.yaml path (default: <project root>\\config.yaml if present)")
    ap.add_argument("--listen", default=defaults.get("listen", "127.0.0.1:9999"),
                    help="Proxy listen address (default 127.0.0.1:9999)")
    ap.add_argument("--display-index", type=int,
                    default=defaults.get("display_index", -1),
                    help="Display index for capture (-1=auto, 0=primary, 1=secondary)")
    ap.add_argument("--chunk", type=int, default=defaults.get("chunk", 2800),
                    help="Chunk bytes per QR (must match B-end, default 2800)")
    ap.add_argument("--window-keywords",
                    default=defaults.get("window_keywords", ""),
                    help="Comma-separated window title keywords to capture (default: auto-detect)")
    ap.add_argument("--no-probe", action="store_true",
                    default=defaults.get("no_probe", False),
                    help="Skip the startup capability probe to the B-end (default: probe once at startup)")
    args = ap.parse_args()

    host, port = args.listen.split(":")
    port = int(port)

    # Find target window for focus-stealing (not for capture region - we capture full screen)
    keywords = None
    if args.window_keywords:
        keywords = [k.strip() for k in args.window_keywords.split(",")]
    global _scan_keywords
    _scan_keywords = keywords

    found = find_target_window(keywords, diagnose=True)
    if found:
        hwnd, bounds = found
        global TARGET_HWND
        TARGET_HWND = hwnd
        log_event("INFO", "START", f"RDP window found: {bounds['width']}x{bounds['height']} @ ({bounds['left']},{bounds['top']})")
        log_event("INFO", "START", "RDP window will be focused for clipboard synchronization")
    else:
        log_event("WARN", "START", "RDP window not found; clipboard synchronization may fail")
        log_event("INFO", "START", "copy one of the candidate window titles above and restart with:")
        log_event("INFO", "START", "--window-keywords \"<cloud-desktop title>\" (use any stable substring of the title)")

    # Initialize screen capturer (capture full screen, not window region)
    capturer = ScreenCapturer(capture_region=None)

    # Start background capture loop
    cap_loop = CaptureLoop(capturer, capture_region=None)
    cap_loop.start()
    # Register capture loop so bring_rdp_to_foreground can update its region
    bring_rdp_to_foreground._capture_loop = cap_loop
    log_event("INFO", "START", f"capture loop started: poll={cap_loop.poll_interval*1000:.0f}ms, full screen")

    # Periodic window re-scan: if HSRClient was not running at A-end startup
    # (e.g. start_a.bat launched before the client), TARGET_HWND is None or a
    # wrong fallback. This thread re-pins the correct window once it appears,
    # so a restart of A-end is no longer required.
    start_window_monitor(interval=2.0)
    log_event("INFO", "START", "window monitor started (periodic HSRClient re-scan)")

    # Start HTTP proxy
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    log_event("INFO", "START", f"proxy listening on {host}:{port}")
    log_event("INFO", "START", f"IDEA URL example: http://10.211.55.4:{port}")
    log_event("INFO", "START", "press Ctrl+C to stop")

    # Startup probe: run in the background so the proxy accepts requests
    # immediately. When A-end starts before HSRClient, wait until the periodic
    # window monitor discovers a trusted client HWND; probing earlier can never
    # cross the RDP clipboard and would falsely classify the current B-end as
    # legacy. The HTTP server remains available throughout this daemon wait.
    if not args.no_probe:
        def _probe_worker():
            try:
                wait_for_target_window()
                run_probe()
            except Exception as exc:
                log_event("ERROR", "PROBE", f"startup probe crashed: {exc}")
        threading.Thread(target=_probe_worker, daemon=True, name="qrtunnel-probe").start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("INFO", "STOP", "shutting down")
    finally:
        cap_loop.stop()
        server.server_close()
        release_single_instance()


if __name__ == "__main__":
    main()
