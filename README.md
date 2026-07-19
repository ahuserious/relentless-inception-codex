# Relentless Inception for Codex

Relentless Inception is a Codex-native plugin for bounded multi-model deliberation, generative fusion, adversarial review gates, and verified execution handoff. It is built for cases where the cost of a wrong answer is materially larger than the cost of several frontier-model calls.

The default pipeline is:

1. independent, identity-hidden model seats with distinct persona/context lenses (the host must pre-partition material when seats need different data);
2. deterministic evidence supplied by Codex or the user;
3. a structured comparative judge that diagnoses rather than decides;
4. a fresh strongest-model synthesis that must preserve supported minority findings;
5. independent reviewers bound to the exact artifact SHA-256;
6. a persisted host-workflow packet whose enabled plan/pre-execution gates must pass before the active Codex session authorizes mutation.

This is a new runtime-backed implementation inspired by [`ahuserious/relentless-inception-grok`](https://github.com/ahuserious/relentless-inception-grok), not a mechanical host rename. The source Grok plugin is principally an orchestration contract; this port implements the call graph, configuration enforcement, persistence, budgets, kill switch, resume semantics, structured gates, and Codex packaging in code.

## Status

Version `0.1.1` is an alpha suitable for deliberate, cost-aware use. Direct xAI Grok 4.5, OpenAI Responses, Anthropic Messages, OpenRouter, OpenRouter native Fusion, and generic OpenAI-compatible/TrustedRouter transports are supported. External API seats can use provider-hosted tools when explicitly configured, but they never receive Codex filesystem access.

Version 0.1.1 intentionally cannot resume run state written by earlier releases. Existing run directories remain preserved for audit, but rerunning their work requires a new run ID; see the [run-state compatibility boundary](docs/ARCHITECTURE.md#run-state-compatibility).

Codex plugins currently do not auto-register plugin-owned native agents. The bundled skills coordinate compatible native Codex reviewers/executors through the active host; opt-in provider/agent TOML templates are retained for explicit compatibility retesting. On Codex 0.145, a direct foreground Grok 4.5 text turn succeeded only after every tool surface was removed, but an actual spawned custom-agent turn still failed at xAI with HTTP 422 because the Codex subagent input did not match xAI's accepted `ModelInput` variants. A separate tool-result continuation also failed with a compaction-blob error. The shipped default therefore registers no native Grok role. Multiple external Grok 4.5/4.3 seats provide the operational Grok path for fusion, gates, and xAI-hosted tools.

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
- `doctor`, `provider_models`, and the opt-in, billable `provider_test` for ordinary seats (OpenRouter Fusion probes are refused because they can fan out);
- `fuse` and standalone `adversarial_gate`;
- `run_status`, `run_abort`, and `execution_handoff`.

There is no undocumented settings GUI. The complete schema is displayable through `config_schema`, and every persistent change goes through validated user overrides.

For a shell-level diagnostic without installation:

```bash
./scripts/ri doctor
./scripts/ri config validate
```

## Safety and cost boundaries

- Provider credentials are read from named environment variables or explicitly listed 0600 files; values are never printed, and authenticated completion/model-discovery redirects are refused.
- xAI/OpenAI Responses retention defaults to `store: false`.
- Model outputs are untrusted data and are fenced in downstream prompts.
- During `fuse` or `adversarial_gate`, every HTTP-success response that fails semantic validation is persisted and accounted before fallback; its usage, cost, or unknown-cost status can latch a stop and prevent fallback.
- Every reusable model result participates in the schema-v3 run-ledger receipt chain binding the run and hashed prompt, reserved HTTP attempt, complete visible response, private raw artifact, and exact ledger entry; incomplete crash state fails closed without redispatch.
- Panel, call, token, tool-call, provider-cost, total-cost, wall-time, and concurrency limits are enforced within a run/process. A cross-process lease permits only one active owner for a run ID, but it does not globally coordinate provider concurrency across distinct run IDs or processes.
- Duplicate panel, optional-panel, or reviewer entries and required/optional panel overlap are rejected. Any completed `NEEDS_WORK` or `FAIL` verdict blocks a gate even when the numeric `PASS` quorum is met.
- Maximum-intelligence mode fails closed on panel collapse, schema failure, gate failure, or budget exhaustion.
- The ordinary execution backend is `active_codex`. Its packet freezes the selected profile and execution settings; enabled plan/pre-execution gates keep mutation readiness false until the visible host records their passing receipts.
- Recursive `codex exec` is disabled unless both config and an explicit command confirmation enable it; it requires the separately reviewed schema-v2 packet hash and refuses pending host gates or any packet mismatch.
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
