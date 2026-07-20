# Security Policy

The detailed trust model, provider boundary, credential rules, egress behavior, gate integrity, budget controls, persistence model, and incident-response guidance are maintained in [`docs/SECURITY.md`](docs/SECURITY.md).

Do not open a public issue containing an API key, bearer token, auth file, private run artifact, prompt history, or provider response that may contain sensitive data. Use GitHub private vulnerability reporting when enabled, or contact the repository owner privately. Rotate a credential immediately if it was pasted into chat, a shell transcript, or a run log.

When reporting a receipt or resume vulnerability, include the plugin version, commit, run-ledger schema, minimal redacted reproduction, and whether the issue permits inconsistent artifacts, unaccounted redispatch, model substitution, or execution-handoff bypass. Never attach the private run directory wholesale.

The public [Codex fusion artifact](https://github.com/ahuserious/codex-fusion-artifact/tree/limited-cost-2026-07-20) is curated evidence, not a safe destination for new private run data.
