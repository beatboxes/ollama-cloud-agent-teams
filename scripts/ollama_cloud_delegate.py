#!/usr/bin/env python3
"""Ollama Cloud Agent Teams delegate script.

Cloud-only. Delegates read-only analysis tasks to Ollama Cloud models via the
local Ollama daemon HTTP API. Never falls back to local models. Never reads
auth tokens. Never mutates files. Never backgrounds/detaches.

Exit codes:
  0  Success
  10 CLOUD_ONLY_VIOLATION
  11 AUTH_REQUIRED
  12 NO_CLOUD_MODELS
  13 REDACTION_REFUSAL
  14 API_ERROR
  15 BAD_ARGS
  16 PYTHON_ERROR
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error

# --- Constants --------------------------------------------------------------

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MAX_CONTEXT_CHARS = 100_000
EXIT_SUCCESS = 0
EXIT_CLOUD_ONLY_VIOLATION = 10
EXIT_AUTH_REQUIRED = 11
EXIT_NO_CLOUD_MODELS = 12
EXIT_REDACTION_REFUSAL = 13
EXIT_API_ERROR = 14
EXIT_BAD_ARGS = 15
EXIT_PYTHON_ERROR = 16

# Cloud-tag validation: split on ':', accept if final tag equals 'cloud' or
# ends with '-cloud'. Reject everything else.
def is_cloud_tagged(model_name: str) -> bool:
    if not model_name:
        return False
    parts = model_name.split(":")
    final_tag = parts[-1]
    return final_tag == "cloud" or final_tag.endswith("-cloud")

# Routing map aligned with references/model-routing.md.
ROUTING_MAP = {
    "deep-debugging": [
        "kimi-k2.6:cloud",
        "kimi-k2-thinking:cloud",
        "kimi-k2:1t-cloud",
    ],
    "security-review": [
        "glm-5.1:cloud",
        "mistral-large-3:675b-cloud",
        "deepseek-v3.2:cloud",
    ],
    "performance-analysis": [
        "deepseek-v4-pro:cloud",
        "glm-5:cloud",
        "qwen3-coder:480b-cloud",
    ],
    "architecture-review": [
        "qwen3-coder:480b-cloud",
        "devstral-2:123b-cloud",
        "kimi-k2.6:cloud",
    ],
    "frontend-ui-review": [
        "kimi-k2.6:cloud",
        "gemini-3-flash-preview:cloud",
        "gpt-oss:120b-cloud",
    ],
    "test-plan-generation": [
        "kimi-k2.6:cloud",
        "qwen3-coder-next:cloud",
    ],
    "log-error-clustering": [
        "deepseek-v3.2:cloud",
        "deepseek-v4-pro:cloud",
        "minimax-m2.7:cloud",
    ],
    "documentation-summarization": [
        "gemma3:4b-cloud",
        "gpt-oss:20b-cloud",
        "gemini-3-flash-preview:cloud",
    ],
}

VALID_TASKS = list(ROUTING_MAP.keys())
VALID_ROLES = {
    "debugger",
    "security-reviewer",
    "performance-reviewer",
    "qa-planner",
    "architecture-critic",
    "summarizer",
}

ROLE_PROMPTS = {
    "debugger": (
        "Senior debugger. Hypothesize root cause. Suggest fix locations. "
        "Do not write fixes. Do not execute commands. Output ranked "
        "hypotheses, most likely first. Each hypothesis: file:line, "
        "suggested diagnostic step, risk level (Low/Med/High). If you "
        "lack context, say so — do not invent."
    ),
    "security-reviewer": (
        "Security reviewer. Find vulnerabilities in the supplied code. "
        "Map each finding to an OWASP category. Give a concrete exploit "
        "scenario for each. Suggest a mitigation area (not a fix). Do "
        "not write fixes. Do not execute commands. If you lack context, "
        "say so."
    ),
    "performance-reviewer": (
        "Performance engineer. Find bottlenecks in the supplied code or "
        "trace. Suggest measurements to confirm each. Give expected "
        "improvement range. Do not write fixes. Do not execute commands. "
        "If you lack context, say so."
    ),
    "qa-planner": (
        "QA architect. Design a test matrix for the supplied feature. "
        "Find edge cases. Identify coverage gaps in the existing tests. "
        "Suggest test fixtures. Do not write fixes. Do not execute "
        "commands. If you lack context, say so."
    ),
    "architecture-critic": (
        "Principal engineer. Critique layering, coupling, and boundaries "
        "in the supplied architecture. Identify violations. Propose a "
        "refactor with risk and sequencing. Do not write fixes. Do not "
        "execute commands. If you lack context, say so."
    ),
    "summarizer": (
        "Technical summarizer. Summarize the supplied document. Extract "
        "key points. List open questions. Suggest follow-ups. Do not "
        "invent content not in the source. If the document is internally "
        "inconsistent, say so."
    ),
}

# Redaction regexes. Each tuple: (compiled_regex, replacement_string).
REDACTION_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***REDACTED:bearer***"),
    (re.compile(r"Authorization:\s*[^\s\n]+"), "Authorization: ***REDACTED:auth***"),
    (re.compile(r"api_key\s*=\s*[^\s\n]+"), "api_key=***REDACTED:key***"),
    (re.compile(r"API_KEY\s*=\s*[^\s\n]+"), "API_KEY=***REDACTED:key***"),
    (re.compile(r"MONGO_URI\s*=\s*[^\s\n]+"), "MONGO_URI=***REDACTED:mongo_uri***"),
    (
        re.compile(r"mongodb(\+srv)?://[^\s/\?]+@[^\s]+"),
        "***REDACTED:mongo_uri***",
    ),
    (
        re.compile(r"OP_SESSION_[A-Za-z0-9_]+\s*=\s*[^\s\n]+"),
        "OP_SESSION_***REDACTED:op_session***",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "***REDACTED:pem_key***",
    ),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "***REDACTED:openai_key***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"), "***REDACTED:github_pat***"),
    (re.compile(r"password\s*=\s*[^\s\n]+"), "password=***REDACTED:password***"),
    (re.compile(r"token\s*=\s*[^\s\n]+"), "token=***REDACTED:token***"),
    (
        re.compile(r"client_secret\s*=\s*[^\s\n]+"),
        "client_secret=***REDACTED:client_secret***",
    ),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "***REDACTED:aws_akid***"),
    (
        re.compile(r"aws_secret_access_key\s*=\s*[^\s\n]+"),
        "aws_secret_access_key=***REDACTED:aws_secret***",
    ),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]+"), "***REDACTED:slack_token***"),
    (
        re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        "***REDACTED:jwt***",
    ),
]

# Files the script refuses to read entirely. Matched against basename and
# full path. If any --context-file matches, exit 13 REDACTION_REFUSAL.
REFUSED_FILE_PATTERNS = [
    re.compile(r"(^|/)\.env(\..+)?$"),
    re.compile(r"\.env$"),
    re.compile(r"\.key$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.crt$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"(^|/)id_rsa"),
    re.compile(r"(^|/)id_ed25519"),
    re.compile(r"(^|/)id_ecdsa"),
    re.compile(r"service_account"),
    re.compile(r"service-account"),
    re.compile(r"op_service_account_token"),
    re.compile(r"(/|\\)\.ollama(/|\\)config\.json$"),
    re.compile(r"(/|\\)\.config(/|\\)ollama(/|\\)config\.json$"),
]


# --- Helpers ----------------------------------------------------------------

def die(code: int, message: str, json_out: bool = False) -> None:
    """Print error and exit with code."""
    if json_out:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": message,
                    "exit_code": code,
                }
            )
        )
    else:
        print(f"ERROR [{code}]: {message}", file=sys.stderr)
    sys.exit(code)


def redact(text: str) -> tuple[str, int]:
    """Apply redaction regexes. Return (redacted_text, count)."""
    count = 0
    out = text
    for regex, replacement in REDACTION_PATTERNS:
        new_out, n = regex.subn(replacement, out)
        count += n
        out = new_out
    return out, count


def is_binary(path: str) -> bool:
    """Detect binary files by checking for null bytes in first 8 KB."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False


def is_refused_path(path: str) -> bool:
    """Check if path matches any refused pattern."""
    # Normalize separators for cross-platform matching.
    normalized = path.replace("\\", "/")
    for pat in REFUSED_FILE_PATTERNS:
        if pat.search(normalized) or pat.search(path):
            return True
    return False


def read_context_file(path: str, max_chars: int) -> tuple[str, str]:
    """Read a context file. Return (status, content_or_reason).
    status: 'included', 'skipped', or 'refused'.
    """
    if not os.path.isfile(path):
        return "skipped", f"not a file: {path}"
    if is_refused_path(path):
        return "refused", f"secret-bearing path: {path}"
    if is_binary(path):
        return "refused", f"binary file: {path}"
    size = os.path.getsize(path)
    if size > max_chars:
        return "skipped", f"file too large ({size} > {max_chars} chars): {path}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
        if len(content) > max_chars:
            return "skipped", f"content exceeds max_context_chars: {path}"
        return "included", content
    except OSError as e:
        return "skipped", f"read error: {e}"


def discover_cloud_models() -> tuple[list[str], list[str], list[str]]:
    """Discover cloud-tagged models.
    Returns (accepted, rejected_from_env, rejected_from_cli).
    - accepted: cloud-tagged models available (deduped, sorted).
    - rejected_from_env: non-cloud entries in OLLAMA_CLOUD_MODELS.
    - rejected_from_cli: non-cloud entries from `ollama list` (names only).
    """
    accepted: set[str] = set()
    rejected_env: list[str] = []
    rejected_cli: list[str] = []

    # 1. OLLAMA_CLOUD_MODELS allowlist (comma-separated).
    env_models = os.environ.get("OLLAMA_CLOUD_MODELS", "")
    if env_models:
        for raw in env_models.split(","):
            name = raw.strip()
            if not name:
                continue
            if is_cloud_tagged(name):
                accepted.add(name)
            else:
                rejected_env.append(name)

    # 2. `ollama list` CLI output.
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            # First line is header: NAME, ID, SIZE, MODIFIED. Skip it.
            for line in lines[1:]:
                # Columns are whitespace-separated. First column is NAME.
                parts = line.split()
                if not parts:
                    continue
                name = parts[0]
                if is_cloud_tagged(name):
                    accepted.add(name)
                else:
                    rejected_cli.append(name)
    except FileNotFoundError:
        # ollama CLI not on PATH. Only env allowlist matters.
        pass
    except subprocess.TimeoutExpired:
        pass

    return sorted(accepted), rejected_env, rejected_cli


def select_model(
    task: str,
    model_arg: str,
    discovered: list[str],
) -> tuple[str, str]:
    """Select a cloud model. Returns (model_name, selection_reason).
    Exits with CLOUD_ONLY_VIOLATION if explicit non-cloud model supplied.
    Exits with NO_CLOUD_MODELS if no cloud models discovered.
    """
    if not discovered and model_arg == "auto":
        die(
            EXIT_NO_CLOUD_MODELS,
            "No cloud-tagged models discovered. Sign in via the `ollama` "
            "GUI/CLI, set OLLAMA_CLOUD_MODELS, or proceed without "
            "delegation. No local-model fallback.",
        )

    if model_arg != "auto":
        if not is_cloud_tagged(model_arg):
            die(
                EXIT_CLOUD_ONLY_VIOLATION,
                f"Model '{model_arg}' is not cloud-tagged. Cloud-only rule "
                "enforced. No local-model fallback.",
            )
        if model_arg not in discovered:
            die(
                EXIT_NO_CLOUD_MODELS,
                f"Model '{model_arg}' is cloud-tagged but not discovered. "
                "Sign in via the `ollama` GUI/CLI or set OLLAMA_CLOUD_MODELS.",
            )
        return model_arg, f"explicit --model {model_arg}"

    # auto: walk the routing preference list for the task.
    preferences = ROUTING_MAP.get(task, [])
    for pref in preferences:
        if pref in discovered:
            return pref, f"auto-selected from routing map for {task}"

    # None of preferences available: pick first discovered, state why.
    if discovered:
        chosen = discovered[0]
        return chosen, (
            f"preferred list for {task} unavailable; fell back to first "
            f"discovered cloud model {chosen}"
        )

    die(EXIT_NO_CLOUD_MODELS, "No cloud-tagged models discovered.")
    return "", ""  # unreachable


def normalize_host(host: str) -> str:
    """Ensure host has http:// or https:// scheme. Default to http:// if missing."""
    if not host:
        return DEFAULT_OLLAMA_HOST
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return "http://" + host


def call_ollama(
    model: str,
    system_prompt: str,
    user_prompt: str,
    ollama_host: str,
) -> tuple[str, int, str]:
    """POST /api/chat to local Ollama daemon.
    Returns (response_body, http_status, error_message).
    """
    host = normalize_host(ollama_host)
    url = host.rstrip("/") + "/api/chat"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return raw, resp.status, ""
    except urllib.error.HTTPError as e:
        return "", e.code, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return "", 0, f"URL error: {e.reason}"
    except OSError as e:
        return "", 0, f"OS error: {e}"


def extract_response_text(raw_body: str) -> str:
    """Extract message.content from /api/chat response JSON."""
    try:
        parsed = json.loads(raw_body)
        msg = parsed.get("message", {})
        return msg.get("content", "")
    except json.JSONDecodeError:
        return ""


# --- Subcommands ------------------------------------------------------------

def cmd_discover(args) -> int:
    accepted, rejected_env, rejected_cli = discover_cloud_models()
    payload = {
        "cloud_only": True,
        "discovered_cloud_models": accepted,
        "rejected_from_env": rejected_env,
        "rejected_from_cli_count": len(rejected_cli),
        "ollama_host": os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("## Cloud-tagged models discovered")
        for m in accepted:
            print(f"  - {m}")
        if rejected_env:
            print("\n## Rejected from OLLAMA_CLOUD_MODELS (non-cloud)")
            for m in rejected_env:
                print(f"  - {m}  (CLOUD_ONLY_VIOLATION)")
        if rejected_cli:
            print(f"\n## Rejected from `ollama list` (non-cloud): {len(rejected_cli)} models (not listed)")
        print(f"\n## OLLAMA_HOST: {payload['ollama_host']}")
        if not accepted:
            print("\n## NO_CLOUD_MODELS — no cloud-tagged models available")
    return EXIT_SUCCESS


def cmd_self_test(args) -> int:
    """Validate cloud-only guard, redaction, routing. No live call."""
    accept_set = [
        "kimi-k2.6:cloud",
        "gpt-oss:120b-cloud",
        "qwen3-coder:480b-cloud",
        "qwen3-vl:235b-instruct-cloud",
    ]
    reject_set = [
        "llama3:8b",
        "qwen3-coder:480b",
        "deepseek-v3.2",
        "cloud-model-without-tag",
    ]
    failures: list[str] = []

    for name in accept_set:
        if not is_cloud_tagged(name):
            failures.append(f"accept {name}: FAILED (should be cloud-tagged)")
        else:
            print(f"accept {name}: PASS")

    for name in reject_set:
        if is_cloud_tagged(name):
            failures.append(f"reject {name}: FAILED (should NOT be cloud-tagged)")
        else:
            print(f"reject {name}: PASS")

    # Redaction sanity check.
    sample = (
        "Bearer abc123.token "
        "api_key=fakekey123 "
        "OP_SESSION_example=fakevalue "
        "sk-fakesk20char1234567890 "
        "ghp_fakegithubpat1234567890abcdefghijklmnopqrstuv "
        "password=fakepass "
        "mongodb+srv://fakeuser:fakepass@cluster.example.net/db "
    )
    redacted, count = redact(sample)
    if "fakekey123" in redacted or "fakevalue" in redacted or "fakepass" in redacted:
        failures.append(
            f"redaction: FAILED (secrets leaked through: {redacted[:120]}...)"
        )
    else:
        print(f"redaction: PASS ({count} redactions)")

    # Routing sanity check.
    if "deep-debugging" not in ROUTING_MAP:
        failures.append("routing: deep-debugging missing from ROUTING_MAP")
    elif "kimi-k2.6:cloud" not in ROUTING_MAP["deep-debugging"]:
        failures.append("routing: kimi-k2.6:cloud not preferred for deep-debugging")
    else:
        print("routing: deep-debugging -> kimi-k2.6:cloud: PASS")

    if failures:
        print("\n## Self-test FAILURES", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return EXIT_CLOUD_ONLY_VIOLATION

    print("\n## Self-test PASS")
    return EXIT_SUCCESS


def cmd_delegate(args) -> int:
    """Main delegation path."""
    json_out = args.json

    # Validate task.
    if args.task not in VALID_TASKS:
        die(
            EXIT_BAD_ARGS,
            f"Invalid --task '{args.task}'. Valid: {VALID_TASKS}",
            json_out,
        )

    # Validate role.
    if args.role not in VALID_ROLES:
        die(
            EXIT_BAD_ARGS,
            f"Invalid --role '{args.role}'. Valid: {sorted(VALID_ROLES)}",
            json_out,
        )

    # Validate prompt.
    if not args.prompt:
        die(EXIT_BAD_ARGS, "--prompt is required for delegation.", json_out)

    # Discover models.
    discovered, rejected_env, _ = discover_cloud_models()

    # If explicit non-cloud model, exit 10 even if cloud models exist.
    if args.model != "auto" and not is_cloud_tagged(args.model):
        die(
            EXIT_CLOUD_ONLY_VIOLATION,
            f"Model '{args.model}' is not cloud-tagged. "
            "CLOUD_ONLY_VIOLATION. No API call. No fallback model selected.",
            json_out,
        )

    # Select model.
    model, reason = select_model(args.task, args.model, discovered)

    # Read + redact context files.
    files_included: list[dict] = []
    files_skipped: list[dict] = []
    files_refused: list[dict] = []
    total_redactions = 0

    # Redact the prompt itself first.
    redacted_prompt, prompt_redactions = redact(args.prompt)
    total_redactions += prompt_redactions

    context_blobs: list[str] = []
    for path in args.context_file or []:
        status, content = read_context_file(path, args.max_context_chars)
        if status == "included":
            redacted_content, n = redact(content)
            total_redactions += n
            context_blobs.append(
                f"--- context file: {path} ---\n{redacted_content}\n"
            )
            files_included.append(
                {"path": path, "redactions": n, "chars": len(redacted_content)}
            )
        elif status == "refused":
            files_refused.append({"path": path, "reason": content})
            if not args.dry_run:
                die(
                    EXIT_REDACTION_REFUSAL,
                    f"Context file refused: {content}",
                    json_out,
                )
        else:
            files_skipped.append({"path": path, "reason": content})

    # Build user message.
    user_message = redacted_prompt
    if context_blobs:
        user_message += "\n\n" + "\n".join(context_blobs)

    system_prompt = ROLE_PROMPTS[args.role]

    # Output skeleton.
    payload = {
        "model": model,
        "task": args.task,
        "role": args.role,
        "cloud_only": True,
        "dry_run": args.dry_run,
        "selection_reason": reason,
        "files_included": files_included,
        "files_skipped": files_skipped,
        "files_refused": files_refused,
        "redaction_count": total_redactions,
        "status": "pending",
    }

    if args.dry_run:
        preview = redacted_prompt[:500]
        payload["prompt_preview"] = preview
        payload["status"] = "dry_run"
        if json_out:
            print(json.dumps(payload, indent=2))
        else:
            print("## Selected model")
            print(f"  {model}  ({reason})")
            print("\n## Task / Role")
            print(f"  task: {args.task}")
            print(f"  role: {args.role}")
            print("\n## Files included")
            for f in files_included:
                print(f"  - {f['path']}  (redactions: {f['redactions']}, chars: {f['chars']})")
            if not files_included:
                print("  (none)")
            print("\n## Files skipped")
            for f in files_skipped:
                print(f"  - {f['path']}  ({f['reason']})")
            if not files_skipped:
                print("  (none)")
            print("\n## Files refused")
            for f in files_refused:
                print(f"  - {f['path']}  ({f['reason']})")
            if not files_refused:
                print("  (none)")
            print(f"\n## Redactions: {total_redactions}")
            print("\n## Prompt preview (first 500 chars, redacted)")
            print(preview)
            print("\n## Status: dry_run (no API call)")
        return EXIT_SUCCESS

    # Live call.
    ollama_host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    raw, status, err = call_ollama(model, system_prompt, user_message, ollama_host)
    if err:
        if status in (401, 403):
            die(
                EXIT_AUTH_REQUIRED,
                "Ollama Cloud auth missing/expired. Sign in via the "
                "`ollama` GUI or CLI. Script does not handle auth tokens. "
                f"Detail: {err}",
                json_out,
            )
        die(EXIT_API_ERROR, f"Ollama daemon error: {err}", json_out)

    response_text = extract_response_text(raw)
    # Re-run redaction on response as a safety net.
    response_text, response_redactions = redact(response_text)
    total_redactions += response_redactions

    payload["response"] = response_text
    payload["http_status"] = status
    payload["redaction_count"] = total_redactions
    payload["status"] = "ok"

    if json_out:
        print(json.dumps(payload, indent=2))
    else:
        print("## Selected model")
        print(f"  {model}  ({reason})")
        print("\n## Task / Role")
        print(f"  task: {args.task}")
        print(f"  role: {args.role}")
        print("\n## Files included")
        for f in files_included:
            print(f"  - {f['path']}  (redactions: {f['redactions']}, chars: {f['chars']})")
        if not files_included:
            print("  (none)")
        print("\n## Files skipped")
        for f in files_skipped:
            print(f"  - {f['path']}  ({f['reason']})")
        if not files_skipped:
            print("  (none)")
        print("\n## Files refused")
        for f in files_refused:
            print(f"  - {f['path']}  ({f['reason']})")
        if not files_refused:
            print("  (none)")
        print(f"\n## Redactions: {total_redactions}")
        print(f"\n## HTTP status: {status}")
        print("\n## Response")
        print(response_text)
        print("\n## Status: ok")
    return EXIT_SUCCESS


# --- CLI --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ollama_cloud_delegate",
        description=(
            "Delegate read-only analysis tasks to Ollama Cloud models. "
            "Cloud-only. No local-model fallback. No secrets. Advisory "
            "output only."
        ),
    )
    p.add_argument("--discover", action="store_true", help="List cloud-tagged models available.")
    p.add_argument("--self-test", action="store_true", help="Validate cloud-only guard, redaction, routing. No live call.")
    p.add_argument("--task", help=f"Task family. Valid: {VALID_TASKS}")
    p.add_argument("--role", help=f"Agent role. Valid: {sorted(VALID_ROLES)}")
    p.add_argument("--model", default="auto", help="Model name or 'auto' (default: auto).")
    p.add_argument("--prompt", help="Task prompt text.")
    p.add_argument("--context-file", action="append", help="Context file path (repeatable). Each redacted + capped.")
    p.add_argument("--max-context-chars", type=int, default=DEFAULT_MAX_CONTEXT_CHARS, help=f"Max chars per context file (default: {DEFAULT_MAX_CONTEXT_CHARS}).")
    p.add_argument("--dry-run", action="store_true", help="No live API call; print selected model + sanitized preview.")
    p.add_argument("--json", action="store_true", help="Structured JSON output.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test:
        return cmd_self_test(args)
    if args.discover:
        return cmd_discover(args)
    # Default path: delegation. Requires --task, --role, --prompt.
    missing = []
    if not args.task:
        missing.append("--task")
    if not args.role:
        missing.append("--role")
    if not args.prompt:
        missing.append("--prompt")
    if missing:
        die(EXIT_BAD_ARGS, f"Missing required args: {missing}")
    return cmd_delegate(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(EXIT_PYTHON_ERROR)
    except Exception as e:
        print(f"ERROR [{EXIT_PYTHON_ERROR}]: {e}", file=sys.stderr)
        sys.exit(EXIT_PYTHON_ERROR)