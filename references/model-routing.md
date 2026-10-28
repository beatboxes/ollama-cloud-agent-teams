# Model Routing

Routing table for `scripts/ollama_cloud_delegate.py`. Preferences are conditional on local discovery — never hardcode availability. When a preferred model is unavailable, fall through to the next preference or to any discovered cloud model that fits the task family.

## Cloud-tag validation

A model is cloud-tagged when, after splitting the name on `:`, the final tag:
- equals `cloud` (e.g. `kimi-k2.6:cloud`), OR
- ends with `-cloud` (e.g. `gpt-oss:120b-cloud`, `qwen3-coder:480b-cloud`, `qwen3-vl:235b-instruct-cloud`).

All other names are rejected with exit code 10 `CLOUD_ONLY_VIOLATION`. No local-model fallback.

## Routing table

| Task family | Preferred cloud models (fallback order) | Context-size guidance | Verification expectations |
|---|---|---|---|
| Deep debugging / hard bug trace | `kimi-k2.6:cloud` → `kimi-k2-thinking:cloud` → `kimi-k2:1t-cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with 100k+ ctx | 200k+ chars of relevant code + stack trace + repro steps | Repro steps, ranked root-cause hypotheses (most likely first), each with `file:line` + suggested diagnostic step + risk level. CC verifies each hypothesis against actual code. |
| Security review | `glm-5.1:cloud` → `mistral-large-3:675b-cloud` → `deepseek-v3.2:cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with 100k+ ctx | 100k+ chars of code excerpts (no secrets), auth flow, input validation points | Vulnerability list with severity (Critical/High/Med/Low), OWASP category, exploit scenario, suggested mitigation area. CC verifies with static analysis tools (semgrep, CodeQL). |
| Performance analysis | `deepseek-v4-pro:cloud` → `glm-5:cloud` → `qwen3-coder:480b-cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with 100k+ ctx | 100k+ chars of hot path code, profiler output (redacted), query plans | Bottleneck list with suspected cause, measurement plan, expected improvement range. CC verifies with profiler (py-spy, chrome-devtools-mcp, Lighthouse). |
| Architecture review | `qwen3-coder:480b-cloud` → `devstral-2:123b-cloud` → `kimi-k2.6:cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with 200k+ ctx | 200k+ chars of module map, dependency graph, key interfaces | Layer map, coupling issues, boundary violations, refactor proposal with risk + sequencing. CC verifies against repo structure. |
| Frontend/UI review | `kimi-k2.6:cloud` (vision capable) → `gemini-3-flash-preview:cloud` → `gpt-oss:120b-cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with vision capability | 50k+ chars + screenshot path (script accepts image context files) | Issue list with severity (Critical/High/Med/Low), repro steps, suggested fix area. CC verifies with Playwright + chrome-devtools-mcp. |
| Test-plan generation | `kimi-k2.6:cloud` → `qwen3-coder-next:cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with 100k+ ctx | 100k+ chars of feature spec, code excerpts, existing tests | Test matrix (case × condition), edge cases, coverage gaps, suggested test fixtures. CC verifies by running the generated tests. |
| Log/error clustering | `deepseek-v3.2:cloud` → `deepseek-v4-pro:cloud` → `minimax-m2.7:cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model with 100k+ ctx | 100k+ chars of log lines (redacted) | Cluster list with representative sample line, suspected root cause per cluster, suggested next diagnostic. CC verifies with actual logs. |
| Documentation summarization | `gemma3:4b-cloud` → `gpt-oss:20b-cloud` → `gemini-3-flash-preview:cloud` → `ministral-3:14b-cloud` → `deepseek-v4.1-flash:cloud` → any discovered cloud model | 30k+ chars of doc text | Summary, key points, open questions, suggested follow-ups. CC verifies against source doc. |

## Selection rules

1. If `--model <explicit>` is passed and the model is cloud-tagged, use it, even when it is not in `ollama list` or `OLLAMA_CLOUD_MODELS`. If the daemon then answers "model not found", the script exits 12 with an `ollama pull <model>` hint.
2. If `--model auto` is passed, pick the first available discovered model from the task family's preference list.
3. If none of the preferred models are available, pick the first discovered cloud model and clearly state why ("preferred list unavailable, falling back to `<model>`").
4. If zero cloud models are discovered, exit 12 `NO_CLOUD_MODELS`. No local fallback.

## Context-size notes

- The script caps context at `--max-context-chars` (default 100000). Increase only if the model's discovered context length supports it.
- For models with 200k+ ctx (kimi-k2.6, kimi-k2:1t, deepseek-v3.2, qwen3-coder:480b), 200k chars is safe.
- For smaller-context models (gemma3:4b, ministral-3:14b), keep context under 30k chars.
- The script does not auto-adjust context size based on model — the caller must pass `--max-context-chars` appropriately.

## Verification expectations

Every delegated output must be verified by Claude Code before acting. The table above lists the expected verification step per task family. In all cases:

- Open the cited file at the cited line.
- Run the cited test.
- Read the cited log line.
- If the model's claim does not match reality, discard the claim.

Delegated models hallucinate. They cite file paths that do not exist. They invent APIs. Verify everything.