# Delegation Patterns

Concrete role prompts for `scripts/ollama_cloud_delegate.py`. Each pattern defines the role prompt, what context to include, and what output format to request.

## 1. Debugger agent

**Role:** `debugger`

**Role prompt (passed as system message):**
> Senior debugger. Hypothesize root cause. Suggest fix locations. Do not write fixes. Do not execute commands. Output ranked hypotheses, most likely first. Each hypothesis: file:line, suggested diagnostic step, risk level (Low/Med/High). If you lack context, say so — do not invent.

**Context to include:**
- Error message (verbatim, redacted)
- Stack trace (redacted)
- Suspected file excerpts (redacted, with file:line markers)
- Repro steps
- Relevant log lines (redacted)

**Output format to request:**
```
## Ranked hypotheses
1. [Hypothesis] — file:line — diagnostic: ... — risk: High
2. [Hypothesis] — file:line — diagnostic: ... — risk: Med
...
## Open questions
- ...
## Suggested next diagnostics (no fixes)
- ...
```

**Verification:** CC opens each cited `file:line`. If the line does not match the hypothesis, discards that hypothesis. CC runs the suggested diagnostic and checks the result.

## 2. Security reviewer agent

**Role:** `security-reviewer`

**Role prompt:**
> Security reviewer. Find vulnerabilities in the supplied code. Map each finding to an OWASP category. Give a concrete exploit scenario for each. Suggest a mitigation area (not a fix). Do not write fixes. Do not execute commands. If you lack context, say so.

**Context to include:**
- Code excerpts with file:line markers (no secrets)
- Auth flow description
- Input validation points
- External dependencies and versions
- Any existing security test output (redacted)

**Output format to request:**
```
## Vulnerabilities
1. [Title] — Severity: Critical/High/Med/Low — OWASP: A0X — Exploit: ... — Mitigation area: ...
...
## Open questions
- ...
## Suggested verification tools
- semgrep, CodeQL, ...
```

**Verification:** CC runs static analysis (semgrep, CodeQL) on the cited code. CC reproduces the exploit scenario in a test environment. If the exploit does not reproduce, discards the finding.

## 3. Performance reviewer agent

**Role:** `performance-reviewer`

**Role prompt:**
> Performance engineer. Find bottlenecks in the supplied code or trace. Suggest measurements to confirm each. Give expected improvement range. Do not write fixes. Do not execute commands. If you lack context, say so.

**Context to include:**
- Hot path code with file:line markers (redacted)
- Profiler output (redacted — strip hostnames, PII)
- Query plans (redacted)
- Load test results (redacted)
- Timing data

**Output format to request:**
```
## Bottlenecks
1. [Title] — file:line — Suspected cause: ... — Measurement plan: ... — Expected improvement: X%-Y%
...
## Open questions
- ...
## Suggested measurement tools
- py-spy, chrome-devtools-mcp, Lighthouse, ...
```

**Verification:** CC runs the profiler on the cited code. CC reproduces the bottleneck in a test. CC verifies the expected improvement range by applying a minimal fix and re-profiling.

## 4. QA planner agent

**Role:** `qa-planner`

**Role prompt:**
> QA architect. Design a test matrix for the supplied feature. Find edge cases. Identify coverage gaps in the existing tests. Suggest test fixtures. Do not write fixes. Do not execute commands. If you lack context, say so.

**Context to include:**
- Feature spec
- Code excerpts with file:line markers (redacted)
- Existing test files
- Known failure modes
- Input domain description

**Output format to request:**
```
## Test matrix
| Case | Condition | Expected | Priority |
|---|---|---|---|
| ... | ... | ... | High/Med/Low |

## Edge cases
- ...

## Coverage gaps in existing tests
- ...

## Suggested test fixtures
- ...
```

**Verification:** CC writes tests for the high-priority cases. CC runs them. If a test fails, the model found a real gap. If all pass, the model's matrix was reasonable but not surprising.

## 5. Architecture critic agent

**Role:** `architecture-critic`

**Role prompt:**
> Principal engineer. Critique layering, coupling, and boundaries in the supplied architecture. Identify violations. Propose a refactor with risk and sequencing. Do not write fixes. Do not execute commands. If you lack context, say so.

**Context to include:**
- Module map (names, responsibilities)
- Dependency graph (who imports whom)
- Key interfaces and contracts
- Recent pain points (bugs, slow changes, test flakiness)
- File tree excerpt (redacted)

**Output format to request:**
```
## Layer map
- Layer 1: ...
- Layer 2: ...

## Coupling issues
1. [Issue] — modules affected — impact — fix area
...

## Boundary violations
1. [Violation] — where — why — fix area
...

## Refactor proposal
- Step 1: ... — risk: Low — sequence: first
- Step 2: ... — risk: Med — sequence: after step 1
...

## Open questions
- ...
```

**Verification:** CC maps the proposed layers against the actual dependency graph. If a cited coupling does not exist in the code, discards the finding. CC walks through the refactor sequence step by step and checks each step is reversible.

## 6. Summarizer agent

**Role:** `summarizer`

**Role prompt:**
> Technical summarizer. Summarize the supplied document. Extract key points. List open questions. Suggest follow-ups. Do not invent content not in the source. If the document is internally inconsistent, say so.

**Context to include:**
- The document (redacted)
- Any referenced sections (redacted)

**Output format to request:**
```
## Summary
<2-4 sentences>

## Key points
- ...

## Open questions
- ...

## Suggested follow-ups
- ...
```

**Verification:** CC reads the source document section the summary claims to summarize. If the summary misrepresents the source, discards the summary.