# Changelog

## v0.1.0 — 2026-07-05
Initial public release.
- 6-file skill: SKILL.md, 3 references, 1 script, 1 example
- Cloud-only model selection (no local fallback)
- Secret redaction at prompt boundary
- Role-routed model selection: debugger / security-reviewer / performance-reviewer
- --self-test and --dry-run modes
- Explicit --model <name> override (rejected if not cloud-tagged, exit 10)