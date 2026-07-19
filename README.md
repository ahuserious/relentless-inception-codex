# Relentless Inception for Codex

Relentless Inception is a Codex-native plugin for bounded multi-model deliberation, generative fusion, adversarial review gates, and verified execution handoff. It is built for cases where the cost of a wrong answer is materially larger than the cost of several frontier-model calls.

The default pipeline is:

1. independent, identity-hidden model seats with distinct lenses and context bundles;
2. deterministic evidence supplied by Codex or the user;
3. a structured comparative judge that diagnoses rather than decides;
4. a fresh strongest-model synthesis that must preserve supported minority findings;
5. independent reviewers bound to the exact artifact SHA-256;
6. a passing handoff to the active Codex session, which retains workspace, sandbox, and approval control.

This is a new runtime-backed implementation inspired by [`ahuserious/relentless-inception-grok`](https://github.com/ahuserious/relentless-inception-grok), not a mechanical host rename. The source Grok plugin is principally an orchestration contract; this port implements the call graph, configuration enforcement, persistence, budgets, kill switch, resume semantics, structured gates, and Codex packaging in code.

## Status

Version `0.1.0` is an alpha suitable for deliberate, cost-aware use. Direct xAI Grok 4.5, OpenAI Responses, Anthropic Messages, OpenRouter, OpenRouter native Fusion, and generic OpenAI-compatible/TrustedRouter transports are supported. External API seats can use provider-hosted tools when explicitly configured, but they never receive Codex filesystem access.

Codex plugins currently do not auto-register plugin-owned native agents. The bundled skills coordinate native Codex reviewers/executors through the active host when available; opt-in TOML templates are included for personal agent/provider setup.

## Install from this checkout

```bash
codex plugin marketplace add /absolute/path/to/relentless-inception-codex
codex plugin add relentless-inception@ahuserious-codex
```

Restart the Codex desktop app after first installation so the bundled MCP server and skills are discovered.

The plugin stores validated overrides and run evidence under `PLUGIN_DATA` when Codex supplies it, or `~/.codex/relentless-inception` otherwise. Secrets are never accepted as config values. Use environment-variable names or an explicitly configured owner-only `secret_env_files` path.

## Use

Ask Codex:

> Use Relentless Inception to fuse independent frontier-model plans, execute the verified plan, and gate the resulting diff.

The plugin exposes MCP tools for:

- `config_show`, `config_schema`, `config_get`, `config_set`, and `config_validate`;
- `doctor`, `provider_models`, and the opt-in, billable `provider_test`;
- `fuse` and standalone `adversarial_gate`;
- `run_status`, `run_abort`, and `execution_handoff`.

There is no undocumented settings GUI. The complete schema is displayable through `config_schema`, and every persistent change goes through validated user overrides.

For a shell-level diagnostic without installation:

```bash
./scripts/ri doctor
./scripts/ri config validate
```

## Safety and cost boundaries

- Provider credentials are read from named environment variables or explicitly listed 0600 files; values are never printed.
- xAI/OpenAI Responses retention defaults to `store: false`.
- Model outputs are untrusted data and are fenced in downstream prompts.
- Panel, call, token, tool-call, provider-cost, total-cost, wall-time, and concurrency limits are enforced.
- Maximum-intelligence mode fails closed on panel collapse, schema failure, gate failure, or budget exhaustion.
- The ordinary execution backend is `active_codex`. Recursive `codex exec` is disabled unless both config and an explicit command confirmation enable it.
- A global or per-run empty `KILL` file stops further calls.

See [configuration](docs/CONFIGURATION.md), [architecture](docs/ARCHITECTURE.md), [providers](docs/PROVIDERS.md), [security](docs/SECURITY.md), and [design evidence](docs/DESIGN_EVIDENCE.md).

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q plugins/relentless-inception
```

The unit suite is network-free. Live provider probes are intentionally separate because they can incur API cost.

## License

No license has been selected yet. The repository owner should choose one before public distribution. See [NOTICE.md](NOTICE.md) for provenance.
