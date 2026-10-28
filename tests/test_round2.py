"""Round-2 regression tests for scripts/ollama_cloud_delegate.py and docs.

stdlib unittest only. No network, no real subprocess: urllib and subprocess
are mocked. Separate from tests/test_delegate.py, which stays untouched. Run
from the repo root:

    python3 -I -m unittest discover -s tests -v

Covers: truncated HTTP error bodies (F2), daemon error text hygiene (F3),
normalize_host hardening (F4) and doc/code drift (F7).
"""

import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
SCRIPT = os.path.join(ROOT, "scripts", "ollama_cloud_delegate.py")

_spec = importlib.util.spec_from_file_location("ollama_cloud_delegate_r2", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

DEEPSEEK = "deepseek-v4.1-flash:cloud"
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123"
DELEGATE = ["--task", "deep-debugging", "--role", "debugger",
            "--model", DEEPSEEK, "--prompt", "x"]


def run_cli(argv, env=None):
    """Run mod.main(argv) the way the `__main__` block does.

    Returns (exit_code, stdout, stderr). Unexpected exceptions map to exit 16
    exactly like the script's own entry point does.
    """
    out, err = io.StringIO(), io.StringIO()
    env = dict(env or {})
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


class TruncatedBody(io.BytesIO):
    def read(self, *args, **kwargs):
        raise http.client.IncompleteRead(b'{"error": "par', 100)


def truncated_http_error(code=500):
    err = urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat", code, "Internal Server Error", {},
        TruncatedBody())
    _OPENED.append(err)
    return err


def live(side_effect, extra_env=None, json_out=True):
    """Run a live (non dry-run) delegation with urlopen mocked."""
    argv = DELEGATE + (["--json"] if json_out else [])
    with mock.patch.object(mod, "discover_cloud_models",
                           return_value=([DEEPSEEK], [], [])), \
            mock.patch.object(mod.urllib.request, "urlopen",
                              side_effect=side_effect) as urlopen:
        res = run_cli(argv, env=extra_env)
    return res + (urlopen,)


class TruncatedErrorBody(unittest.TestCase):
    """F2: IncompleteRead is an HTTPException, not an OSError."""

    def test_call_ollama_returns_status_without_detail(self):
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=truncated_http_error(500)):
            raw, status, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual((raw, status), ("", 500))
        self.assertTrue(err.startswith("HTTP 500: Internal Server Error"), err)

    def test_truncated_error_body_is_exit_14_with_json(self):
        code, out, _, _ = live(truncated_http_error(500))
        self.assertEqual(code, mod.EXIT_API_ERROR)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["exit_code"], mod.EXIT_API_ERROR)
        self.assertIn("HTTP 500", payload["error"])

    def test_truncated_error_body_plain_mode_is_exit_14(self):
        code, out, err, _ = live(truncated_http_error(502), json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertEqual(out, "")
        self.assertIn("ERROR [14]", err)

    def test_truncated_body_on_401_stays_auth_required(self):
        code, out, _, _ = live(truncated_http_error(401))
        self.assertEqual(code, mod.EXIT_AUTH_REQUIRED)
        self.assertEqual(json.loads(out)["exit_code"], mod.EXIT_AUTH_REQUIRED)


class DaemonErrorText(unittest.TestCase):
    """F3: daemon-supplied error text is redacted, sanitised and capped."""

    PREFIX = "HTTP 500: Error: "

    def err_for(self, text):
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=http_error(500, {"error": text})):
            _, status, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual(status, 500)
        self.assertTrue(err.startswith(self.PREFIX), err)
        return err[len(self.PREFIX):]

    def test_secret_redacted(self):
        detail = self.err_for(f"upstream said {SECRET} rejected")
        self.assertNotIn(SECRET, detail)
        self.assertNotIn("sk-abc", detail)
        self.assertIn("REDACTED", detail)

    def test_other_secret_shapes_redacted(self):
        detail = self.err_for("Bearer abcDEF123.tok and password=hunter2")
        self.assertNotIn("abcDEF123", detail)
        self.assertNotIn("hunter2", detail)

    def test_control_characters_become_spaces(self):
        detail = self.err_for("a\x1b[31mred\x1b[0m\x00b\x7fc\nd\te\rf")
        self.assertIsNone(CONTROL.search(detail), repr(detail))
        self.assertNotIn("\x1b", detail)
        self.assertEqual(detail, "a [31mred [0m b c d e f")

    def test_secret_and_escape_together_end_to_end(self):
        body = {"error": f"boom {SECRET} \x1b[31mRED\x1b[0m"}
        code, out, _, _ = live(http_error(500, body))
        self.assertEqual(code, mod.EXIT_API_ERROR)
        msg = json.loads(out)["error"]
        self.assertNotIn(SECRET, msg)
        self.assertIn("REDACTED", msg)
        self.assertIsNone(CONTROL.search(msg), repr(msg))

    def test_secret_and_escape_plain_mode_stderr(self):
        body = {"error": f"boom {SECRET} \x1b[31mRED\x1b[0m"}
        code, _, err, _ = live(http_error(500, body), json_out=False)
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertNotIn(SECRET, err)
        self.assertNotIn("\x1b", err)

    def test_auth_error_text_also_clean(self):
        body = {"error": f"unauthorized {SECRET} \x1b[31m", "signin_url": "https://x/?k=S3CR3T"}
        code, out, _, _ = live(http_error(401, body))
        self.assertEqual(code, mod.EXIT_AUTH_REQUIRED)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("S3CR3T", out)
        self.assertIsNone(CONTROL.search(json.loads(out)["error"]))

    def test_capped_at_300_chars(self):
        self.assertEqual(len(self.err_for("A" * 1000)), 300)

    def test_secret_straddling_the_cap_is_redacted_before_truncation(self):
        # Redact the full text first; truncating first would leave a
        # 10-char prefix of the key that no redaction pattern matches.
        detail = self.err_for("A" * 290 + SECRET)
        self.assertNotIn("sk-", detail)
        self.assertLessEqual(len(detail), 300)

    def test_non_string_error_is_ignored(self):
        with mock.patch.object(mod.urllib.request, "urlopen",
                               side_effect=http_error(500, {"error": {"x": 1}})):
            _, _, err = mod.call_ollama(DEEPSEEK, "s", "u", "127.0.0.1:11434")
        self.assertEqual(err, "HTTP 500: Error")


class NormalizeHostHardening(unittest.TestCase):
    """F4: fail closed on userinfo / non-http(s); keep path; port default."""

    def assert_bad_host(self, raw, forbidden=()):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                mod.normalize_host(raw)
        self.assertEqual(cm.exception.code, mod.EXIT_BAD_ARGS, raw)
        message = err.getvalue()
        self.assertIn("ERROR [15]", message)
        self.assertIn("OLLAMA_HOST", message)
        for frag in forbidden:
            self.assertNotIn(frag, message, raw)

    def test_userinfo_is_rejected(self):
        cases = [
            ("http://127.0.0.1:11434@evil.example", ("evil.example",)),
            ("127.0.0.1:11434@evil.example", ("evil.example",)),
            ("http://user:s3cretpw@host:11434", ("s3cretpw",)),
            ("user:s3cretpw@host", ("s3cretpw",)),
            ("https://user@host", ("user@",)),
            ("http://@host:11434", ()),
        ]
        for raw, forbidden in cases:
            with self.subTest(raw=raw):
                self.assert_bad_host(raw, forbidden)

    def test_non_http_schemes_are_rejected(self):
        for raw in ("ftp://host", "file:///etc/passwd", "ssh://host:22",
                    "FTP://host:21", "ws://host:11434", "gopher://host"):
            with self.subTest(raw=raw):
                self.assert_bad_host(raw)

    def test_malformed_hosts_are_rejected(self):
        for raw in ("http://[::1", "http://host:99999", "http://host:abc"):
            with self.subTest(raw=raw):
                self.assert_bad_host(raw)

    def test_path_is_kept_without_trailing_slash(self):
        cases = {
            "http://host:11434/ollama/": "http://host:11434/ollama",
            "http://host:11434/ollama": "http://host:11434/ollama",
            "host:11434/ollama/": "http://host:11434/ollama",
            "https://h.example/a/b//": "https://h.example/a/b",
            "https://h.example/a/b": "https://h.example/a/b",
            "0.0.0.0:11434/proxy/": "http://127.0.0.1:11434/proxy",
            "http://host:11434/": "http://host:11434",
            "http://host:11434//": "http://host:11434",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.normalize_host(raw), want)

    def test_default_port_only_when_no_scheme_given(self):
        cases = {
            "host": "http://host:11434",
            "localhost": "http://localhost:11434",
            ":11434": "http://127.0.0.1:11434",
            "0.0.0.0": "http://127.0.0.1:11434",
            "[::]": "http://[::1]:11434",
            "host/ollama": "http://host:11434/ollama",
            # explicit scheme, no port: no port is invented (http means 80)
            "http://host": "http://host",
            "HTTP://Host": "http://host",
            "https://host": "https://host",
            "http://0.0.0.0": "http://127.0.0.1",
            "http://[::]": "http://[::1]",
            "http://host/ollama": "http://host/ollama",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.normalize_host(raw), want)

    def test_wildcard_whitespace_and_slash_handling_kept(self):
        cases = {
            "": mod.DEFAULT_OLLAMA_HOST,
            "   ": mod.DEFAULT_OLLAMA_HOST,
            "  http://127.0.0.1:11434/ \n": "http://127.0.0.1:11434",
            "0.0.0.0:11434": "http://127.0.0.1:11434",
            "http://0.0.0.0:11434/": "http://127.0.0.1:11434",
            "[::]:11434": "http://[::1]:11434",
            "http://[::]:11434/": "http://[::1]:11434",
            "http://[::1]:11434": "http://[::1]:11434",
            "HTTP://Example.com:8080": "http://example.com:8080",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.normalize_host(raw), want)

    def test_call_ollama_url_keeps_path_prefix(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            raise urllib.error.URLError("stop")

        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake):
            mod.call_ollama(DEEPSEEK, "s", "u", "http://h.example:8080/ollama/")
        self.assertEqual(seen["url"], "http://h.example:8080/ollama/api/chat")

    def test_live_call_with_userinfo_host_exits_15_and_never_dials(self):
        code, out, _, urlopen = live(
            RuntimeError("must not be called"),
            extra_env={"OLLAMA_HOST": "http://127.0.0.1:11434@evil.example"})
        self.assertEqual(code, mod.EXIT_BAD_ARGS)
        payload = json.loads(out)
        self.assertEqual((payload["status"], payload["exit_code"]), ("error", 15))
        self.assertIn("OLLAMA_HOST", payload["error"])
        self.assertNotIn("evil.example", payload["error"])
        urlopen.assert_not_called()

    def test_live_call_with_scheme_host_exits_15_plain_stderr(self):
        code, out, err, urlopen = live(
            RuntimeError("must not be called"),
            extra_env={"OLLAMA_HOST": "ftp://host"}, json_out=False)
        self.assertEqual(code, mod.EXIT_BAD_ARGS)
        self.assertEqual(out, "")
        self.assertIn("ERROR [15]", err)
        urlopen.assert_not_called()

    def test_discover_with_bad_host_exits_15(self):
        with mock.patch.object(mod, "discover_cloud_models",
                               return_value=([DEEPSEEK], [], [])):
            code, out, _ = run_cli(["--discover", "--json"],
                                   env={"OLLAMA_HOST": "user:pw@host"})
        self.assertEqual(code, mod.EXIT_BAD_ARGS)
        self.assertEqual(json.loads(out)["exit_code"], 15)

    def test_discover_reports_normalised_host_with_path(self):
        with mock.patch.object(mod, "discover_cloud_models",
                               return_value=([DEEPSEEK], [], [])):
            code, out, _ = run_cli(["--discover", "--json"],
                                   env={"OLLAMA_HOST": "0.0.0.0/ollama/"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ollama_host"],
                         "http://127.0.0.1:11434/ollama")

    def test_dry_run_does_not_need_ollama_host(self):
        with mock.patch.object(mod, "discover_cloud_models",
                               return_value=([DEEPSEEK], [], [])):
            code, _, _ = run_cli(DELEGATE + ["--dry-run", "--json"],
                                 env={"OLLAMA_HOST": "127.0.0.1:9"})
        self.assertEqual(code, 0)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


CLOUD_MODEL = re.compile(r"`([a-z0-9][a-z0-9.\-]*:[a-z0-9.\-]*cloud)`")


def table_rows(text, heading):
    """Return the data rows (list of cell lists) of the first table after heading."""
    start = text.index(heading)
    rows = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not set("".join(cells)) <= set("-: "):
                rows.append(cells)
        elif rows:
            break
    return rows[1:]  # drop header row


def is_subsequence(needle, haystack):
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


class DocDrift(unittest.TestCase):
    """F7: docs must say what the code does."""

    ROW_TASK = {
        "Deep debugging": "deep-debugging",
        "Security review": "security-review",
        "Performance analysis": "performance-analysis",
        "Architecture review": "architecture-review",
        "Frontend/UI review": "frontend-ui-review",
        "Test-plan generation": "test-plan-generation",
        "Log/error clustering": "log-error-clustering",
        "Documentation summarization": "documentation-summarization",
    }

    def test_model_routing_table_ends_with_deepseek_flash_and_matches_code(self):
        rows = table_rows(_read("references", "model-routing.md"), "## Routing table")
        self.assertEqual(len(rows), len(mod.ROUTING_MAP))
        seen = set()
        for cells in rows:
            task = next(t for prefix, t in self.ROW_TASK.items()
                        if cells[0].startswith(prefix))
            seen.add(task)
            models = CLOUD_MODEL.findall(cells[1])
            self.assertEqual(models[-1], DEEPSEEK, task)
            self.assertTrue(is_subsequence(mod.ROUTING_MAP[task], models),
                            f"{task}: code {mod.ROUTING_MAP[task]} vs docs {models}")
        self.assertEqual(seen, set(mod.ROUTING_MAP))

    def test_skill_md_table_ends_with_deepseek_flash(self):
        rows = table_rows(_read("SKILL.md"), "## Model selection preferences")
        self.assertEqual(len(rows), 7)
        for cells in rows:
            models = CLOUD_MODEL.findall(cells[1])
            self.assertEqual(models[-1], DEEPSEEK, cells[0])

    def test_explicit_model_rule_is_not_discovered_only(self):
        rule = next(line for line in _read("references", "model-routing.md").splitlines()
                    if line.startswith("1. If `--model <explicit>`"))
        self.assertNotIn("and discovered", rule)
        self.assertIn("pull", rule)
        self.assertIn("12", rule)

    def test_exit_14_row_mentions_empty_response(self):
        row = next(line for line in _read("SKILL.md").splitlines()
                   if line.startswith("| 14 |"))
        self.assertIn("empty response", row)

    def test_isolated_mode_claim_is_accurate(self):
        for name in ("SKILL.md", "README.md"):
            text = _read(name)
            self.assertNotIn("in the working directory is never imported", text, name)
            self.assertNotIn("ignores `PYTHONPATH` and the current directory", text, name)
            line = next(l for l in text.splitlines() if "`-I`" in l and "isolated" in l)
            for needle in ("PYTHONPATH", "site-packages", "sys.path"):
                self.assertIn(needle, line, name)

    def test_code_comment_does_not_claim_exact_alignment(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("Routing map aligned with", text)


if __name__ == "__main__":
    unittest.main()
