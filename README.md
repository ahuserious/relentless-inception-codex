# Relentless Inception for Codex

Relentless Inception is a Codex-native plugin for bounded multi-model deliberation, generative fusion, adversarial review gates, and verified execution handoff. It is built for cases where the cost of a wrong answer is materially larger than the cost of several frontier-model calls.

![Original Relentless Inception fusion gate with Map and Panel complete and Fuse in progress](docs/img/fusion-panel-fuse.png)

> The screenshot is a lineage capture from the original Claude-hosted edition. It shows the same Map → Panel → Fuse mental model, but its Fable/Claude seat labels are not the Codex plugin's current models or UI. See the [Codex-native fusion walkthrough](docs/FUSION_DELIBERATION.md) for the exact mapping.

The default pipeline is:

1. independent, identity-hidden model seats with distinct persona/context lenses (the host must pre-partition material when seats need different data);
2. deterministic evidence supplied by Codex or the user;
3. a structured comparative judge that diagnoses rather than decides;
4. a fresh strongest-model synthesis that must preserve supported minority findings;
5. independent reviewers bound to the exact artifact SHA-256;
6. a persisted host-workflow packet whose enabled plan/pre-execution gates must pass before the active Codex session authorizes mutation.

The shipped maximum-intelligence profile is deliberately frontier-only: the active Codex host, native reviewers, and execution handoff use `gpt-5.6-sol`; every direct xAI panel, judge, synthesizer, and adversarial reviewer uses exact `grok-4.5` at high effort. Router and provider-direct alternatives remain displayable and configurable, but are disabled and never selected as implicit fallbacks.

## Why use the Codex edition

| Benefit | What it means in practice |
|---|---|
| Codex keeps execution authority | External models deliberate over a bounded packet; the active Codex session owns filesystem access, approvals, implementation, and tests. |
| Cross-family intelligence is available | The default combines a GPT-5.6 Sol host with direct Grok 4.5 deliberation. OpenAI, Anthropic, OpenRouter, native OpenRouter Fusion, and compatible trusted routers can be explicitly added. |
| Synthesis is not voting | A comparative judge maps agreement and conflict, then a fresh strongest configured seat writes a new result while retaining supported minority findings. |
| Review binds to bytes | Reviewers gate the exact artifact SHA-256 and byte-identical mechanical evidence; a passing review of an earlier draft cannot authorize a later one. |
| Controls survive prompt failure | Provider dispatch, schema checks, receipt chains, budgets, cancellation, resume, and handoff readiness are enforced in the MCP runtime. |
| Every major setting is inspectable | Providers, seats, routing, models, personas, gates, budgets, privacy, rescue, evidence, and execution policy are exposed through the validated schema. |

Use this plugin for expensive mistakes, long or adversarial builds, high-stakes architecture, or work that needs a retained audit trail. A single ordinary Codex turn is usually the better tool for small edits, interactive teaching, or tasks where several paid frontier calls would not change the decision.

This is a new runtime-backed implementation inspired by [`ahuserious/relentless-inception-grok`](https://github.com/ahuserious/relentless-inception-grok), not a mechanical host rename. The source Grok plugin is principally an orchestration contract; this port implements the call graph, configuration enforcement, persistence, budgets, kill switch, resume semantics, structured gates, and Codex packaging in code.

## Status

Version `0.1.4` is an alpha suitable for deliberate, cost-aware use. Direct xAI Grok 4.5, OpenAI Responses, Anthropic Messages, OpenRouter, OpenRouter native Fusion, and generic OpenAI-compatible/TrustedRouter transports are supported. External API seats can use provider-hosted tools when explicitly configured, but they never receive Codex filesystem access.

Version 0.1.4 retains the v3 run ledger introduced in 0.1.1 and intentionally cannot resume pre-0.1.1 run state. Its stricter receipt checks can also reject incomplete 0.1.1 crash or native-fallback artifacts; those directories remain preserved for audit, but the work requires a new run ID. See the [run-state compatibility boundary](docs/ARCHITECTURE.md#run-state-compatibility).

Codex plugins currently do not auto-register plugin-owned native agents. The bundled skills coordinate compatible native Codex reviewers/executors through the active host; opt-in provider/agent TOML templates are retained for explicit compatibility retesting. On Codex 0.145, a direct foreground Grok 4.5 text turn succeeded only after every tool surface was removed, but an actual spawned custom-agent turn still failed at xAI with HTTP 422 because the Codex subagent input did not match xAI's accepted `ModelInput` variants. A separate tool-result continuation also failed with a compaction-blob error. The shipped default therefore registers no native Grok role. The active Codex host and execution handoff use `gpt-5.6-sol`; every operational xAI fusion and gate seat uses exact `grok-4.5`, with no weaker automatic fallback.

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

### The fusion lifecycle

```text
goal + bounded evidence
        ↓
independent panel reports
        ↓
anonymous comparative diagnosis
        ↓
fresh minority-preserving synthesis
        ↓
exact-hash adversarial gate
        ↓
Codex plan/pre-execution gates → implementation → post/final gates
```

The judge is diagnostic, not sovereign: it identifies consensus, contradictions, partial coverage, unique insights, minority findings, blind spots, and verification priorities. The synthesizer receives the raw panel reports and writes a fresh answer; it may not decide by majority vote or erase a supported lone finding. The active Codex host then decides what evidence to collect and performs only the mutations authorized by the normal Codex sandbox and approval flow.

The maximum-intelligence profile starts with three role-diverse direct-xAI `grok-4.5` panel seats, a `grok-4.5` judge, a `grok-4.5` synthesizer, and two exact-artifact `grok-4.5` reviewers. The execution handoff remains `gpt-5.6-sol` at `xhigh`. Adding an explicitly enabled GPT, Claude, or routed provider seat creates broader model-family diversity; several calls to Grok 4.5 alone remain multi-agent deliberation, not cross-model fusion.

For the annotated screenshots, configuration-to-seat mapping, failure behavior, and the reason fusion differs from voting, read [Fusion Deliberation](docs/FUSION_DELIBERATION.md).

### Inspect before spending

Start with configuration and diagnostics; provider calls are opt-in and can be billable:

```text
config_show
config_schema
config_validate
doctor
provider_models
```

Use `provider_test` only when you intend a small paid completion probe. It refuses OpenRouter native Fusion because that path can fan out into several calls. The default caps are ceilings, not a spending forecast; inspect `profiles.maximum_intelligence.budgets` and lower them for the task.

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

## What this edition carries forward

The original Relentless Inception report described an unattended orchestrator built from four complementary ideas. The Codex implementation keeps the durable parts while replacing shell- and prompt-only enforcement with a host-specific runtime:

| Lineage | Codex edition |
|---|---|
| Relentless Inception | phased work, explicit checkpoints, bounded rescue, continuation state, and completion based on proof rather than confidence |
| Batch Create Eval | independent work units, requirement traces, realistic shakedowns, and a hard distinction between task reward and harness acceptance |
| Gigaprompt | evidence-backed completion, stable context packets, explicit handoffs, and verification before declaring done |
| Exaflop | deliberately different expert lenses, parallel first passes, bounded escalation, and hard time/cost limits |
| TrustedRouter/OpenRouter fusion research | generative synthesis over voting, raw-panel preservation, strongest-seat synthesis, and protection for lone-correct minority evidence |

Planning styles such as staff-up or lawyer-up and execution styles such as proof loops remain useful host-level prompting patterns, but they are not hidden runtime modes in this release. The enforced surface is the published configuration schema and MCP state machine.

## Evidence and scope of claims

The immutable [limited-cost fusion artifact](https://github.com/ahuserious/codex-fusion-artifact/tree/limited-cost-2026-07-20) publishes curated jigs, exact safe response receipts, harness verifier outputs, SHA-256 manifests, and the failures that bounded this release campaign.

The strongest current live proof is `codex-0144-frontier-smoke-001`: ten direct-xAI calls, every requested and returned as exact `grok-4.5`; an initial 1/2 gate forced one amendment; the amended artifact passed 2/2; the ledger reports $0.268100 and zero unknown-cost calls. The campaign was deliberately limited by API cost and is not a statistically powered benchmark.

One historical Terminal-Bench `fix-git` trace earned reward 1.0, but it predates the final all-Grok-4.5 defaults and its recorded contract drifts from the current validator. One DeepSWE/Pier trace earned reward 0 and stopped before fusion. These outcomes remain visible rather than being converted into release passes. OpenRouter was not called live because no funded credential was available; mocked adapter coverage is not a live-provider claim.

See [release evidence](docs/RELEASE_EVIDENCE.md) for the exact matrix and [benchmark protocol](docs/BENCHMARK_PROTOCOL.md) for the separation between task reward, fusion gates, and evidence-contract acceptance.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q plugins/relentless-inception
```

The unit suite is network-free. Live provider probes are intentionally separate because they can incur API cost.

## Documentation map

| Guide | Use it for |
|---|---|
| [Fusion Deliberation](docs/FUSION_DELIBERATION.md) | visual mental model, exact Codex topology, minority preservation, gates, and lineage screenshots |
| [Configuration](docs/CONFIGURATION.md) | every provider, seat, fusion, gate, budget, privacy, rescue, evidence, and execution field |
| [Architecture](docs/ARCHITECTURE.md) | trust boundaries, ledgers, receipts, resume, and execution authority |
| [Providers and Models](docs/PROVIDERS.md) | direct providers, OpenRouter, trusted routers, pricing, tools, and native-agent compatibility |
| [Security and Privacy](docs/SECURITY.md) | secrets, egress, injection fencing, workspace safety, and cost-denial controls |
| [Release Evidence](docs/RELEASE_EVIDENCE.md) | precisely observed live behavior and claim boundaries |
| [Benchmark Protocol](docs/BENCHMARK_PROTOCOL.md) | Terminal-Bench/DeepSWE acceptance and why task reward is separate from fusion validity |

## License

No license has been selected yet. The repository owner should choose one before public distribution. See [NOTICE.md](NOTICE.md) for provenance.
