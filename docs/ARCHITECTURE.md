# Architecture

Relentless Inception is a Codex plugin that separates **deliberation** from **execution**. A local MCP server can ask several external models for independent analyses, compare them, produce a generative synthesis, and gate that synthesis. The active Codex session then decides whether and how to execute the resulting handoff under the user's normal workspace permissions.

The design goal is maximum useful intelligence without giving remote model responses ambient authority over the user's machine.

## System boundary

```mermaid
flowchart LR
    U["User and active Codex session"]
    S["Plugin skills"]
    M["Local Relentless Inception MCP server"]
    C["Config, run state, hashes, and budget ledger"]
    P["Independent external API panelists"]
    J["Comparative judge"]
    F["Strong generative synthesizer"]
    G["Adversarial same-artifact gate"]
    H["Execution handoff"]
    W["Codex tools, sandbox, approvals, and workspace"]
    N["Optional native Codex subagents"]

    U --> S --> M
    M <--> C
    M --> P --> J --> F --> G --> H
    H --> U
    U --> W
    U -. "optional Codex delegation" .-> N --> W
```

The arrow from the handoff returns to the active Codex session. It does not continue from the MCP server into the workspace.

## Components

### Codex plugin package

The installable plugin lives under `plugins/relentless-inception/`:

- `.codex-plugin/plugin.json` provides plugin metadata and points Codex to skills and MCP configuration.
- `.mcp.json` starts the local Python MCP server.
- `skills/` contains the user-facing orchestration, configuration, and review contracts.
- `config/default.json` and `schemas/config.schema.json` define the external-seat configuration surface.
- `relentless_inception/` implements configuration, provider adapters, orchestration, persistence, budgets, and execution handoffs.
- `examples/native-codex-*.toml.example` contains opt-in native Codex setup snippets.

Codex plugins can bundle skills and MCP servers, but the current plugin manifest does not install arbitrary native Codex agents. Native agents are separate TOML configuration layers. See the official [Codex plugin documentation](https://developers.openai.com/codex/plugins/build) and [subagent documentation](https://developers.openai.com/codex/multi-agent).

### MCP control plane

The MCP server exposes four groups of tools:

| Group | Tools | Purpose |
|---|---|---|
| Configuration | `config_show`, `config_schema`, `config_get`, `config_set`, `config_validate` | Display and safely update redacted plugin settings. |
| Capability | `doctor`, `provider_models`, `provider_test` | Verify local state, discover live model ids, and probe seats. |
| Deliberation | `fuse`, `adversarial_gate` | Run independent panels, comparison, synthesis, and evidence gates. |
| Lifecycle | `run_status`, `run_abort`, `execution_handoff` | Inspect or stop runs and recover a previously produced handoff. |

The tool schemas returned by the running server are the authoritative argument contract.
The same redacted views are available as MCP resources at `relentless-inception://config`, `relentless-inception://schema`, and `relentless-inception://doctor` for clients that render resources more naturally than tool output.

### Enforcement layers

The configuration schema is intentionally broader than one Python function because several controls belong to the Codex host:

| Layer | Enforces |
|---|---|
| MCP runtime | Provider credentials and calls, normalized responses, panel liveness, judge/verdict structure, artifact hashes, reviewer quorum, atomic pre-dispatch attempt ceilings, observed-response token/tool/known-cost/time stop thresholds, run confinement, and kill checks. |
| Codex skills and active session | Goal/scope confirmation, path selection and redaction before egress, acceptance criteria, mechanical evidence collection, active-workspace execution, approvals, diff review, and post-execution submission. |
| Native Codex runtime | Actual subagent availability, model/provider selection, inherited tools, sandbox, permission mode, and native tool-loop compatibility. |
| Provider/router | Upstream retention, model availability, routing, usage reporting, and remote cancellation behavior. |

A setting in `native_codex`, `privacy`, `evidence`, or `execution` does not grant the MCP process Codex permissions. It gives the host skill an explicit policy to follow and audit.

### Provider adapters

External seats are normalized into one `ModelResponse` shape containing requested model, actual model, provider, route metadata, latency, usage, and response text. The current transport contracts are:

- xAI Responses;
- OpenAI Responses;
- Anthropic Messages;
- OpenAI-compatible Chat Completions;
- OpenRouter Chat Completions;
- OpenRouter's optional Fusion plugin path.

An unsupported proprietary protocol is not made compatible by changing its provider name. Provider behavior and limitations are detailed in [PROVIDERS.md](PROVIDERS.md).

### Run state and budgets

The MCP server stores run artifacts beneath `PLUGIN_DATA` when Codex supplies it, `RELENTLESS_INCEPTION_DATA_DIR` when explicitly set, or `~/.codex/relentless-inception/` as the fallback. A run directory is keyed by a generated run id and binds:

- a SHA-256 of the task;
- a redacted configuration hash;
- an operation-input hash covering the selected profile, context, mechanical evidence, and, for standalone gates, the candidate artifact hash;
- stage status and artifact names;
- requested and actual model provenance;
- input, output, reasoning-detail, cached-input-detail, aggregate input-plus-output, tool, and known-cost accounting;
- count of calls for which cost was unknown;
- raw external responses needed for evidence, synthesis, and deterministic resume;
- the fused result, gate result, and execution handoff.

Resume refuses a run id when the task, configuration, operation, selected profile, context, mechanical evidence, or candidate artifact identity differs. This prevents stale panel or gate artifacts from being reused after evidence changes. Cumulative wall time and cost accounting survive resume. Runtime directories are private (`0700`), files are private (`0600`), writes are atomic, and artifact paths are confined to the run directory. Constructed outbound prompts and hidden reasoning are not persisted as separate artifacts, but verbatim returned model text and normalized response envelopes are. A global or per-run `KILL` file aborts later stages; `run_abort` is the user-facing control.

Opening a run acquires a nonblocking operating-system lease for that run ID and holds it through the orchestration lifecycle. A second process cannot concurrently resume the same run. This is not a machine-wide provider semaphore: distinct run IDs and processes have separate leases and do not share a global coordinator for the configured provider/profile concurrency limits.

Budget enforcement distinguishes what is knowable before network dispatch. Every actual HTTP attempt, including retries and fallbacks, is atomically reserved against `max_calls` before sending. Token, provider-tool, elapsed-time, and dollar usage are known only from a response or local estimate, so those values are stop-before-next-dispatch thresholds. A response and other concurrent requests already in flight can cross them. The resulting stop reason is persisted and blocks resume from launching more work. Unknown cost fails closed by default after recording the completed call. Within an orchestrated run, an HTTP-success response that fails semantic validation is also persisted and accounted before any fallback; if that accounting latches a blocking stop, fallback does not dispatch.

## Deliberation pipeline

### 1. Map

The active Codex session constructs a bounded packet containing the task, acceptance criteria, constraints, and the smallest sufficient evidence bundle. All artifact content is fenced as untrusted data.

### 2. Independent panel

Panel seats receive the same core task before seeing another seat's response. Diversity should come from model/provider families, roles, and context partitions—not merely sampling temperature. Each response must stand alone and distinguish evidence, inference, uncertainty, edge cases, and verification work.

The server may run seats concurrently within configured provider and profile limits. Required and optional panel lists reject duplicate seat names and cannot overlap, so repeated names cannot manufacture independence. A failed or missing seat stays visible. If the panel drops below `min_live_seats`, the run fails closed.

### 3. Comparative judge

The judge sees anonymized panel reports and produces structured diagnostics:

- consensus supported by evidence;
- contradictions;
- partial coverage;
- unique insights;
- minority findings;
- blind spots;
- verification priorities;
- guidance for synthesis.

The judge does not choose a winner or write the final answer. This allows an economical judge without making it the quality bottleneck.

### 4. Generative synthesis

The synthesizer receives the original task, raw panel reports, judge diagnostics, and supplied mechanical evidence. It writes a new coherent answer or plan. It must resolve contradictions by evidence and preserve supported lone-minority findings.

Majority voting and score averaging are explicitly prohibited. Repetition across correlated models is not proof. This design follows the user's [TrustedRouter fusion artifact](https://github.com/ahuserious/trustedrouter-fusion-artifact): retain raw independent evidence, use an economical comparison stage, and spend the strongest suitable model on generative synthesis.

### 5. Adversarial gate

Independent verifier seats try to falsify the exact candidate artifact. Reviewer lists reject duplicate seat names. For artifacts generated by `fuse`, enabled author exclusion compares reviewers with the actual synthesis author, including a native Fusion seat. A gate binds its verdict to the candidate SHA-256. Missing evidence, schema failure, a reproducible mechanical failure, or insufficient live reviewers blocks a pass.

The default maximum-intelligence policy requires a same-artifact quorum: two independent verifier seats must each return `PASS` for the identical candidate SHA-256. Every completed `NEEDS_WORK` or `FAIL` is independently blocking even when the numeric `PASS` quorum is met. This is not two sequential whole-gate rounds. A higher-level release workflow may additionally run the complete gate twice on an unchanged commit hash. Plan, pre-execution, post-execution, final, and summary checkpoints use the same primitive with stage-specific evidence requirements, but they are invoked by the active Codex skill. The MCP `fuse` runtime owns only the synthesis-candidate gate; it never reports a named host lifecycle stage as completed implicitly.

### 6. Execution handoff

The MCP server returns an `execution_handoff` containing only the sections selected by `handoff_include`: the verified synthesis, execution constraints, unresolved minority findings, blind spots, required checks, and/or remaining budget information. It also freezes the selected profile and a hash-bound execution-settings snapshot. Its default backend is the **active Codex session**.

This handoff is a host-workflow packet with two readiness levels. `ready_for_host_workflow` means the synthesis gate passed and the required plan is present. `ready` and `mutation_authorized` remain false while enabled plan or pre-execution gates are pending. Post-execution, final, and summarize gates are recorded as later completion obligations, not incorrectly treated as prerequisites for starting work.

The active session then:

1. re-inspects the real workspace;
2. assembles every configured plan-stage evidence item and invokes `adversarial_gate` over that immutable manifest;
3. assembles every configured pre-execution evidence item and invokes a separate exact-artifact gate;
4. records both host receipts and only then authorizes mutation in visible task state;
5. applies repository instructions and current user scope, requesting approvals through Codex when needed;
6. edits with ordinary Codex tools and runs mechanical checks;
7. invokes configured post-execution, final, and summarize gates with their required evidence.

The handoff is advice plus an evidence contract. Schema v2 hashes the complete persisted packet—including the selected artifacts, instruction, lifecycle state, synthesis receipt, profile, and execution settings—and the recursive CLI refuses any mismatch. It is not an instruction that bypasses newer user input, repository reality, permissions, or pending host gates. Host receipts are append-only workflow evidence; they do not retroactively rewrite the original packet's readiness fields.

## Execution authority

| Actor | Sees local workspace by default | Can run local tools | Can request approvals | Can write files |
|---|---:|---:|---:|---:|
| External API panelist | No | No | No | No |
| Judge or synthesizer API seat | No | No | No | No |
| Relentless Inception MCP server | Only explicit local config/run data and content sent by Codex | Its own bounded provider/state operations | No interactive Codex approval authority | Only plugin/run configuration and state |
| Active Codex session | Yes, within sandbox | Yes | Yes | According to sandbox and user scope |
| Native Codex subagent | According to inherited tools and sandbox | Yes | Only where the runtime can surface approval; otherwise approval-required actions fail | According to inherited/effective sandbox |

This distinction is central. Describing an API panelist as a "Grok subagent" would be misleading. A true Grok-powered Codex subagent requires an opt-in Codex provider, a registered custom-agent role, and a provider/tool-loop combination that passes a compatibility test.

## Optional native Codex agents

Codex 0.145 registers named roles under `[agents.<role>]` in the main configuration. Each role declares its description, a `config_file` path, and optional nickname candidates. The referenced personal or project TOML is a session configuration layer containing model/provider selection, reasoning effort, sandbox, MCP, and instruction settings—not the role metadata itself. The parent turn's live permissions remain authoritative. See the official [configuration schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json) and [subagent documentation](https://developers.openai.com/codex/multi-agent).

Provider definitions and credentials are machine-local configuration. Codex ignores `model_provider` and `model_providers` in project-scoped `.codex/config.toml`, so direct xAI, OpenRouter, or trusted-router definitions must be merged into user-level `~/.codex/config.toml`. No plugin install should do that silently. See [PROVIDERS.md](PROVIDERS.md) and the examples directory.

The native path has a stronger capability than an external panelist only when the provider is sufficiently Responses-compatible: the model participates in Codex's tool loop and Codex executes requested tools under its sandbox. Codex 0.145 custom providers advertise/send namespace tools by default, which xAI rejected with HTTP 422 in a local probe. Disabling web search, shell, plugins/apps, inherited MCP servers, and other tool-producing features allowed a direct foreground Grok 4.5 text turn, but a real custom-agent spawn still failed with HTTP 422 because its request did not match xAI's accepted `ModelInput` variants. In a separate probe Codex executed Grok's first shell function call, but xAI rejected the continuation because the returned compaction blob could not be decoded. The former Chat Completions wire is not available as a workaround. Native Grok is therefore not an operational subagent path on the tested build; external plugin seats supply Grok deliberation and review. Provider defaults are visible in Codex's [provider source](https://github.com/openai/codex/blob/main/codex-rs/model-provider/src/provider.rs).

The plugin's external xAI seats use their own Responses adapter and remain operational, including explicitly configured xAI-hosted tools. Native Grok should receive a complete immutable evidence packet from its parent and must not call tools until a full two-turn function-call continuation passes on the installed Codex/xAI versions.

For any native custom provider, compatibility must be tested with streaming and a two-turn function-call continuation, not inferred from a successful text completion.

## Failure, degradation, and recovery

- Network, authentication, quota, schema, and semantic failures are recorded per seat.
- Retries are bounded. Repeated open-ended debate is not a recovery strategy.
- Optional seats can fail without erasing their failure, but the configured minimum live panel still applies.
- Model fallback requires explicit seat/profile permission and retains requested-versus-actual provenance.
- Budget exhaustion stops and reports; it does not silently lower quality or continue unmetered.
- `run_status` is the source of truth for a persisted run. A task/config/full-input hash mismatch prevents accidental resume with stale context, evidence, profile, or artifact identity.
- This release does not promise an always-running watchdog or unattended daemon. Continued Codex execution still depends on the active task and its normal lifecycle.

## Design lineage

The Codex implementation keeps the strongest ideas from the user's prior systems while dropping host-specific assumptions:

| Source | Preserved idea | Codex adaptation |
|---|---|---|
| Claude/Grok Relentless Inception | Plan/phase/summary gates, persistent evidence, rescue-oriented checkpoints | MCP run state plus explicit active-Codex handoffs; no unverified TUI relay or hook claim. |
| Batch Create Eval | Decomposition, per-unit verification, simulated-user shakedown | The active Codex session owns work units and real tests. |
| Gigaprompt | Evidence bar, context preservation, repeated adversarial checks | Hash-bound reviewer quorums, with optional repeated whole-gate release rounds. |
| Exaflop | Cross-persona divergence and hard resource guards | Configurable seats and profiles with bounded calls, tokens, time, and dollars. |
| TrustedRouter fusion artifact | Strong synthesizer, economical judge, minority preservation | Client-orchestrated panel → judge → synthesizer, with optional routed seats. |

## Deliberate limitations

- External seats do not browse, use MCP tools, run shell commands, or inspect files unless the active Codex session first supplies the resulting evidence.
- Provider-advertised capabilities in configuration are descriptive; the current external-seat loop is text/structured-output deliberation, not unrestricted remote tool execution.
- Plugin install does not install API keys, user-level Codex providers, or custom agents.
- OpenRouter/trusted-router upstream selection can weaken independence unless routing is constrained and recorded.
- A multi-model verdict is still fallible. Mechanical tests and user authority remain higher-order controls.
