# Ollama Cloud Agent Teams for Claude Code

A Claude Code personal skill that lets the lead Claude Code session delegate read-only specialist analysis (debugging, security review, performance analysis) to Ollama Cloud models, then cross-check each model's claims against the source code before synthesizing a final report.

## What it is NOT (non-native-subagent caveat)

This does not make Ollama models native first-class Claude Code Task agents. It provides an advisory delegation layer — the lead Claude Code session invokes the delegate script, reads the model's response, and verifies every claim against the cited `file:line` before propagating it. Ollama outputs are advisory only and never mutate files directly.

## Features

- **Cloud-only model selection** — no local-model fallback. Explicit `--model <name>` override is rejected if the model is not cloud-tagged (exit 10).
- **Secret redaction at the prompt boundary** — known secret patterns (API keys, Bearer tokens, PEM private keys, `password=`, MongoDB connection strings, etc.) are redacted before the prompt is sent to the model.
- **Role-routed model selection** — debugger / security-reviewer / performance-reviewer roles auto-select from a routing map.
- **`--self-test` mode** — exercises accept patterns, reject patterns, redaction, and routing without making any API call.
- **`--dry-run` mode** — builds the request, redacts the prompt, prints the payload, and exits without calling the model.
- **`--discover --json`** — lists cloud-tagged models reachable from your `OLLAMA_HOST` endpoint.
- **Explicit `--model <name>` override** — power-user escape hatch, but rejected if the model is not cloud-tagged.

## Requirements

- Claude Code CLI
- Python 3.8+
- `gh` CLI (only for repo management, not skill operation)
- Ollama Cloud access (set `OLLAMA_HOST` to your cloud endpoint)
- No Python packages beyond stdlib

## Install (PowerShell on Windows)

```powershell
git clone https://github.com/beatboxes/ollama-cloud-agent-teams.git "$env:USERPROFILE\.claude\skills\ollama-cloud-agent-teams"
python "$env:USERPROFILE\.claude\skills\ollama-cloud-agent-teams\scripts\ollama_cloud_delegate.py" --self-test
python "$env:USERPROFILE\.claude\skills\ollama-cloud-agent-teams\scripts\ollama_cloud_delegate.py" --discover --json
```

## Install (macOS/Linux)

```bash
git clone https://github.com/beatboxes/ollama-cloud-agent-teams.git ~/.claude/skills/ollama-cloud-agent-teams
python ~/.claude/skills/ollama-cloud-agent-teams/scripts/ollama_cloud_delegate.py --self-test
python ~/.claude/skills/ollama-cloud-agent-teams/scripts/ollama_cloud_delegate.py --discover --json
```

## Verify install

`--self-test` should print: accept 4/4, reject 4/4, redaction 7, routing 1, EXIT 0. Then `--discover --json` should list cloud-tagged models reachable from your `OLLAMA_HOST`.

## Example debugging delegation (dry-run, no API call)

```powershell
$env:OLLAMA_HOST = "http://127.0.0.1:11434"
python "scripts\ollama_cloud_delegate.py" --task deep-debugging --role debugger --model auto --prompt "Inspect app.py for logic bugs. Output ranked hypotheses with file:line." --context-file ".\app.py" --dry-run --json
Remove-Item Env:\OLLAMA_HOST -ErrorAction SilentlyContinue
```

## Security warning

Never paste secrets, API keys, tokens, credentials, or production data into `--prompt` or `--context-file`. The script redacts known secret patterns at the prompt boundary, but treat this as defense-in-depth — do not feed secrets to delegated models in the first place. Use `--dry-run` to preview the redacted prompt before any live call.

## License

MIT — see [LICENSE](LICENSE).
