#!/usr/bin/env python3
"""Static + logic regression tests for the QR Tunnel first-phase upgrade.

These tests run on macOS without Windows: they extract pure functions from the
real source via AST and exercise them in isolation, and assert the invariants
that must hold after the upgrade (B-end read-only clipboard, QRT:IDLE marker,
version/config single sources and root-level startup scripts).
"""
import ast
import base64
import json
import sys
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
A_SRC = (ROOT / "a_end" / "a_proxy.py").read_text(encoding="utf-8")
B_SRC = (ROOT / "b_end" / "b_tunnel.py").read_text(encoding="utf-8")


def extract_function(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"function {name} not found")


def exec_function(source, name, namespace):
    code = extract_function(source, name)
    exec(compile(code, f"<{name}>", "exec"), namespace)
    return namespace[name]


def test_config_loading():
    import tempfile

    config_text = """
# comments and blank lines are allowed
a_listen: 0.0.0.0:9999
a_chunk: 2800
a_no_probe: false
b_page_ms: 300
b_disable_bulk: true
quoted: "hello world"  # trailing comments are ignored for bare values
number: -1
ratio: 1.5
"""
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(config_text, encoding="utf-8")
    for src, side in ((A_SRC, "a"), (B_SRC, "b")):
        ns = {"Path": Path, "re": __import__("re")}
        coerce = exec_function(src, "_coerce_config_value", ns)
        ns["_coerce_config_value"] = coerce
        load_config = exec_function(src, "load_config", ns)
        side_defaults = exec_function(src, "side_defaults", ns)
        parsed = load_config(path)
        assert parsed["a_listen"] == "0.0.0.0:9999"
        assert parsed["a_no_probe"] is False
        assert parsed["number"] == -1
        assert parsed["ratio"] == 1.5
        defaults = side_defaults(parsed, side)
        if side == "a":
            assert defaults == {"listen": "0.0.0.0:9999", "chunk": 2800,
                                "no_probe": False}
        else:
            assert defaults == {"page_ms": 300, "disable_bulk": True}
        assert load_config(path.with_name("missing.yaml")) == {}
    print("test_config_loading OK")


def test_root_startup_layout():
    assert (ROOT / "config.yaml").is_file()
    assert (ROOT / "start_a.bat").is_file()
    assert (ROOT / "start_b.bat").is_file()
    assert not (ROOT / "a_end" / "start_a.bat").exists()
    assert not (ROOT / "b_end" / "start_b.bat").exists()
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "a_python:" in config and "b_python:" in config
    assert "a_listen:" in config and "b_target:" in config
    for name, key, script in (("start_a.bat", "a_python:", A_SRC),
                               ("start_b.bat", "b_python:", B_SRC)):
        raw = (ROOT / name).read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
        text = raw.decode("utf-8")
        assert key in text
        assert "config.yaml" in text and "--config" in text
        assert script  # keep the side association explicit in this test
    assert "--config" in A_SRC and "--config" in B_SRC
    assert "side_defaults(load_config(config_path), \"a\")" in A_SRC
    assert "side_defaults(load_config(config_path), \"b\")" in B_SRC
    print("test_root_startup_layout OK")


def test_parse_request_validation():
    ns = {"json": json, "base64": base64}
    parse_request = exec_function(B_SRC, "parse_request", ns)

    def make(payload):
        return "QRT:b64:" + base64.b64encode(json.dumps(payload).encode()).decode()

    # parse_request is a method; pass a dummy self.
    def call(text):
        return parse_request(None, text)

    # Valid request
    req = call(make({"id": "abc-123", "method": "GET", "path": "/x", "headers": [["a", "b"]], "body": None}))
    assert req is not None and req["id"] == "abc-123"

    # Scalar JSON (not dict) must be rejected
    assert call("QRT:b64:" + base64.b64encode(b"123").decode()) is None
    assert call("QRT:b64:" + base64.b64encode(b'"str"').decode()) is None
    assert call("QRT:b64:" + base64.b64encode(b"[]").decode()) is None

    # Missing id / method / path
    assert call(make({"method": "GET", "path": "/x"})) is None
    assert call(make({"id": "x", "path": "/x"})) is None
    assert call(make({"id": "x", "method": "GET"})) is None

    # Non-string id
    assert call(make({"id": 123, "method": "GET", "path": "/x"})) is None

    # Malformed headers
    assert call(make({"id": "x", "method": "GET", "path": "/x", "headers": "nope"})) is None
    assert call(make({"id": "x", "method": "GET", "path": "/x", "headers": [["a"]]})) is None
    assert call(make({"id": "x", "method": "GET", "path": "/x", "headers": [[1, 2]]})) is None

    # Non-string body
    assert call(make({"id": "x", "method": "GET", "path": "/x", "body": 123})) is None

    # Malformed optional feature list
    assert call(make({"id": "x", "method": "GET", "path": "/x", "features": "bulk"})) is None
    assert call(make({"id": "x", "method": "GET", "path": "/x", "features": ["bulk", 1]})) is None

    # Non-QRT prefix
    assert call("hello") is None
    print("test_parse_request_validation OK")


def test_parse_meta_page_validation():
    ns = {"json": json}
    parse_meta_page = exec_function(A_SRC, "parse_meta_page", ns)

    good = json.dumps({"meta": True, "id": "abc", "chunks": 3, "status": 200}).encode()
    assert parse_meta_page(good) is not None

    # Not a dict
    assert parse_meta_page(b"123") is None
    assert parse_meta_page(b'"str"') is None
    assert parse_meta_page(b"[]") is None

    # Missing meta flag
    assert parse_meta_page(json.dumps({"id": "abc", "chunks": 3, "status": 200}).encode()) is None

    # Missing / wrong-typed required fields
    assert parse_meta_page(json.dumps({"meta": True, "chunks": 3, "status": 200}).encode()) is None
    assert parse_meta_page(json.dumps({"meta": True, "id": "abc", "status": 200}).encode()) is None
    assert parse_meta_page(json.dumps({"meta": True, "id": "abc", "chunks": "3", "status": 200}).encode()) is None
    assert parse_meta_page(json.dumps({"meta": True, "id": "abc", "chunks": 3, "status": "200"}).encode()) is None
    assert parse_meta_page(json.dumps({"meta": True, "id": "abc", "chunks": 3, "status": 200, "bulk": True}).encode()) is not None
    assert parse_meta_page(json.dumps({"meta": True, "id": "abc", "chunks": 3, "status": 200, "bulk": "yes"}).encode()) is None
    print("test_parse_meta_page_validation OK")


def test_range_encoding():
    ns = {}
    encode_ranges = exec_function(A_SRC, "encode_ranges", ns)
    decode_ranges = exec_function(B_SRC, "decode_ranges", ns)

    assert encode_ranges([1, 2, 3, 5, 6, 7, 10]) == "1-3,5-7,10"
    assert encode_ranges([]) == ""
    assert encode_ranges([5]) == "5"
    assert decode_ranges("0-3,5,7-10") == {0, 1, 2, 3, 5, 7, 8, 9, 10}
    assert decode_ranges("") == set()
    print("test_range_encoding OK")


def test_bulk_helpers():
    import gzip as _gzip

    # _compress_plan must count pages on the compressed payload (507-cap fix)
    ns = {"gzip": _gzip}
    compress_plan = exec_function(B_SRC, "_compress_plan", ns)

    # Highly compressible body: way fewer pages after gzip than raw.
    raw = (b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" * 1000)  # 32KB of 'a'
    data, use_gzip, n_chunks = compress_plan(raw, 2800)
    assert use_gzip is True
    assert len(data) < len(raw)
    raw_chunks = (len(raw) + 2799) // 2800
    assert n_chunks < raw_chunks, "compressed page count must be used for the cap"

    # Incompressible-ish body: plan and encode must agree on gzip decision.
    raw2 = bytes(range(256)) * 40  # all 256 byte values -> poor compression
    plan2 = compress_plan(raw2, 2800)
    enc_ns = {"gzip": _gzip, "json": json, "struct": __import__("struct"),
              "PROTOCOL_VERSION": "qrtunnel-qr-1", "VERSION": "0.4.0-dev",
              "blog_event": lambda *a, **k: None,
              "_compress_plan": compress_plan}
    encode_response = exec_function(B_SRC, "encode_response", enc_ns)
    pages_ck = encode_response(200, [], raw2, "11111111-2222-3333-4444-555555555555", 2800)
    meta_ck = json.loads(pages_ck[0].decode("utf-8"))
    assert meta_ck["gzip"] == plan2[1], "plan and encode must agree on gzip"
    assert meta_ck["chunks"] == len(pages_ck) - 1

    # encode_response with bulk=True tags the meta, never breaks the format.
    enc_ns = {"gzip": _gzip, "json": json, "struct": __import__("struct"),
              "PROTOCOL_VERSION": "qrtunnel-qr-1", "VERSION": "0.4.0-dev",
              "blog_event": lambda *a, **k: None,
              "_compress_plan": compress_plan}
    encode_response = exec_function(B_SRC, "encode_response", enc_ns)
    pages = encode_response(200, [("Content-Type", "text/plain")], b"hello world" * 300,
                            "11111111-2222-3333-4444-555555555555", 2800, bulk=True)
    assert isinstance(pages, list) and len(pages) >= 2
    meta = json.loads(pages[0].decode("utf-8"))
    assert meta["bulk"] is True
    assert meta["chunks"] == len(pages) - 1
    # Header+seq+id layout on data pages unchanged
    assert pages[1][0] == 0x01

    # Non-bulk meta must not carry the flag.
    pages2 = encode_response(200, [], b"x" * 300,
                             "11111111-2222-3333-4444-555555555555", 2800)
    assert "bulk" not in json.loads(pages2[0].decode("utf-8"))

    # _select_transfer_plan: only an explicitly advertised Bulk capability
    # enables the larger chunk; disable/legacy/empty features stay normal QR.
    plan_ns = {
        "_compress_plan": lambda body, chunk: (b"data", False,
                                                500 if chunk == 2800 else 483),
    }
    select_plan = exec_function(B_SRC, "_select_transfer_plan", plan_ns)
    assert select_plan(b"body", 2800, 2900, 400, False, ["bulk"])
    use_bulk, eff_chunk, normal, effective = select_plan(
        b"body", 2800, 2900, 400, False, ["bulk"])
    assert (use_bulk, eff_chunk, normal, effective) == (True, 2900, 500, 483)
    assert select_plan(b"body", 2800, 2900, 400, True, ["bulk"])[0] is False
    assert select_plan(b"body", 2800, 2900, 400, False, ["probe"])[0] is False
    assert select_plan(b"body", 2800, 2900, 400, False, None)[0] is False

    # CLI surface/params present.
    assert "--disable-bulk" in B_SRC
    assert "--bulk-threshold" in B_SRC
    assert "--bulk-chunk" in B_SRC
    assert '"bulk"' in A_SRC and '"bulk"' in B_SRC
    print("test_bulk_helpers OK")


def test_invariants():
    # B-end must remain strictly read-only on the clipboard.
    for forbidden in ("SetClipboardData", "EmptyClipboard", "GlobalAlloc"):
        assert forbidden not in B_SRC, f"B-end must not contain {forbidden}"
    # A-end must keep the non-empty idle marker.
    assert "QRT:IDLE" in A_SRC
    # Single VERSION source at repo root; per-end copies must not exist.
    root_ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert root_ver, "root VERSION must be non-empty"
    assert not (ROOT / "a_end" / "VERSION").exists(), "a_end must not carry VERSION"
    assert not (ROOT / "b_end" / "VERSION").exists(), "b_end must not carry VERSION"
    # Both ends read script-dir VERSION first, else fall back to PROJECT_ROOT
    # (repo root), else the hardcoded default — which must match root VERSION.
    for src_name, src in (("A", A_SRC), ("B", B_SRC)):
        assert 'PROJECT_ROOT / "VERSION"' in src, f"{src_name} must fall back to root VERSION"
        assert f'VERSION = "{root_ver}"' in src, (
            f"{src_name} hardcoded default must stay in sync with root VERSION")
    # Config is a single root source; both entry points support it.
    assert (ROOT / "config.yaml").is_file()
    assert "--config" in A_SRC and "load_config(config_path)" in A_SRC
    assert "--config" in B_SRC and "load_config(config_path)" in B_SRC
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    for key in ("a_python:", "b_python:", "a_listen:", "b_target:",
                "b_page_ms:", "b_max_qr:", "b_bulk_chunk:"):
        assert key in config_text, f"config.yaml missing {key}"
    # Startup scripts belong at repository root and pass the same config file.
    for name, python_key, script_path in (
        ("start_a.bat", "a_python:", "a_end\\a_proxy.py"),
        ("start_b.bat", "b_python:", "b_end\\b_tunnel.py"),
    ):
        path = ROOT / name
        assert path.is_file(), f"missing root {name}"
        raw = path.read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
        text = raw.decode("utf-8")
        assert python_key in text and "config.yaml" in text
        assert "--config" in text and script_path in text
    assert not (ROOT / "a_end" / "start_a.bat").exists()
    assert not (ROOT / "b_end" / "start_b.bat").exists()
    # Protocol/version metadata present on both ends.
    assert "PROTOCOL_VERSION" in A_SRC and "PROTOCOL_VERSION" in B_SRC
    assert "client_version" in A_SRC and "server_version" in B_SRC
    # QuickEdit + mutex present on both ends.
    assert "configure_console_quickedit" in A_SRC and "configure_console_quickedit" in B_SRC
    assert "acquire_single_instance" in A_SRC and "acquire_single_instance" in B_SRC
    # HEAD support on A.
    assert "def do_HEAD" in A_SRC
    print("test_invariants OK")


def test_summary_stale_field_reset():
    import tempfile
    import threading
    import time as _time

    # Extract _write_summary from A source and run it against mocked globals.
    code = extract_function(A_SRC, "_write_summary")
    tmpdir = Path(tempfile.mkdtemp())
    ns = {
        "json": json,
        "time": _time,
        "threading": threading,
        "VERSION": "0.4.0-dev",
        "SUMMARY_PATH": tmpdir / "latest.json",
        "HISTORY_PATH": tmpdir / "history.jsonl",
        "SUMMARY_LOCK": threading.RLock(),
        "_last_summary": {
            "version": "0.4.0-dev", "role": "A", "status": "failed",
            "request_id": "old", "http_status": 401, "response_bytes": 26,
            "elapsed_seconds": 125.0, "failure_reason": "response_timeout",
            "bulk": True, "bulk_chunk": 2900,
        },
        "_last_history_key": None,
        "log_event": lambda *a, **k: None,
    }
    exec(compile(code, "<_write_summary>", "exec"), ns)
    write_summary = ns["_write_summary"]

    # Simulate a new request starting after a failed one.
    write_summary({"status": "in_progress", "request_id": "new", "method": "GET", "path": "/x"})
    snap = ns["_last_summary"]
    for stale in ("http_status", "response_bytes", "elapsed_seconds", "failure_reason", "terminal_reason", "qr_pages", "bulk", "bulk_chunk"):
        assert stale not in snap, f"stale field {stale} leaked into new request"
    assert snap["request_id"] == "new"
    assert snap["status"] == "in_progress"
    print("test_summary_stale_field_reset OK")


def test_probe_detection():
    ns = {"PROBE_PATH": "/__qrtunnel/probe"}
    is_probe_request = exec_function(B_SRC, "is_probe_request", ns)

    assert is_probe_request({"probe": True, "method": "GET", "path": "/__qrtunnel/probe"})
    assert is_probe_request({"probe": True, "method": "GET", "path": "/anything"})
    assert is_probe_request({"method": "GET", "path": "/__qrtunnel/probe"})
    # Not a probe: different method, different path, no flag
    assert not is_probe_request({"method": "POST", "path": "/__qrtunnel/probe"})
    assert not is_probe_request({"method": "GET", "path": "/repo/info/refs"})
    assert not is_probe_request({"probe": False})
    assert not is_probe_request(None)
    assert not is_probe_request("not-a-dict")
    print("test_probe_detection OK")


def test_build_probe_response():
    ns = {"json": json, "time": _time, "VERSION": "0.4.0-dev",
          "PROTOCOL_VERSION": "qrtunnel-qr-1", "FEATURES": ["probe"]}
    build_probe_response = exec_function(B_SRC, "build_probe_response", ns)

    status, headers, body = build_probe_response()
    assert status == 200
    assert ("Content-Type", "application/json; charset=utf-8") in headers
    obj = json.loads(body.decode("utf-8"))
    assert obj["probe"] is True
    assert obj["role"] == "B"
    assert obj["version"] == "0.4.0-dev"
    assert obj["protocol"] == "qrtunnel-qr-1"
    assert obj["features"] == ["probe"]
    # Content-Length matches actual body length
    clen = dict(headers).get("Content-Length")
    assert clen is not None and int(clen) == len(body)
    print("test_build_probe_response OK")


def test_parse_probe_response():
    ns = {"json": json}
    parse_probe_response = exec_function(A_SRC, "parse_probe_response", ns)

    good = {"probe": True, "role": "B", "version": "0.4.0-dev",
            "protocol": "qrtunnel-qr-1", "features": ["probe"]}
    body = json.dumps(good).encode()
    cap = parse_probe_response(200, [], body)
    assert cap is not None and cap["version"] == "0.4.0-dev" and cap["features"] == ["probe"]

    # Wrong status
    assert parse_probe_response(404, [], body) is None
    # Non-JSON body
    assert parse_probe_response(200, [], b"<html>404 Not Found</html>") is None
    # Missing probe flag / wrong flag
    assert parse_probe_response(200, [], json.dumps({"version": "x"}).encode()) is None
    assert parse_probe_response(200, [], json.dumps({"probe": False, "version": "x"}).encode()) is None
    # Missing / wrong-typed fields
    assert parse_probe_response(200, [], json.dumps({"probe": True, "version": 123}).encode()) is None
    assert parse_probe_response(200, [], json.dumps({"probe": True, "version": "x", "protocol": 1}).encode()) is None
    assert parse_probe_response(200, [], json.dumps({"probe": True, "version": "x", "protocol": "y", "features": "nope"}).encode()) is None
    print("test_parse_probe_response OK")


def test_probe_match_ok():
    ns = {"PROTOCOL_VERSION": "qrtunnel-qr-1", "VERSION": "0.4.0-dev"}
    _probe_match_ok = exec_function(A_SRC, "_probe_match_ok", ns)

    ok, why = _probe_match_ok({"protocol": "qrtunnel-qr-1", "version": "0.4.0-dev"})
    assert ok and why == "ok"
    ok, why = _probe_match_ok({"protocol": "qrtunnel-qr-1", "version": "0.2.1"})
    assert not ok and "version mismatch" in why
    ok, why = _probe_match_ok({"protocol": "other", "version": "0.4.0-dev"})
    assert not ok and "protocol mismatch" in why
    print("test_probe_match_ok OK")


def test_file_logging_present():
    # Both ends must set up a rotating file log and advertise the probe feature.
    for src in (A_SRC, B_SRC):
        assert "RotatingFileHandler" in src, "missing RotatingFileHandler"
        assert "tunnel.log" in src, "missing tunnel.log path"
        assert '"probe"' in src, "missing probe feature advertisement"
        assert "PROBE_PATH" in src, "missing PROBE_PATH"
    assert "def run_probe" in A_SRC, "missing A-end run_probe"
    assert "def build_probe_response" in B_SRC, "missing B-end probe response builder"
    assert "--no-probe" in A_SRC, "missing A-end --no-probe switch"
    print("test_file_logging_present OK")


def test_no_missing_global_declaration():
    """Regression: bring_rdp_to_foreground assigns _last_alt_sent but its
    global statement omitted it, so Python treated it as a local and raised
    UnboundLocalError the first time Layer-2 Alt unlock ran (real deployment:
    'cannot access local variable _last_alt_sent'). Scan both sources for any
    function that assigns a module-level name without declaring it global.
    """
    import re as _re

    for name, src in (("A", A_SRC), ("B", B_SRC)):
        tree = ast.parse(src)
        module_assigns = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        module_assigns.add(t.id)
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                declared = set()
                for n in ast.walk(fn):
                    if isinstance(n, ast.Global):
                        declared.update(n.names)
                assigned = {n.id for n in ast.walk(fn)
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                bad = (assigned & module_assigns) - declared
                assert not bad, (
                    f"{name}-end {fn.name}() assigns module-level {sorted(bad)} "
                    f"without global declaration"
                )
    print("test_no_missing_global_declaration OK")


def test_window_selection_guards():
    """Regression: A-end must never pick a system overlay (e.g. TextInputHost
    touch keyboard) as the RDP window. Real deployment matched
    'Windows 输入体验' / TextInputHost.exe (score=20, pure area) when HSRClient
    wasn't running yet, which silently broke clipboard sync until A-end restart.
    Guards: blocklist in enum_proc, no arbitrary-window fallback in automatic
    mode, and a periodic re-scan thread so a late-starting HSRClient gets pinned
    without restarting A-end.
    """
    fn = extract_function(A_SRC, "find_target_window")
    assert "textinputhost.exe" in fn.lower(), "missing system-window blocklist"
    assert "Shell_TrayWnd" in fn, "missing taskbar class blocklist"
    assert "never choose by size/score alone" in fn, "automatic mode still allows blind fallback"
    assert "results = []" in fn, "automatic no-match path must return no candidate"
    assert "start_window_monitor" in A_SRC, "missing periodic window monitor"
    assert "qrtunnel-window-monitor" in A_SRC, "missing monitor thread name"
    # _score_candidate must treat TextInputHost-like pure-area as low.
    score_fn = extract_function(A_SRC, "_score_candidate")
    assert "score += min(w * h / 2000000.0, 1.0) * 20" in score_fn
    print("test_window_selection_guards OK")


def test_deferred_probe_waits_for_window():
    """Startup probe must wait for a trusted HSRClient HWND when A-end starts
    first. It must not write a doomed probe and falsely classify B as legacy.
    """
    code = extract_function(A_SRC, "wait_for_target_window")

    class FakeTime:
        def __init__(self):
            self.now = 0.0
        def monotonic(self):
            return self.now
        def sleep(self, seconds):
            self.now += seconds

    class FakeUser32:
        def IsWindow(self, hwnd):
            return hwnd == 123

    fake_time = FakeTime()
    calls = {"rescan": 0}
    ns = {
        "time": fake_time,
        "TARGET_HWND": None,
        "_user32": FakeUser32(),
        "log_event": lambda *a, **k: None,
    }

    def rescan():
        calls["rescan"] += 1
        if calls["rescan"] == 2:
            ns["TARGET_HWND"] = 123

    ns["_rescan_target_window"] = rescan
    exec(compile(code, "<wait_for_target_window>", "exec"), ns)
    assert ns["wait_for_target_window"](timeout=1.0, poll_interval=0.05, log_interval=1.0)
    assert calls["rescan"] == 2

    # Timeout path is bounded for tests/diagnostics; production uses None.
    fake_time2 = FakeTime()
    ns2 = {
        "time": fake_time2,
        "TARGET_HWND": None,
        "_user32": FakeUser32(),
        "log_event": lambda *a, **k: None,
        "_rescan_target_window": lambda: None,
    }
    exec(compile(code, "<wait_for_target_window_timeout>", "exec"), ns2)
    assert not ns2["wait_for_target_window"](
        timeout=0.1, poll_interval=0.05, log_interval=1.0)
    assert "wait_for_target_window()" in A_SRC
    assert "wait_for_target_window()\n                run_probe()" in A_SRC
    print("test_deferred_probe_waits_for_window OK")


def test_probe_and_426_local_response_no_crash():
    """Regression: B-end _process_request must not crash on local-response
    paths (startup probe, HTTP 426 compatibility error) where worker and
    forward_control are None. Real deployment hit
    AttributeError: 'NoneType' object has no attribute 'join' at worker.join().
    """
    import threading as _threading
    import time as _time

    code = extract_function(B_SRC, "_process_request")
    summaries = []

    class FakeDisplay:
        def show_pages(self, pages, req_id):
            return "done"

    class FakeSelf:
        def __init__(self, is_probe):
            self.is_probe = is_probe
            self.processed = {}
            self.chunk_bytes = 2800
            self.max_pages = 500
            self.page_ms = 200
            self.ack_ms = 800
            self.display_mode = "tkinter"
            self.target = ("192.168.21.14", 8888)
            self.display = FakeDisplay()
        def log(self, msg):
            pass
        def log_req(self, level, phase, message, req_id):
            pass
        def parse_request(self, text):
            if self.is_probe:
                return {"id": "c0e2bef4-1111-2222-3333-444444444444",
                        "method": "GET", "path": "/__qrtunnel/probe",
                        "headers": [], "body": None, "protocol": "qrtunnel-qr-1",
                        "client_version": "0.4.0-dev", "features": ["probe"],
                        "probe": True}
            # 426 path: same build metadata but a mismatched protocol
            return {"id": "426path-1111-2222-3333-444444444444",
                    "method": "GET", "path": "/repo/info/refs",
                    "headers": [], "body": None, "protocol": "qrtunnel-qr-OLD",
                    "client_version": "0.4.0-dev"}
        def cleanup(self):
            pass
        def observe_completed_clipboard(self, req_id):
            return False
        def _is_cancelled(self, req_id):
            return False
        disable_bulk = True
        bulk_threshold = 400
        bulk_chunk = 2900

    ns = {
        "json": json,
        "time": _time,
        "threading": _threading,
        "PROTOCOL_VERSION": "qrtunnel-qr-1",
        "VERSION": "0.4.0-dev",
        "is_probe_request": lambda req: bool(req and req.get("probe") is True)
                                       or (bool(req) and req.get("path") == "/__qrtunnel/probe"
                                           and req.get("method") == "GET"),
        "build_probe_response": lambda: (200, [("Content-Type", "application/json; charset=utf-8")],
                                         b'{"probe":true,"role":"B","version":"0.4.0-dev"}'),
        "show_ack": lambda req_id, hold_ms: None,
        "show_stopped": lambda req_id, hold_ms: None,
        "encode_response": lambda status, headers, body, req_id, chunk_bytes, bulk=False: ["meta", "data"],
        "_compress_plan": lambda body, chunk_bytes: (body, False, 1),
        "_select_transfer_plan": lambda body, chunk_bytes, bulk_chunk, bulk_threshold, disable_bulk, peer_features=None: (False, chunk_bytes, 1, 1),
        "_write_summary": lambda upd: summaries.append(upd),
        "ForwardControl": lambda: None,
        "forward_request": lambda *a, **k: (200, [], b""),
        "get_screen_size": lambda: (1920, 1080),
    }

    # --- Probe path ---
    fake = FakeSelf(is_probe=True)
    exec(compile(code, "<_process_request>", "exec"), ns)
    ns["_process_request"](fake, "QRT:b64:probe")
    req_id = "c0e2bef4-1111-2222-3333-444444444444"
    assert req_id in fake.processed, "probe req_id must be marked processed"
    assert any(s.get("status") == "probe_completed" for s in summaries), summaries
    assert not any(s.get("status") == "completed" for s in summaries), summaries

    # --- 426 path (local error response, no forward worker) ---
    fake2 = FakeSelf(is_probe=False)
    ns["_process_request"](fake2, "QRT:b64:426")
    req_id2 = "426path-1111-2222-3333-444444444444"
    assert req_id2 in fake2.processed, "426 req_id must be marked processed"
    assert any(s.get("status") == "completed" for s in summaries), summaries
    print("test_probe_and_426_local_response_no_crash OK")


def test_compose_frame():
    """Per-frame compose (render-speedup building block) must produce one
    canvas with every QR centered in its cell and empty cells left black."""
    import PIL.Image as _PILImage

    ns = {"Image": _PILImage}
    compose_qr_frame = exec_function(B_SRC, "compose_qr_frame", ns)

    img = _PILImage.new("L", (10, 10), 255)  # white QR placeholder (10x10)
    canvas = compose_qr_frame([img, img, img], 2, 2, 20, 20)
    assert canvas.mode == "L"
    assert canvas.size == (40, 40)
    # Canvas background is black.
    assert canvas.getpixel((0, 0)) == 0
    # Cell 0 (top-left): image centered at x=(20-10)//2=5, y=5 -> center (10,10).
    assert canvas.getpixel((10, 10)) == 255
    # Cell 1 (top-right): center (30,10). Cell 2 (bottom-left): center (10,30).
    assert canvas.getpixel((30, 10)) == 255
    assert canvas.getpixel((10, 30)) == 255
    # Cell 3 has no image (only 3 given): stays black.
    assert canvas.getpixel((30, 30)) == 0
    # Images beyond cell capacity are ignored (first pages win, like the old
    # per-label grid where extra labels were never placed): with 5 images the
    # first 4 fill all cells and the 5th is dropped.
    canvas2 = compose_qr_frame([img] * 5, 2, 2, 20, 20)
    assert canvas2.size == (40, 40)
    assert canvas2.getpixel((10, 10)) == 255
    assert canvas2.getpixel((30, 30)) == 255  # cell 3 filled by 4th image
    # Non-square qr_w/qr_h cell: image centered in both axes.
    img2 = _PILImage.new("L", (8, 8), 255)
    canvas3 = compose_qr_frame([img2], 1, 1, 30, 12)
    assert canvas3.size == (30, 12)
    # x = (30-8)//2 = 11, y = (12-8)//2 = 2 -> image spans (11..18, 2..9).
    assert canvas3.getpixel((11, 2)) == 255
    assert canvas3.getpixel((18, 9)) == 255
    assert canvas3.getpixel((0, 0)) == 0
    print("test_compose_frame OK")


def main():
    test_config_loading()
    test_root_startup_layout()
    test_parse_request_validation()
    test_parse_meta_page_validation()
    test_range_encoding()
    test_invariants()
    test_summary_stale_field_reset()
    test_probe_detection()
    test_build_probe_response()
    test_parse_probe_response()
    test_probe_match_ok()
    test_file_logging_present()
    test_no_missing_global_declaration()
    test_window_selection_guards()
    test_deferred_probe_waits_for_window()
    test_bulk_helpers()
    test_compose_frame()
    test_probe_and_426_local_response_no_crash()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()