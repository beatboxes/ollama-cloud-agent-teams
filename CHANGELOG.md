# Changelog

## v0.1.1
- Add deepseek-v4.1-flash:cloud to routing map and --self-test.
- Accept an explicit cloud-tagged --model that is not pulled yet; daemon 404 "model not found" -> exit 12 with `ollama pull` hint.
- Thinking models: use message.thinking when message.content is empty; empty response -> exit 14; JSON adds response_source and done_reason.
- normalize_host: trailing slash, whitespace, bare port, default port, 0.0.0.0/[::] -> loopback.
- normalize_host fails closed (exit 15) on userinfo, non-http(s) schemes and unparsable values; keeps a path prefix; default port 11434 only for scheme-less values (`http://host` stays port 80).
- Daemon error text in API errors is redacted, stripped of control characters and capped at 300 chars; a truncated error body keeps exit 14 instead of exit 16.
- The HTTP status-line reason phrase gets the same treatment as the error text; C1 controls, U+2028/U+2029 and bidi embedding/override/isolate controls are stripped too; control characters are stripped before redaction (a NUL between `api_key` and `=` no longer hides the value); daemon text is cut to 64 KiB before it is scanned and the error body is read with the same bound.
- Truncated or malformed HTTP replies (IncompleteRead, BadStatusLine, ...) and absurdly nested error bodies exit 14 with valid `--json` output instead of exit 16.
- normalize_host also rejects whitespace, control characters, backslashes and `%` in the host name (an IPv6 zone id in brackets stays valid); OLLAMA_HOST is validated before `ollama list` is started (delegate, `--dry-run` and `--discover`).
- Docs: deepseek-v4.1-flash:cloud last in every routing row, explicit-model rule, exit 14/15 rows and `-I` wording corrected; SKILL.md and references/model-routing.md list exactly the models ROUTING_MAP routes (the security and performance lists are no longer merged).
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