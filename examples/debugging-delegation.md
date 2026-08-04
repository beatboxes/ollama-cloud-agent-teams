# Debugging Delegation — Safe End-to-End Example

A safe, no-secrets example showing Claude Code delegating a dashboard-load debugging task to `kimi-k2.6:cloud` as an auxiliary agent-team reviewer. Claude Code remains the only actor allowed to mutate files. The delegated model returns advisory hypotheses only.

## Scenario

Claude Code (CC) is investigating slow dashboard load on `dashboard.example.com`. The Network panel shows an `/api/devices` waterfall with 47 serial requests. CC wants a second opinion on root cause hypotheses before opening any source files.

CC already captured a redacted trace to a temp file. No secrets, no tokens, no PII. Hostnames are kept generic.

## Step 1 — Prepare redacted context

CC writes a redacted trace excerpt to a temp file (no real secrets, fake hostnames used for illustration):

```
C:\Users\dev\temp\dashboard-trace-redacted.txt
```

Contents (excerpt):

```
[trace excerpt — dashboard.example.com, redacted]
GET /api/devices 200 47 serial requests, total 8.2s, avg 175ms each
GET /api/devices?page=1 200 178ms
GET /api/devices?page=2 200 181ms
... (47 requests in sequence, no parallelism)
[frontend framework: React 18, useEffect dependency array = [currentPage]]
[backend: nginx proxy → upstream dashboard service on port 3007]
[no pagination cursor, no batch endpoint, no client-side concurrency]
```

## Step 2 — Dry-run delegation

CC runs the delegate script in `--dry-run` mode first to verify model selection and redaction before any live call:

```
python C:\Users\dev\.claude\skills\ollama-cloud-agent-teams\scripts\ollama_cloud_delegate.py \
  --task deep-debugging \
  --role debugger \
  --model auto \
  --prompt "Dashboard load takes 8.2s. Network panel shows /api/devices waterfall with 47 serial requests. Hypothesize root cause and suggest fix locations. Output ranked hypotheses, most likely first, each with file:line + suggested diagnostic step + risk level." \
  --context-file C:\Users\dev\temp\dashboard-trace-redacted.txt \
  --dry-run
```

### Expected dry-run output

```markdown
## Selected model
kimi-k2.6:cloud

## Task / Role
Task: deep-debugging
Role: debugger

## Files included
- C:\Users\dev\temp\dashboard-trace-redacted.txt (1.2 KB, 0 redactions applied)

## Files skipped
(none)

## Redactions
0 patterns redacted across prompt + context files.

## Sanitized prompt preview (first 500 chars)
Dashboard load takes 8.2s. Network panel shows /api/devices waterfall with 47 serial requests.
Hypothesize root cause and suggest fix locations. Output ranked hypotheses, most likely first,
each with file:line + suggested diagnostic step + risk level.

## Status
DRY_RUN — no API call made. Cloud-only validation PASS. Model is cloud-tagged (final tag: cloud).
```

CC verifies the selected model is `kimi-k2.6:cloud` (cloud-tagged, correct task-family routing), the context file was included, and no redactions were needed (the trace excerpt had no secret patterns).

## Step 3 — Live delegation

CC drops `--dry-run` and runs the live call:

```
python C:\Users\dev\.claude\skills\ollama-cloud-agent-teams\scripts\ollama_cloud_delegate.py \
  --task deep-debugging \
  --role debugger \
  --model auto \
  --prompt "Dashboard load takes 8.2s. Network panel shows /api/devices waterfall with 47 serial requests. Hypothesize root cause and suggest fix locations. Output ranked hypotheses, most likely first, each with file:line + suggested diagnostic step + risk level." \
  --context-file C:\Users\dev\temp\dashboard-trace-redacted.txt \
  --json
```

### Expected JSON response (illustrative — actual output varies)

```json
{
  "model": "kimi-k2.6:cloud",
  "task": "deep-debugging",
  "role": "debugger",
  "cloud_only": true,
  "dry_run": false,
  "files_included": ["C:\\Users\\dev\\temp\\dashboard-trace-redacted.txt"],
  "files_skipped": [],
  "redaction_count": 0,
  "status": "ok",
  "response": "## Ranked hypotheses\n1. Missing client-side concurrency — frontend fires 47 serial /api/devices requests instead of batching or Promise.all-ing them. file: frontend/src/components/DeviceList.jsx (useEffect dependency array, pagination loop). diagnostic: open the component, check whether requests are awaited sequentially in a for-loop vs Promise.all. risk: Low\n2. No batch/cursor endpoint — backend exposes only single-page /api/devices?page=N, forcing N round trips. file: backend/routes/devices.js (route handler). diagnostic: grep routes for /api/devices, check for ?cursor= or ?per_page= support. risk: Med\n3. useEffect re-firing on every render — dependency array includes a state object that changes identity each render, causing the pagination loop to re-run. file: frontend/src/components/DeviceList.jsx. diagnostic: add console.log(currentPage) inside the effect, observe whether it logs more than 47 times. risk: Low\n\n## Open questions\n- Is there a server-side batch endpoint that the frontend is not using?\n- What is the cache policy on /api/devices responses?\n\n## Suggested next diagnostics (no fixes)\n- Read frontend/src/components/DeviceList.jsx and trace the useEffect dependency array.\n- Grep backend/routes for /api/devices to find available query params.\n- Capture a HAR with full request timing to confirm serial vs parallel dispatch."
}
```

## Step 4 — CC cross-checks every claim before any fix

Delegated models hallucinate. They cite file paths that do not exist. They invent APIs. CC verifies every claim:

1. **Open the cited file at the cited line.** CC reads `frontend/src/components/DeviceList.jsx` (on the target host via SSH per local project instructions). If the file does not exist or does not contain the cited useEffect, hypothesis 1 is discarded.
2. **Run the cited diagnostic.** CC greps `backend/routes` for `/api/devices` to confirm hypothesis 2. If no batch/cursor endpoint exists, hypothesis 2 is confirmed. If one exists and the frontend just isn't using it, hypothesis 1 is the real root cause.
3. **Read the cited log line.** If CC captured a HAR, CC reads it to confirm serial vs parallel dispatch.
4. **Discard claims that don't match reality.** If the model's cited `file:line` does not match the actual code, CC discards that hypothesis. CC does not apply the model's suggested fix verbatim — CC writes its own fix after confirming the root cause locally.

## Step 5 — CC applies the fix (CC's authority, not the model's)

CC confirmed hypothesis 1 (serial dispatch in useEffect). CC writes the fix locally, tests it with Playwright against `dashboard.example.com`, and verifies the waterfall becomes a parallel batch or a single cursor-paginated request. The delegated model never wrote a file, never executed a command, never touched production.

## What this example demonstrates

- **Cloud-only:** `kimi-k2.6:cloud` is cloud-tagged (final tag == `cloud`). No local-model fallback.
- **Redaction:** The trace excerpt had no secret patterns, so `redaction_count` is 0. If it had contained `Bearer ...` or `api_key=...`, the script would have redacted them and reported the count.
- **Advisory only:** The model returned hypotheses, not commands. CC cross-checked every claim before acting.
- **CC remains the only actor with mutation authority:** CC wrote the fix locally after verifying the root cause. The delegated model never touched a file.
- **No secrets sent:** The trace excerpt used generic hostnames and no tokens. If real secrets were present, the script's redaction regexes would have caught them. If a context file looked secret-bearing and couldn't be safely redacted, the script would have refused with exit 13 `REDACTION_REFUSAL`.

## What this example does NOT demonstrate

- **No local-model fallback:** If `kimi-k2.6:cloud` were unavailable, the script would fall through to `kimi-k2-thinking:cloud` or `kimi-k2:1t-cloud` per the routing map. If zero cloud models were discovered, exit 12 `NO_CLOUD_MODELS`.
- **No authority granted to the model:** The model never got shell exec, file write, network, credential, deploy, or wallet authority. The delegate script only sends a chat prompt and receives a text response.
- **No backgrounding:** The script ran in the foreground and blocked until the Ollama daemon returned. No `&`, no `nohup`, no detached subprocess (per CLAUDE.md §2).