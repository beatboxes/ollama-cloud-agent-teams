"""Unit tests for scripts/ollama_cloud_delegate.py.

stdlib unittest only. No network, no real subprocess: urllib and subprocess
are mocked. Run from the repo root:

    python3 -I -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "ollama_cloud_delegate.py")

_spec = importlib.util.spec_from_file_location("ollama_cloud_delegate", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

DEEPSEEK = "deepseek-v4.1-flash:cloud"


def run_main(argv, env=None):
    """Run mod.main(argv); return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env = dict(env or {})
    with mock.patch.dict(os.environ, env, clear=False), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        os.environ.pop("OLLAMA_CLOUD_MODELS", None)
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def http_error(code, body):
    return urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat",
        code,
        "Not Found" if code == 404 else "Error",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


class FakeResp:
    status = 200

    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Routing(unittest.TestCase):
    def test_deepseek_flash_in_every_routing_list(self):
        for task, models in mod.ROUTING_MAP.items():
            self.assertIn(DEEPSEEK, models, task)

    def test_self_test_covers_deepseek_flash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.cmd_self_test(None)
        self.assertEqual(rc, 0)
        self.assertIn(f"accept {DEEPSEEK}: PASS", out.getvalue())
        self.assertIn(f"-> {DEEPSEEK}: PASS", out.getvalue())

    def test_auto_prefers_routing_entry_when_discovered(self):
        # deepseek-v4.1-flash is routed, so the reason is the routing map,
        # not the "first discovered" fallback.
        model, reason = mod.select_model("log-error-clustering", "auto", [DEEPSEEK])
        self.assertEqual(model, DEEPSEEK)
        self.assertIn("routing map", reason)


class CloudTag(unittest.TestCase):
    def test_cloud_tag(self):
        self.assertTrue(mod.is_cloud_tagged(DEEPSEEK))
        self.assertFalse(mod.is_cloud_tagged("llama3:8b"))
        self.assertFalse(mod.is_cloud_tagged("deepseek-v4.1-flash"))

    def test_noncloud_explicit_still_exit_10(self):
        with mock.patch.object(mod, "discover_cloud_models", return_value=([], [], [])):
            code, _, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--model", "llama3:8b", "--prompt", "x", "--dry-run"])
        self.assertEqual(code, mod.EXIT_CLOUD_ONLY_VIOLATION)

    def test_auto_with_nothing_discovered_still_exit_12(self):
        with mock.patch.object(mod, "discover_cloud_models", return_value=([], [], [])):
            code, _, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--prompt", "x", "--dry-run"])
        self.assertEqual(code, mod.EXIT_NO_CLOUD_MODELS)


class ExplicitUnpulled(unittest.TestCase):
    def test_select_model_accepts_explicit_cloud_model_not_listed(self):
        model, reason = mod.select_model("deep-debugging", DEEPSEEK, [])
        self.assertEqual(model, DEEPSEEK)
        self.assertIn("not in", reason)

    def test_dry_run_explicit_unpulled_model_ok(self):
        with mock.patch.object(mod, "discover_cloud_models", return_value=([], [], [])):
            code, out, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--model", DEEPSEEK, "--prompt", "x", "--dry-run", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["model"], DEEPSEEK)

    def test_daemon_404_model_not_found_maps_to_pull_message_exit_12(self):
        err = http_error(404, {"error": f"model '{DEEPSEEK[:-6]}' not found"})
        with mock.patch.object(mod, "discover_cloud_models", return_value=([], [], [])), \
                mock.patch.object(mod.urllib.request, "urlopen", side_effect=err):
            code, out, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--model", DEEPSEEK, "--prompt", "x", "--json"])
        self.assertEqual(code, mod.EXIT_NO_CLOUD_MODELS)
        msg = json.loads(out)["error"]
        self.assertIn(f"ollama pull {DEEPSEEK}", msg)

    def test_other_404_stays_api_error(self):
        err = http_error(404, {"error": "page not found"})
        with mock.patch.object(mod, "discover_cloud_models", return_value=([DEEPSEEK], [], [])), \
                mock.patch.object(mod.urllib.request, "urlopen", side_effect=err):
            code, _, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--model", DEEPSEEK, "--prompt", "x", "--json"])
        self.assertEqual(code, mod.EXIT_API_ERROR)

    def test_401_stays_auth_required_and_omits_signin_url(self):
        err = http_error(401, {"error": "unauthorized",
                               "signin_url": "https://ollama.com/connect?key=SECRETISH"})
        with mock.patch.object(mod, "discover_cloud_models", return_value=([DEEPSEEK], [], [])), \
                mock.patch.object(mod.urllib.request, "urlopen", side_effect=err):
            code, out, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--model", DEEPSEEK, "--prompt", "x", "--json"])
        self.assertEqual(code, mod.EXIT_AUTH_REQUIRED)
        self.assertNotIn("SECRETISH", out)


class Discover(unittest.TestCase):
    LIST_OUT = (
        "NAME                         ID              SIZE    MODIFIED\n"
        "deepseek-v4.1-flash:cloud    e04da138d31e    -       1 minute ago\n"
        "llama3:8b                    aaaaaaaaaaaa    4.7 GB  2 days ago\n"
    )

    def test_parses_ollama_list(self):
        cp = mock.Mock(returncode=0, stdout=self.LIST_OUT)
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(mod.subprocess, "run", return_value=cp):
            os.environ.pop("OLLAMA_CLOUD_MODELS", None)
            acc, renv, rcli = mod.discover_cloud_models()
        self.assertEqual(acc, [DEEPSEEK])
        self.assertEqual(rcli, ["llama3:8b"])

    def test_cmd_discover_reports_normalised_host(self):
        with mock.patch.object(mod, "discover_cloud_models", return_value=([DEEPSEEK], [], [])):
            code, out, _ = run_main(["--discover", "--json"],
                                    env={"OLLAMA_HOST": "0.0.0.0:11434/"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ollama_host"], "http://127.0.0.1:11434")


class ExtractResponse(unittest.TestCase):
    def body(self, **msg):
        return json.dumps({"message": dict(role="assistant", **msg), "done": True})

    def test_content_preferred(self):
        self.assertEqual(
            mod.extract_response_text(self.body(content="391", thinking="obey")), "391")

    def test_thinking_used_when_content_empty(self):
        self.assertEqual(
            mod.extract_response_text(self.body(content="", thinking="the answer")),
            "the answer")

    def test_thinking_used_when_content_whitespace(self):
        self.assertEqual(
            mod.extract_response_text(self.body(content="  \n", thinking="t")), "t")

    def test_garbage_is_empty(self):
        for raw in ("", "not json", "[]", "null", '{"message": null}',
                    '{"message": "x"}', '{"message": {"content": null}}'):
            self.assertEqual(mod.extract_response_text(raw), "", raw)

    def _delegate(self, resp_body):
        with mock.patch.object(mod, "discover_cloud_models", return_value=([DEEPSEEK], [], [])), \
                mock.patch.object(mod.urllib.request, "urlopen",
                                  return_value=FakeResp(resp_body)):
            return run_main(
                ["--task", "deep-debugging", "--role", "debugger",
                 "--model", DEEPSEEK, "--prompt", "x", "--json"])

    def test_delegate_thinking_only_response(self):
        code, out, _ = self._delegate(
            {"message": {"role": "assistant", "content": "", "thinking": "391"},
             "done": True, "done_reason": "length"})
        self.assertEqual(code, 0)
        p = json.loads(out)
        self.assertEqual(p["response"], "391")
        self.assertEqual(p["response_source"], "thinking")
        self.assertEqual(p["done_reason"], "length")

    def test_delegate_content_response(self):
        code, out, _ = self._delegate(
            {"message": {"role": "assistant", "content": "391", "thinking": "t"},
             "done": True, "done_reason": "stop"})
        p = json.loads(out)
        self.assertEqual((code, p["response"], p["response_source"]), (0, "391", "content"))

    def test_delegate_empty_response_is_api_error(self):
        code, out, _ = self._delegate(
            {"message": {"role": "assistant", "content": ""}, "done": True})
        self.assertEqual(code, mod.EXIT_API_ERROR)
        self.assertIn("empty", json.loads(out)["error"])


class NormalizeHost(unittest.TestCase):
    def test_cases(self):
        cases = {
            "": "http://localhost:11434",
            "127.0.0.1:11434": "http://127.0.0.1:11434",
            "http://127.0.0.1:11434": "http://127.0.0.1:11434",
            "http://127.0.0.1:11434/": "http://127.0.0.1:11434",
            "127.0.0.1:11434/": "http://127.0.0.1:11434",
            "  127.0.0.1:11434 \n": "http://127.0.0.1:11434",
            "0.0.0.0:11434": "http://127.0.0.1:11434",
            "http://0.0.0.0:11434/": "http://127.0.0.1:11434",
            "0.0.0.0": "http://127.0.0.1:11434",
            ":11434": "http://127.0.0.1:11434",
            "localhost": "http://localhost:11434",
            "[::]:11434": "http://[::1]:11434",
            "HTTP://Example.com:8080": "http://example.com:8080",
            "https://ollama.example.com": "https://ollama.example.com",
            "https://ollama.example.com/": "https://ollama.example.com",
        }
        for raw, want in cases.items():
            self.assertEqual(mod.normalize_host(raw), want, repr(raw))

    def test_call_ollama_url_from_bare_host(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            return FakeResp({"message": {"content": "ok"}})

        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake):
            mod.call_ollama(DEEPSEEK, "s", "u", "0.0.0.0:11434/")
        self.assertEqual(seen["url"], "http://127.0.0.1:11434/api/chat")


class Redaction(unittest.TestCase):
    SECRETS = [
        "Bearer abcDEF123.tok",
        "api_key=fakekey123",
        "OP_SESSION_acct=fakevalue",
        "password=hunter2",
        "mongodb+srv://u:p@cluster.example.net/db",
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
    ]

    def test_redact_function(self):
        text, n = mod.redact(" ".join(self.SECRETS))
        for frag in ("abcDEF123", "fakekey123", "fakevalue", "hunter2", "u:p@", "AAAA"):
            self.assertNotIn(frag, text)
        self.assertGreaterEqual(n, len(self.SECRETS))

    def test_redaction_fires_before_live_send(self):
        sent = {}

        def fake(req, timeout=None):
            sent["body"] = req.data.decode()
            return FakeResp({"message": {"content": "ok password=leakedback"}})

        with mock.patch.object(mod, "discover_cloud_models", return_value=([DEEPSEEK], [], [])), \
                mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake):
            code, out, _ = run_main(
                ["--task", "deep-debugging", "--role", "debugger", "--model", DEEPSEEK,
                 "--prompt", "see api_key=fakekey123 and Bearer abcDEF123.tok", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("fakekey123", sent["body"])
        self.assertNotIn("abcDEF123", sent["body"])
        self.assertIn("REDACTED", sent["body"])
        # response safety net still redacts
        self.assertNotIn("leakedback", json.loads(out)["response"])
        self.assertGreaterEqual(json.loads(out)["redaction_count"], 3)

    def test_refused_context_file_exit_13(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as f:
                f.write("A=1\n")
            with mock.patch.object(mod, "discover_cloud_models", return_value=([DEEPSEEK], [], [])):
                code, _, _ = run_main(
                    ["--task", "deep-debugging", "--role", "debugger", "--model", DEEPSEEK,
                     "--prompt", "x", "--context-file", p])
        self.assertEqual(code, mod.EXIT_REDACTION_REFUSAL)


class BadArgs(unittest.TestCase):
    def test_missing_args_json_is_json_on_stdout(self):
        code, out, _ = run_main(["--json", "--task", "deep-debugging"])
        self.assertEqual(code, mod.EXIT_BAD_ARGS)
        self.assertEqual(json.loads(out)["exit_code"], mod.EXIT_BAD_ARGS)

    def test_missing_args_plain_is_stderr(self):
        code, out, err = run_main(["--task", "deep-debugging"])
        self.assertEqual(code, mod.EXIT_BAD_ARGS)
        self.assertEqual(out, "")
        self.assertIn("ERROR [15]", err)


class Docs(unittest.TestCase):
    def test_skill_and_readme_use_python3_on_linux(self):
        root = os.path.join(HERE, "..")
        for name in ("SKILL.md", "README.md"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("python3 -I", text, name)
            self.assertIn(DEEPSEEK, text, name)
            self.assertNotIn("python .\\scripts", text, name)
            self.assertNotIn("python ~/", text, name)


if __name__ == "__main__":
    unittest.main()
