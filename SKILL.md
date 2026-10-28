---
name: ollama-cloud-agent-teams
description: Use when Claude Code needs to delegate read-only specialist analysis, debugging hypotheses, security review, architecture critique, QA planning, performance analysis, or log clustering to Ollama Cloud models as auxiliary agent-team reviewers. Cloud-only. No local models. No secrets in prompts. Advisory output only — delegated models never mutate files.
---

# Ollama Cloud Agent Teams

Delegate read-only specialist analysis tasks to Ollama Cloud models as auxiliary agent-team reviewers. Cloud-only. Claude Code remains the only actor allowed to mutate files.

## Cloud-only rule

Accept only models with a cloud tag. A model is cloud-tagged when, after splitting the name on `:`, the final tag equals `cloud` (e.g. `kimi-k2.6:cloud`) or ends with `-cloud` (e.g. `gpt-oss:120b-cloud`, `qwen3-coder:480b-cloud`, `qwen3-vl:235b-instruct-cloud`).

Never fall back to local models. If no cloud models are discovered, report `NO_CLOUD_MODELS` and give Claude Code next steps that do not expose secrets (sign in via the `ollama` GUI/CLI, set `OLLAMA_CLOUD_MODELS`, or proceed without delegation).

Never claim a model is available unless it is discovered locally via `ollama list`, configured through `OLLAMA_CLOUD_MODELS`, or successfully smoke-tested. An explicit cloud-tagged `--model` that is not listed is still sent to the daemon; a daemon "model not found" is reported as exit 12 with an `ollama pull` hint.

## No-logo/no-assets rule

This skill is text/scripts/references only. Do not create `assets/`, logo, icon, or image files. If a future change requires visuals, stop and ask the operator first.

## Secret-redaction rule

Never send to a delegated model: secrets, tokens, private keys, session cookies, `OP_SESSION_*` values, `.env` contents, 1Password item contents, wallet secrets, mnemonics, production DB dumps, private customer data, or host credential directory contents.

Before sending any context, redact: `Bearer ...`, `Authorization: ...`, `api_key=...`, `API_KEY=...`, `MONGO_URI=...`, `mongodb(\+srv)?://...` URIs with credentials, `OP_SESSION_<account>=<value>`, `-----BEGIN ... PRIVATE KEY-----` blocks, `sk-[A-Za-z0-9]{20,}` patterns, GitHub PATs (`gh[pousr]_[A-Za-z0-9]{36}`), `password=...`, `token=...`, `client_secret=...`.

Limit context to relevant excerpts and file paths. No full DB dumps. The delegate script enforces this — do not bypass it.

## Advisory-only rule

Delegated Ollama models are read-only reviewers. They never get authority to: write files, execute shell commands, make network calls, handle credentials, deploy, restart services, access databases, sign wallet transactions, or mutate any state.

Treat delegated output as untrusted suggestions. Cross-check every claim against local files, tests, and logs before acting. Include delegated output in evidence only if it contains no secrets.

## Model selection preferences

Model availability is conditional on local discovery. Never hardcode availability. When a preferred model is unavailable, fall through to the next preference or to any discovered cloud model that fits the task.

| Task family | Preferred cloud models (in order) |
|---|---|
| Deep debugging, long-horizon coding, stubborn bug analysis, large-context codebase tracing, refactor strategy, complex QA design, UI/code review | `kimi-k2.6:cloud` (1042B, 262k ctx, vision+thinking+tools) → `kimi-k2-thinking:cloud` → `kimi-k2:1t-cloud` → `deepseek-v4.1-flash:cloud` |
| Code-heavy architecture review, multi-file tracing | `qwen3-coder:480b-cloud` → `qwen3-coder-next:cloud` → `devstral-2:123b-cloud` → `kimi-k2.6:cloud` → `deepseek-v4.1-flash:cloud` |
| Long-context reasoning, log/error clustering | `deepseek-v3.2:cloud` → `deepseek-v4-pro:cloud` → `minimax-m2.7:cloud` → `deepseek-v4.1-flash:cloud` |
| General reasoning, security review, performance analysis | `glm-5.1:cloud` → `mistral-large-3:675b-cloud` → `glm-5:cloud` → `deepseek-v3.2:cloud` → `deepseek-v4.1-flash:cloud` |
| Fast summarization, sanity checks, simple alternate explanations | `gemma3:4b-cloud` → `gpt-oss:20b-cloud` → `gemini-3-flash-preview:cloud` → `ministral-3:14b-cloud` → `deepseek-v4.1-flash:cloud` |
| Frontend/UI review (vision useful) | `kimi-k2.6:cloud` → `gemini-3-flash-preview:cloud` → `gpt-oss:120b-cloud` → `deepseek-v4.1-flash:cloud` |
| Test-plan generation | `kimi-k2.6:cloud` → `qwen3-coder-next:cloud` → `deepseek-v4.1-flash:cloud` |

## Standard delegation workflow

1. **Define the subtask and expected output.** Write a focused prompt with the question, the suspected file paths, and the output format you want (e.g. ranked hypotheses, vulnerability list, test matrix).
2. **Minimize and redact context.** Include only the relevant excerpts. Strip secrets per the redaction rule. The delegate script enforces redaction — do not bypass it.
3. **Select a model by task fit and availability.** Use `--model auto` to let the script pick from the routing map, or pass an explicit cloud-tagged model name.
4. **Call `scripts/ollama_cloud_delegate.py`:**
   ```
   python3 -I ~/.claude/skills/ollama-cloud-agent-teams/scripts/ollama_cloud_delegate.py \
     --task deep-debugging \
     --role debugger \
     --model auto \
     --prompt "<focused question>" \
     --context-file <redacted-excerpt-path> \
     [--dry-run] [--json]
   ```
   Linux/macOS: use `python3` (there is often no `python`). On Windows use `python` and backslash paths. `-I` runs Python in isolated mode: `PYTHON*` environment variables (such as `PYTHONPATH`) and the user site-packages are ignored, and neither the script's directory nor the current directory is put on `sys.path`, so a stray `json.py` there is not imported. Example with the DeepSeek flash cloud model: `--model deepseek-v4.1-flash:cloud`. If that model is not pulled yet the script exits 12 and says to run `ollama pull deepseek-v4.1-flash:cloud` (the daemon needs `ollama signin` first). Set `OLLAMA_HOST` only if the daemon is not on `127.0.0.1:11434`; bare `host:port` (port defaults to 11434 only when no `http(s)://` is given), `0.0.0.0` and a `/path` prefix are accepted; userinfo and non-http(s) schemes exit 15. For thinking models the answer is taken from `message.content`, or from `message.thinking` when content is empty (JSON field `response_source` says which).
5. **Treat the result as advisory evidence, not truth.** Delegated models hallucinate. They cite file paths that do not exist. They invent APIs. Verify every claim.
6. **Cross-check with local files, tests, and logs before any fix.** Open the file at the cited line. Run the test. Read the log line. If the model's claim does not match reality, discard the claim.
7. **Include delegated output in evidence only if it contains no secrets.** If redaction removed secrets, the sanitized output is safe to quote. Never quote raw model output that includes unredacted secrets.

## When to delegate

Delegate when:
- You want a second opinion on a hard bug hypothesis.
- You want a security review pass on a diff.
- You want a test matrix for a new feature.
- You want an architecture critique before a refactor.
- You want log clustering on a large error sample.
- You want a fast summary of a long doc.

Do NOT delegate when:
- The task requires mutating files, running commands, or accessing secrets.
- You already know the answer and just need to apply it.
- The context cannot be safely redacted (e.g. the prompt itself is a secret).
- No cloud models are available and the task cannot wait for sign-in.

## Exit codes from the delegate script

| Code | Meaning |
|---|---|
| 0 | Success |
| 10 | `CLOUD_ONLY_VIOLATION` — a non-cloud model was supplied or found in the allowlist |
| 11 | `AUTH_REQUIRED` — Ollama Cloud auth missing or expired |
| 12 | `NO_CLOUD_MODELS` — zero cloud-tagged models discovered, or the daemon reports the named model is not pulled |
| 13 | `REDACTION_REFUSAL` — a context file looks secret-bearing and cannot be safely redacted |
| 14 | `API_ERROR` — HTTP error from the Ollama daemon, or an empty response (no `message.content` and no `message.thinking`) |
| 15 | `BAD_ARGS` — invalid CLI arguments, or an unusable `OLLAMA_HOST` (userinfo before `@`, or a scheme other than http/https) |
| 16 | `PYTHON_ERROR` — unexpected Python exception |

## References

- `references/model-routing.md` — full routing table with context-size guidance and verification expectations per task family.
- `references/security-and-redaction.md` — full redaction rules, refused-file patterns, and safety constraints.
- `references/delegation-patterns.md` — concrete role prompts for debugger, security-reviewer, performance-reviewer, qa-planner, architecture-critic, and summarizer agents.
- `examples/debugging-delegation.md` — a safe end-to-end example showing Claude Code delegating a dashboard-load debugging task to `kimi-k2.6:cloud`.
- `scripts/ollama_cloud_delegate.py` — the delegate script. Cloud-only. Stdlib only. No local fallback.