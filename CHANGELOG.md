# Changelog

## v0.1.1
- Add deepseek-v4.1-flash:cloud to routing map and --self-test.
- Accept an explicit cloud-tagged --model that is not pulled yet; daemon 404 "model not found" -> exit 12 with `ollama pull` hint.
- Thinking models: use message.thinking when message.content is empty; empty response -> exit 14; JSON adds response_source and done_reason.
- normalize_host: trailing slash, whitespace, bare port, default port, 0.0.0.0/[::] -> loopback.
- --json now also applies to the missing-args error.
- Linux docs (python3 -I), Claude Code lead usage line, stdlib unittest suite under tests/.

## v0.1.0 — 2026-07-05
Initial public release.
- 6-file skill: SKILL.md, 3 references, 1 script, 1 example
- Cloud-only model selection (no local fallback)
- Secret redaction at prompt boundary
- Role-routed model selection: debugger / security-reviewer / performance-reviewer
- --self-test and --dry-run modes
- Explicit --model <name> override (rejected if not cloud-tagged, exit 10)