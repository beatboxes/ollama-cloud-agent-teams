"""Round-3 regression tests for scripts/ollama_cloud_delegate.py and docs.

stdlib unittest only. Most tests mock urllib and subprocess in-process. A few
talk to a throwaway mock daemon bound to 127.0.0.2 on an ephemeral port (never
the real daemon's port) and one runs a short child interpreter under a hard
timeout. No network, no `ollama` binary (the spawn tests use a fake shim on a
temporary PATH). tests/test_delegate.py and tests/test_round2.py stay
untouched. Run from the repo root:

    python3 -I -m unittest discover -s tests -v

Covers the round-2 review findings:
  R2-1 HTTP status-line reason phrase is daemon text   -> redact + strip + cap
  R2-2 control-character set                           -> C1, U+2028/9, bidi
  R2-3 order of strip / redact / cap                   -> strip, redact, cap
  R2-4 http.client errors and RecursionError           -> exit 14, valid JSON
  R2-5 whitespace / control / backslash in OLLAMA_HOST -> exit 15
  R2-6 percent in the host name                        -> exit 15 (zone id ok)
  R2-7 host validated before any `ollama list` spawn   -> 0 spawns when rejected
  R2-8 bounded error-body read and bounded redact      -> 64 KiB scan limit
  R2-9 docs list exactly the models ROUTING_MAP routes -> DocRoutingExact
"""

import contextlib
import gc
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import warnings
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
SCRIPT = os.path.join(ROOT, "scripts", "ollama_cloud_delegate.py")

_spec = importlib.util.spec_from_file_location("ollama_cloud_delegate_r3", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

REAL_URLOPEN = urllib.request.urlopen  # captured before any test patches it

DEEPSEEK = "deepseek-v4.1-flash:cloud"
TOKEN = "abcDEF123.tok"                      # shape matched by the Bearer pattern
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123"  # shape matched by the sk- pattern
DELEGATE = ["--task", "deep-debugging", "--role", "debugger",
            "--model", DEEPSEEK, "--prompt", "x"]

# Everything clean_daemon_text must turn into a space (R2-2).
WIDE_CONTROL = re.compile(
    "[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
WIDE_CONTROL_CODEPOINTS = (
    list(range(0x00, 0x20)) + list(range(0x7F, 0xA0)) + [0x2028, 0x2029]
    + list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)))


# --- helpers ----------------------------------------------------------------

def run_cli(argv, env=None):
    """Run mod.main(argv) the way the `__main__` block does.

    Returns (exit_code, stdout, stderr). Unexpected exceptions map to exit 16
    exactly like the script's own entry point does.
    """
    out, err = io.StringIO(), io.StringIO()
    env = dict(env or {})
    # urllib's HTTPError is closed by the garbage collector, which writes a
    # ResourceWarning to stderr under -W always / -W error: keep it out of the
    # captured stderr (and out of later tests) by finalising inside the block.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            os.environ.pop("OLLAMA_CLOUD_MODELS", None)
            if "OLLAMA_HOST" not in env:
                os.environ.pop("OLLAMA_HOST", None)
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code
            except KeyboardInterrupt:
                code = mod.EXIT_PYTHON_ERROR
            except Exception as e:  # noqa: BLE001 - mirrors the script entry point
                print(f"ERROR [{mod.EXIT_PYTHON_ERROR}]: {e}", file=sys.stderr)
                code = mod.EXIT_PYTHON_ERROR
        gc.collect()
    return code, out.getvalue(), err.getvalue()


_OPENED = []  # HTTPError objects to close at module teardown (no ResourceWarning)


def tearDownModule():
    while _OPENED:
        _OPENED.pop().close()


def http_error(code, body, reason="Error"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    err = urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat", code, reason, {}, io.BytesIO(raw))
    _OPENED.append(err)
    return err


class FakeResp:
    """What urlopen returns on success: a context manager with read()/status."""

    status = 200

    def __init__(self, body=b"", read_exc=None):
        self.body, self.read_exc = body, read_exc

    def read(self, *args):
        if self.read_exc is not None:
            raise self.read_exc
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def live(urlopen_kwargs, extra_env=None, json_out=True):
    """Live (non dry-run) delegation with discovery and urlopen mocked."""
    argv = DELEGATE + (["--json"] if json_out else [])
    with mock.patch.object(mod, "discover_cloud_models",
                           return_value=([DEEPSEEK], [], [])), \
            mock.patch.object(mod.urllib.request, "urlopen",
                              **urlopen_kwargs) as urlopen:
        res = run_cli(argv, env=extra_env)
    return res + (urlopen,)


def quick_urlopen(req, *args, **kwargs):
    """The real urlopen with a short timeout so a misbehaving mock cannot hang."""
    return REAL_URLOPEN(req, timeout=10)


def raw_http(status, reason, body=b"", clen=None, extra=b""):
    if clen is None:
        clen = len(body)
    head = (b"HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n%sConnection: close\r\n\r\n"
            % (status, reason, clen, extra))
    return head + body


class MockDaemon:
    """Loopback HTTP mock on 127.0.0.2 (ephemeral port) replying with canned bytes."""

    HOST = "127.0.0.2"

    def __init__(self, reply):
        self.hits = 0
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                outer._serve(self.request, reply)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            self.server = Server((self.HOST, 0), Handler)
        except OSError as e:  # e.g. platforms without 127.0.0.0/8 loopback
            raise unittest.SkipTest(f"cannot bind {self.HOST}: {e}")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self.thread.start()

    def _serve(self, conn, reply):
        try:
            conn.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                head += chunk
            header, _, body = head.partition(b"\r\n\r\n")
            length = 0
            for line in header.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            self.hits += 1
            conn.sendall(reply)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def via_loopback(reply, json_out=True):
    """Run the CLI against a MockDaemon answering `reply`. -> (code, out, err, hits)."""
    daemon = MockDaemon(reply)
    try:
        argv = DELEGATE + (["--json"] if json_out else [])
        env = {"OLLAMA_HOST": f"{daemon.HOST}:{daemon.port}",
               "no_proxy": "*", "NO_PROXY": "*"}
        with mock.patch.object(mod, "discover_cloud_models",
                               return_value=([DEEPSEEK], [], [])), \
                mock.patch.object(mod.urllib.request, "urlopen",
                                  side_effect=quick_urlopen):
            res = run_cli(argv, env=env)
    finally:
        daemon.close()
    return res + (daemon.hits,)


class CliAssertions(unittest.TestCase):
    """Shared assertions on CLI output."""

    def json_error(self, out, code):
        payload = json.loads(out)  # raises unless stdout is valid JSON
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["exit_code"], code)
        return payload["error"]

    def plain_error(self, out, err, code):
        self.assertEqual(out, "")
        self.assertIn(f"ERROR [{code}]", err)
        return err.rstrip("\n")  # drop print()'s own newline


# --- R2-1 -------------------------------------------------------------------

class ReasonPhraseIsSanitised(CliAssertions):
    """R2-1: the status-line reason phrase is daemon text like any other."""

    HOSTILE = f"\x1b[31mBearer {TOKEN}\x1b[0m SK-{SECRET}"

    def assert_clean(self, text):
        self.assertIsNone(WIDE_CONTROL.search(text), repr(text))
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(SECRET, text)

    def test_call_ollama_redacts_and_strips_the_reason(self):
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=http_error(500, {}, self.HOSTILE)):
            raw, status, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual((raw, status), ("", 500))
        self.assertTrue(err.startswith("HTTP 500: "), err)
        self.assert_clean(err)
        self.assertIn("REDACTED", err)

    def test_c1_and_separators_in_the_reason_become_spaces(self):
        reason = "C1\x9b31mHERE\x85x\u2028y\u202ez\u2066w"
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=http_error(500, {}, reason)):
            _, _, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual(err, "HTTP 500: C1 31mHERE x y z w")

    def test_reason_is_capped_at_300_chars(self):
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=http_error(500, {}, "R" * 1000)):
            _, _, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual(err, "HTTP 500: " + "R" * 300)

    def test_ordinary_reason_and_detail_are_unchanged(self):
        body = {"error": "boom"}
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=http_error(500, body, "Internal Server Error")):
            _, _, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual(err, "HTTP 500: Internal Server Error: boom")

    def test_auth_error_detail_is_clean_end_to_end(self):
        code, out, _, _ = live({"side_effect": http_error(401, {}, self.HOSTILE)})
        self.assertEqual(code, mod.EXIT_AUTH_REQUIRED)
        self.assert_clean(json.loads(out)["error"])

    def test_loopback_status_line_json(self):
        reply = raw_http(500, self.HOSTILE.encode(), b"{}")
        code, out, _, hits = via_loopback(reply)
        self.assertEqual(hits, 1)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        msg = self.json_error(out, 14)
        self.assert_clean(msg)
        self.assertIn("REDACTED", msg)

    def test_loopback_status_line_plain_stderr(self):
        reply = raw_http(500, self.HOSTILE.encode(), b"{}")
        code, out, err, _ = via_loopback(reply, json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assert_clean(self.plain_error(out, err, 14))

    def test_loopback_c1_byte_in_status_line(self):
        # http.client decodes the status line as latin-1, so 0x9b arrives as U+009B.
        reply = raw_http(500, b"C1\x9b31mHERE", b"{}")
        code, out, _, _ = via_loopback(reply)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        msg = self.json_error(out, 14)
        self.assertIsNone(WIDE_CONTROL.search(msg), repr(msg))
        self.assertIn("C1 31mHERE", msg)


# --- R2-2 -------------------------------------------------------------------

class ControlCharacterSet(CliAssertions):
    """R2-2: C1 controls, U+2028/2029 and bidi embedding/override/isolate controls."""

    def test_every_listed_code_point_becomes_a_space(self):
        survivors = [hex(cp) for cp in WIDE_CONTROL_CODEPOINTS
                     if mod.clean_daemon_text(f"a{chr(cp)}b") != "a b"]
        self.assertEqual(survivors, [])

    def test_neighbours_of_the_ranges_are_kept(self):
        # Off-by-one guards: the code points just outside each range, plus
        # ordinary non-ASCII text, must survive untouched.
        keep = [0x20, 0x7E, 0xA0, 0xA1, 0xE9, 0x3042, 0x2027, 0x202F, 0x2065,
                0x206A, 0x1F600]
        mangled = [hex(cp) for cp in keep
                   if mod.clean_daemon_text(f"a{chr(cp)}b") != f"a{chr(cp)}b"]
        self.assertEqual(mangled, [])

    def test_c1_csi_and_bidi_override_in_an_error_body_end_to_end(self):
        text = "x\u009b31mCSI\u009b0m y\u0085z\u2028w\u202ev\u2066u"
        code, out, _, _ = live({"side_effect": http_error(500, {"error": text})})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        msg = json.loads(out)["error"]
        self.assertIsNone(WIDE_CONTROL.search(msg), repr(msg))
        self.assertTrue(msg.endswith("x 31mCSI 0m y z w v u"), repr(msg))

    def test_c1_csi_in_an_error_body_plain_stderr(self):
        text = "x\u009b31mCSI y\u202ez"
        code, out, err, _ = live({"side_effect": http_error(500, {"error": text})},
                                 json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIsNone(WIDE_CONTROL.search(self.plain_error(out, err, 14)))


# --- R2-3 -------------------------------------------------------------------

class StripBeforeRedact(CliAssertions):
    """R2-3: a control character must not hide a secret from the redactor."""

    CASES = [
        ("x api_key\x00=\x00hunter2 y", "hunter2", "api_key=***REDACTED:key***"),
        ("x Bearer\x00tok y", "tok", "Bearer ***REDACTED:bearer***"),
        ("x Bearer\x00" + TOKEN + " y", TOKEN, "Bearer ***REDACTED:bearer***"),
        ("x token\x1b=hunter2 y", "hunter2", "token=***REDACTED:token***"),
        ("x password\x7f=\x7fhunter2 y", "hunter2", "password=***REDACTED:password***"),
        ("x password\x9b=\x9bhunter2 y", "hunter2", "password=***REDACTED:password***"),
        ("x client_secret\u2028=\u2028hunter2 y", "hunter2", "client_secret=***REDACTED:client_secret***"),
        ("x API_KEY\u202e=\u202ehunter2 y", "hunter2", "API_KEY=***REDACTED:key***"),
    ]

    def test_split_secrets_are_redacted(self):
        for text, secret, marker in self.CASES:
            with self.subTest(text=text):
                out = mod.clean_daemon_text(text)
                self.assertNotIn(secret, out)
                self.assertIn(marker, out)
                self.assertIsNone(WIDE_CONTROL.search(out), repr(out))

    def test_lead_cases_exact(self):
        self.assertEqual(mod.clean_daemon_text("x api_key\x00=\x00hunter2 y"),
                         "x api_key=***REDACTED:key*** y")
        self.assertEqual(mod.clean_daemon_text("x Bearer\x00tok y"),
                         "x Bearer ***REDACTED:bearer*** y")

    def test_end_to_end_json_and_plain(self):
        body = {"error": "a api_key\x00=\x00hunter2 and Bearer\x00" + TOKEN}
        code, out, _, _ = live({"side_effect": http_error(500, body)})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        msg = json.loads(out)["error"]
        self.assertNotIn("hunter2", msg)
        self.assertNotIn(TOKEN, msg)
        code, out, err, _ = live({"side_effect": http_error(500, body)}, json_out=False)
        self.assertNotIn("hunter2", err)
        self.assertNotIn(TOKEN, err)

    def test_cap_is_still_applied_last(self):
        # strip -> redact -> cap: a secret straddling char 300 never leaves a fragment.
        out = mod.clean_daemon_text("A" * 290 + SECRET)
        self.assertLessEqual(len(out), 300)
        self.assertNotIn("sk-", out)
        out = mod.clean_daemon_text("A" * 290 + "api_key\x00=\x00hunter2")
        self.assertLessEqual(len(out), 300)
        self.assertNotIn("hunter2", out)


# --- R2-4 -------------------------------------------------------------------

PROTOCOL_ERRORS = [
    http.client.BadStatusLine("SSH-2.0-OpenSSH_9.6"),
    http.client.LineTooLong("status line"),
    http.client.InvalidURL("bad"),
    http.client.UnknownProtocol("HTTP/9"),
    http.client.NotConnected(),
    http.client.CannotSendRequest(),
    http.client.ResponseNotReady(),
    http.client.ImproperConnectionState(),
    http.client.UnknownTransferEncoding("zzz"),
    http.client.IncompleteRead(b"x", 5),
    http.client.HTTPException("generic"),
]


class ProtocolErrorsMapToExit14(CliAssertions):
    """R2-4: malformed or truncated HTTP replies are daemon errors (14), not 16."""

    def test_exception_raised_by_urlopen(self):
        for exc in PROTOCOL_ERRORS:
            with self.subTest(exc=type(exc).__name__):
                code, out, _, _ = live({"side_effect": exc})
                self.assertEqual(code, mod.EXIT_API_ERROR)
                msg = self.json_error(out, 14)
                self.assertIn("HTTP protocol error", msg)
                self.assertIn(type(exc).__name__, msg)

    def test_exception_raised_by_urlopen_plain_mode(self):
        for exc in PROTOCOL_ERRORS:
            with self.subTest(exc=type(exc).__name__):
                code, out, err, _ = live({"side_effect": exc}, json_out=False)
                self.assertEqual(code, mod.EXIT_API_ERROR)
                self.plain_error(out, err, 14)

    def test_truncated_200_body_read(self):
        resp = FakeResp(read_exc=http.client.IncompleteRead(b'{"mess', 982))
        code, out, _, _ = live({"return_value": resp})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIn("IncompleteRead", self.json_error(out, 14))

    def test_truncated_chunked_200_body_read(self):
        resp = FakeResp(read_exc=http.client.IncompleteRead(b"", None))
        code, out, _, _ = live({"return_value": resp})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIn("IncompleteRead", self.json_error(out, 14))

    def test_recursion_error_from_urlopen_and_from_read(self):
        for kwargs in ({"side_effect": RecursionError("deep")},
                       {"return_value": FakeResp(read_exc=RecursionError("deep"))}):
            with self.subTest(kwargs=sorted(kwargs)):
                code, out, _, _ = live(kwargs)
                self.assertEqual(code, mod.EXIT_API_ERROR)
                self.assertIn("RecursionError", self.json_error(out, 14))

    def test_bad_status_line_text_is_not_echoed(self):
        # The status line is attacker-controlled; only the class name is shown.
        exc = http.client.BadStatusLine(f"\x1b[31mBearer {TOKEN}\x1b[0m {SECRET}")
        code, out, _, _ = live({"side_effect": exc})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        msg = self.json_error(out, 14)
        self.assertIsNone(WIDE_CONTROL.search(msg), repr(msg))
        self.assertNotIn(TOKEN, msg)
        self.assertNotIn(SECRET, msg)
        code, out, err, _ = live({"side_effect": exc}, json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertNotIn("\x1b", err)
        self.assertNotIn(TOKEN, err)

    def test_deeply_nested_error_body(self):
        err = http_error(500, b"[" * 200000)
        raw, status, text = None, None, None
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=err):
            raw, status, text = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual((raw, status, text), ("", 500, "HTTP 500: Error"))

    def test_deeply_nested_error_body_exit_14_json_and_plain(self):
        code, out, _, _ = live({"side_effect": http_error(500, b"[" * 200000)})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertEqual(self.json_error(out, 14), "Ollama daemon error: HTTP 500: Error")
        code, out, err, _ = live({"side_effect": http_error(500, b"{" + b'"a":{' * 100000)},
                                 json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.plain_error(out, err, 14)

    def test_existing_os_level_errors_keep_their_mapping(self):
        for exc, prefix in ((urllib.error.URLError(ConnectionRefusedError(111, "refused")), "URL error"),
                            (ConnectionResetError("rst"), "OS error"),
                            (http.client.RemoteDisconnected("gone"), "OS error"),
                            (TimeoutError("t"), "OS error")):
            with self.subTest(exc=type(exc).__name__):
                code, out, _, _ = live({"side_effect": exc})
                self.assertEqual(code, mod.EXIT_API_ERROR)
                self.assertIn(prefix, self.json_error(out, 14))

    # -- the same cases through real sockets (http.client does the parsing) --

    def test_loopback_truncated_200_body(self):
        reply = raw_http(200, b"OK", b'{"message": {"cont', clen=1000)
        code, out, _, hits = via_loopback(reply)
        self.assertEqual(hits, 1)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIn("IncompleteRead", self.json_error(out, 14))

    def test_loopback_truncated_200_body_plain_mode(self):
        reply = raw_http(200, b"OK", b'{"message": {"cont', clen=1000)
        code, out, err, _ = via_loopback(reply, json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.plain_error(out, err, 14)

    def test_loopback_truncated_chunked_200(self):
        reply = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                 b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                 b'64\r\n{"message":\r\n')
        code, out, _, hits = via_loopback(reply)
        self.assertEqual(hits, 1)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIn("IncompleteRead", self.json_error(out, 14))

    def test_loopback_not_http_at_all(self):
        reply = b"SSH-2.0-OpenSSH_9.6\r\nProtocol mismatch.\r\n"
        code, out, _, hits = via_loopback(reply)
        self.assertEqual(hits, 1)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIn("BadStatusLine", self.json_error(out, 14))
        code, out, err, _ = via_loopback(reply, json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.plain_error(out, err, 14)

    def test_loopback_200k_open_brackets_error_body(self):
        reply = raw_http(500, b"Internal Server Error", b"[" * 200000)
        code, out, _, hits = via_loopback(reply)
        self.assertEqual(hits, 1)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertEqual(self.json_error(out, 14),
                         "Ollama daemon error: HTTP 500: Internal Server Error")

    def test_loopback_control_a_good_reply_still_works(self):
        body = json.dumps({"message": {"role": "assistant", "content": "hello"},
                           "done": True, "done_reason": "stop"}).encode()
        code, out, _, hits = via_loopback(raw_http(200, b"OK", body))
        self.assertEqual(hits, 1)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["response"], "hello")


# --- R2-5 and R2-6 ----------------------------------------------------------

class HostInputHardening(unittest.TestCase):
    """R2-5 (whitespace, control characters, backslash) and R2-6 (percent)."""

    def assert_bad_host(self, raw, forbidden=()):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                mod.normalize_host(raw)
        self.assertEqual(cm.exception.code, mod.EXIT_BAD_ARGS, repr(raw))
        message = err.getvalue()
        self.assertIn("ERROR [15]", message)
        self.assertIn("OLLAMA_HOST", message)
        for frag in forbidden:
            self.assertNotIn(frag, message, repr(raw))

    def test_whitespace_and_control_characters_anywhere_are_rejected(self):
        cases = [
            "http://127.0.0.1 :11434",
            "http://127.0.0.1 evil.example",
            "127.0.0.1:11434/a b",
            "http://127.0.0.1:11434/a\tb",
            "http://127.0.0.1\r\n:11434",
            "http://127.0.0.1:11434\r\nHost: evil.example",
            "http://127.0.0.1:11434/a\nb",
            "http://127.0.0.1:11434/a\x0bb",
            "http://127.0.0.1:11434/a\x0cb",
            "http://host\u00a0:11434",
            "http://host\u2028x",
            "http://host\u3000x",
            "http://host/\x01",
            "http://host/\x1b[31m",
            "http://host/\x7f",
            "http://ho\x7fst",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assert_bad_host(raw, forbidden=("evil.example",))

    def test_literal_nul_anywhere_is_rejected(self):
        for raw in ("\x00http://host", "http://ho\x00st", "http://host\x00",
                    "http://host/a\x00b", "host:11434\x00", "\x00"):
            with self.subTest(raw=raw):
                self.assert_bad_host(raw)

    def test_backslash_anywhere_is_rejected(self):
        cases = [
            "http://127.0.0.1\\evil.example",
            "http://127.0.0.1:11434/\\@evil.example",
            "http://127.0.0.1:11434\\@evil.example",
            "http:\\\\127.0.0.1",
            "\\\\host",
            "host\\path",
            "http://host/a\\b",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assert_bad_host(raw, forbidden=("evil.example",))

    def test_surrounding_whitespace_is_still_trimmed(self):
        cases = {
            "  http://127.0.0.1:11434/ \n": "http://127.0.0.1:11434",
            "\thost:1\t": "http://host:1",
            "\r\n127.0.0.1:11434\r\n": "http://127.0.0.1:11434",
            "   ": mod.DEFAULT_OLLAMA_HOST,
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.normalize_host(raw), want)

    def test_percent_in_a_host_name_is_rejected(self):
        cases = [
            "http://127.0.0.1%40evil.example",
            "127.0.0.1%40evil.example",
            "http://127.0.0.1%2540evil.example",
            "http://host%00",
            "http://host%2e%2e",
            "http://ho%73t:11434",
            "http://host%20:11434",
            "http://[%25eth0]:11434",
            "http://[evil%40host]:11434",
            "http://[fe80::1%25eth0%40evil]:11434",
            "http://[fe80::1%%eth0]:11434",
            "http://[fe80::1%25eth0!x]:11434",
            "http://[fe80::1%25eth0$]:11434",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assert_bad_host(raw, forbidden=("evil",))

    def test_zone_pattern_accepts_only_rfc6874_style_zone_ids(self):
        # Pins the pattern itself: current Pythons already make urlsplit refuse
        # most malformed bracketed hosts, older ones leave this as the only guard.
        accept = ["fe80::1%25eth0", "fe80::1%eth0", "::1%lo", "fe80::1%2",
                  "fe80::1%br-1a2b", "fe80::a%wg0.1", "fe80::1%25eth0_x~y"]
        reject = ["127.0.0.1%eth0", "%eth0", "fe80::1%", "fe80::1%%eth0",
                  "fe80::1%eth0%40evil", "zz::1%eth0", "host%25eth0",
                  "evil%40host", "fe80::1%eth0!", "fe80::1%eth0/x", "fe80::1 %eth0"]
        for text in accept:
            with self.subTest(accept=text):
                self.assertIsNotNone(mod._IPV6_ZONE_HOST.fullmatch(text))
        for text in reject:
            with self.subTest(reject=text):
                self.assertIsNone(mod._IPV6_ZONE_HOST.fullmatch(text))

    def test_ipv6_zone_id_inside_brackets_is_still_accepted(self):
        cases = {
            "http://[fe80::1%25eth0]:11434": "http://[fe80::1%25eth0]:11434",
            "[fe80::1%25eth0]:11434": "http://[fe80::1%25eth0]:11434",
            "http://[fe80::1%eth0]:11434": "http://[fe80::1%eth0]:11434",
            "http://[fe80::1%25eth0]": "http://[fe80::1%25eth0]",
            "[fe80::1%25eth0]": "http://[fe80::1%25eth0]:11434",
            "http://[fe80::1%25br-1a2b]:11434/ollama/": "http://[fe80::1%25br-1a2b]:11434/ollama",
            "http://[fe80::1%2]:11434": "http://[fe80::1%2]:11434",
            "http://[::1%25eth0]": "http://[::1%25eth0]",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.normalize_host(raw), want)

    def test_percent_outside_the_host_name_is_not_rejected(self):
        self.assertEqual(mod.normalize_host("http://host:11434/a%20b"),
                         "http://host:11434/a%20b")
        self.assertEqual(mod.normalize_host("http://host:11434/ollama%2Fx/"),
                         "http://host:11434/ollama%2Fx")

    def test_plain_hosts_unchanged(self):
        cases = {
            "127.0.0.1:11434": "http://127.0.0.1:11434",
            "http://localhost": "http://localhost",
            "HTTPS://Example.com:8443/": "https://example.com:8443",
            "[::1]:11434": "http://[::1]:11434",
            "0.0.0.0/proxy/": "http://127.0.0.1:11434/proxy",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.normalize_host(raw), want)

    def test_live_call_rejects_each_class_with_exit_15_and_never_dials(self):
        # (A NUL cannot be put into a real environment variable, so the NUL
        # cases above are exercised through normalize_host directly.)
        for raw in ("127.0.0.1 :11434", "http://127.0.0.1\\evil.example",
                    "http://127.0.0.1%40evil.example", "\x01http://host"):
            with self.subTest(raw=raw):
                code, out, _, urlopen = live(
                    {"side_effect": RuntimeError("must not be called")},
                    extra_env={"OLLAMA_HOST": raw})
                self.assertEqual(code, mod.EXIT_BAD_ARGS)
                payload = json.loads(out)
                self.assertEqual((payload["status"], payload["exit_code"]), ("error", 15))
                self.assertIn("OLLAMA_HOST", payload["error"])
                self.assertNotIn("evil.example", payload["error"])
                urlopen.assert_not_called()


# --- R2-7 -------------------------------------------------------------------

@unittest.skipIf(os.name == "nt", "the fake ollama is a POSIX shell script")
class NoSpawnBeforeHostCheck(unittest.TestCase):
    """R2-7: `ollama list` inherits OLLAMA_HOST, so validate it before spawning."""

    HOSTILE = [
        "http://127.0.0.1:11434@evil.example",
        "ftp://host",
        "user:pw@host",
        "127.0.0.1 :11434",
        "http://127.0.0.1\\evil.example",
        "http://127.0.0.1%40evil.example",
    ]
    GOOD_HOST = "127.0.0.1:9"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r3-shim-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.log")
        shim = os.path.join(self.tmp, "ollama")
        with open(shim, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\n'
                    'printf "spawned\\n" >> "%s"\n'
                    'printf "NAME ID SIZE MODIFIED\\n"\n'
                    'exit 0\n' % self.log)
        os.chmod(shim, 0o755)

    def spawns(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return len(f.read().splitlines())
        except FileNotFoundError:
            return 0

    def run_mode(self, mode, host):
        env = {"OLLAMA_HOST": host, "PATH": self.tmp}
        if mode == "discover":
            return run_cli(["--discover", "--json"], env=env) + (None,)
        argv = DELEGATE + ["--json"] + (["--dry-run"] if mode == "dry-run" else [])
        ok = FakeResp(json.dumps({"message": {"role": "assistant", "content": "hi"},
                                  "done": True}).encode())
        with mock.patch.object(mod.urllib.request, "urlopen", return_value=ok) as urlopen:
            return run_cli(argv, env=env) + (urlopen,)

    def test_control_the_shim_is_spawned_for_a_good_host(self):
        for mode in ("discover", "dry-run", "live"):
            with self.subTest(mode=mode):
                before = self.spawns()
                code, _, _, _ = self.run_mode(mode, self.GOOD_HOST)
                self.assertEqual(code, 0)
                self.assertEqual(self.spawns(), before + 1)

    def test_rejected_host_spawns_nothing_in_every_mode(self):
        for mode in ("discover", "live", "dry-run"):
            for host in self.HOSTILE:
                with self.subTest(mode=mode, host=host):
                    before = self.spawns()
                    code, out, _, urlopen = self.run_mode(mode, host)
                    self.assertEqual(code, mod.EXIT_BAD_ARGS)
                    self.assertEqual(json.loads(out)["exit_code"], 15)
                    self.assertEqual(self.spawns(), before, "ollama was spawned")
                    if urlopen is not None:
                        urlopen.assert_not_called()


# --- R2-8 -------------------------------------------------------------------

CHILD = r'''
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("ocd_child", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
unit = "-----BEGIN PRIVATE KEY-----"
text = (unit * (1048576 // len(unit) + 1))[:1048576]
t0 = time.process_time()
out = m.clean_daemon_text(text)
print("%.3f %d" % (time.process_time() - t0, len(out)))
'''


class ReadSpy(io.BytesIO):
    """Records the size argument of every read()."""

    def __init__(self, data):
        super().__init__(data)
        self.sizes = []

    def read(self, size=-1):
        self.sizes.append(size)
        return super().read(size)


class BoundedDaemonText(unittest.TestCase):
    """R2-8: untrusted daemon text is cut to 64 KiB before the (quadratic) redactor."""

    LIMIT = 65536

    def test_redact_never_sees_more_than_64_kib(self):
        seen = []

        def spy(text):
            seen.append(len(text))
            return text, 0

        with mock.patch.object(mod, "redact", side_effect=spy):
            out = mod.clean_daemon_text("-----BEGIN PRIVATE KEY-----" * 40000)
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), self.LIMIT)
        self.assertEqual(len(out), 300)

    def test_cut_is_per_call_and_short_text_is_untouched(self):
        self.assertEqual(mod.clean_daemon_text("short \x1b text"), "short   text")
        self.assertEqual(len(mod.clean_daemon_text("A" * (self.LIMIT + 5000))), 300)

    def test_error_body_read_is_bounded(self):
        fp = ReadSpy(json.dumps({"error": "boom"}).encode())
        err = urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", 500, "Error", {}, fp)
        _OPENED.append(err)
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=err):
            _, status, text = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual((status, text), (500, "HTTP 500: Error: boom"))
        self.assertTrue(fp.sizes, "error body was never read")
        for size in fp.sizes:
            self.assertTrue(0 < size <= self.LIMIT, f"unbounded read({size})")

    def test_one_megabyte_error_body_keeps_the_status_line_only(self):
        # redact is stubbed so that this test also fails fast (not after minutes)
        # against code that has no bound; the real timing check is the child
        # process test below.
        seen = []

        def spy(text):
            seen.append(len(text))
            return text, 0

        body = json.dumps({"error": "-----BEGIN PRIVATE KEY-----" * 40000}).encode()
        self.assertGreater(len(body), 1000000)
        fp = ReadSpy(body)
        err = urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", 500, "Error", {}, fp)
        _OPENED.append(err)
        with mock.patch.object(mod, "redact", side_effect=spy), \
                mock.patch.object(mod.urllib.request, "urlopen", side_effect=err):
            _, status, text = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertLessEqual(fp.tell(), self.LIMIT, "read more than 64 KiB of the body")
        self.assertLessEqual(max(seen or [0]), self.LIMIT)
        # the cut-off JSON is unparsable: the status line survives, the detail is dropped
        self.assertEqual((status, text), (500, "HTTP 500: Error"))

    def test_one_megabyte_pem_header_repeat_finishes_under_two_seconds(self):
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", CHILD, SCRIPT],
                capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("clean_daemon_text on a 1 MB '-----BEGIN PRIVATE KEY-----' "
                      "repeat did not finish within 20 s")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cpu_seconds, length = proc.stdout.split()
        self.assertLess(float(cpu_seconds), 2.0)
        self.assertEqual(int(length), 300)


# --- R2-9 -------------------------------------------------------------------

def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


CLOUD_MODEL = re.compile(r"`([a-z0-9][a-z0-9.\-]*:[a-z0-9.\-]*cloud)`")


def table_rows(text, heading):
    """Data rows (lists of cells) of the first table after `heading`."""
    start = text.index(heading)
    rows = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not set("".join(cells)) <= set("-: "):
                rows.append(cells)
        elif rows:
            break
    return rows[1:]  # drop the header row


class DocRoutingExact(unittest.TestCase):
    """R2-9: the docs list exactly the models, in the order, that ROUTING_MAP routes."""

    ROUTING_ROWS = {
        "Deep debugging": "deep-debugging",
        "Security review": "security-review",
        "Performance analysis": "performance-analysis",
        "Architecture review": "architecture-review",
        "Frontend/UI review": "frontend-ui-review",
        "Test-plan generation": "test-plan-generation",
        "Log/error clustering": "log-error-clustering",
        "Documentation summarization": "documentation-summarization",
    }
    SKILL_ROWS = {
        "Deep debugging": ["deep-debugging"],
        "Code-heavy architecture review": ["architecture-review"],
        "Long-context reasoning": ["log-error-clustering"],
        "General reasoning": ["security-review", "performance-analysis"],
        "Fast summarization": ["documentation-summarization"],
        "Frontend/UI review": ["frontend-ui-review"],
        "Test-plan generation": ["test-plan-generation"],
    }

    def all_code_models(self):
        return {m for models in mod.ROUTING_MAP.values() for m in models}

    def test_model_routing_rows_match_the_map_exactly(self):
        rows = table_rows(_read("references", "model-routing.md"), "## Routing table")
        seen = set()
        for cells in rows:
            task = next(t for prefix, t in self.ROUTING_ROWS.items()
                        if cells[0].startswith(prefix))
            seen.add(task)
            self.assertEqual(CLOUD_MODEL.findall(cells[1]), mod.ROUTING_MAP[task], task)
        self.assertEqual(seen, set(mod.ROUTING_MAP))

    def test_skill_md_rows_match_the_map_exactly(self):
        rows = table_rows(_read("SKILL.md"), "## Model selection preferences")
        self.assertEqual(len(rows), len(self.SKILL_ROWS))
        for cells in rows:
            tasks = next(t for prefix, t in self.SKILL_ROWS.items()
                         if cells[0].startswith(prefix))
            want = [m for task in tasks for m in mod.ROUTING_MAP[task]]
            self.assertEqual(CLOUD_MODEL.findall(cells[1]), want, cells[0])

    def test_docs_name_no_model_the_map_never_routes(self):
        known = self.all_code_models()
        for name, heading in (("SKILL.md", "## Model selection preferences"),
                              (os.path.join("references", "model-routing.md"),
                               "## Routing table")):
            for cells in table_rows(_read(*name.split(os.sep)), heading):
                extra = [m for m in CLOUD_MODEL.findall(cells[1]) if m not in known]
                self.assertEqual(extra, [], f"{name}: {cells[0]}")

    def test_every_routed_model_is_documented_in_model_routing_md(self):
        text = _read("references", "model-routing.md")
        missing = [m for m in sorted(self.all_code_models()) if f"`{m}`" not in text]
        self.assertEqual(missing, [])

    def test_the_two_models_the_review_named_are_not_documented_as_routed(self):
        for name in ("SKILL.md", os.path.join("references", "model-routing.md")):
            text = _read(*name.split(os.sep))
            self.assertNotIn("ministral", text, name)
        skill_rows = table_rows(_read("SKILL.md"), "## Model selection preferences")
        arch = next(c for c in skill_rows if c[0].startswith("Code-heavy architecture"))
        self.assertNotIn("qwen3-coder-next:cloud", arch[1])

    def test_routing_map_comment_no_longer_excuses_doc_drift(self):
        text = _read("scripts", "ollama_cloud_delegate.py")
        self.assertNotIn("extra tail models", text)
        self.assertNotIn("Routing map aligned with", text)
        self.assertIn("list exactly these models", text)


if __name__ == "__main__":
    unittest.main()
