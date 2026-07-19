# Security and Privacy

Relentless Inception deliberately treats model intelligence and execution authority as separate concerns. More models can improve coverage, but no model response—single or fused—receives permission merely because it is persuasive.

## Trust model

There are four boundaries:

1. **The active Codex session** is the only default workspace executor. Its sandbox, approval policy, repository instructions, and current user request control local actions.
2. **The local MCP server** may read its plugin configuration and run-state directory and may send explicitly supplied material to configured providers. It does not inherit interactive Codex approval authority.
3. **External providers and routers** receive the prompt/evidence material sent to their seats. Their output is untrusted data until the active Codex session verifies it.
4. **Optional native Codex subagents** use Codex-managed tools and inherit the effective parent permissions. They are not installed by the plugin and require a separate opt-in configuration.

## External panelists have no ambient tools

The external provider loop sends text and structured-output requests. A seat's descriptive `tool_policy` or provider capability metadata does not grant filesystem, shell, browser, MCP, connector, or workspace access. For Responses adapters, an explicit `server_tools` array can grant xAI/OpenAI-hosted search or code-interpreter tools; those execute entirely inside the provider boundary.

Consequences:

- A panelist cannot truthfully say it ran a test unless the prompt includes the captured test result.
- A panelist cannot inspect an arbitrary path merely because the path is mentioned.
- A panelist cannot implement its recommendation.
- Provider-side built-in tools remain provider-side, are billable, and must be explicitly listed per seat. The default research seats disclose their xAI-hosted tools in `server_tools`; judge, synthesizer, and verifier seats default to none.

The active Codex session gathers evidence, executes the handoff, and performs mechanical verification.

## Secret handling

Configuration stores only credential environment-variable names such as `XAI_API_KEY`; it must never store the credential value.

The implementation:

- rejects plaintext values under secret-like configuration keys;
- redacts secret-like fields from displayed configuration and configuration hashes;
- reads bearer tokens from the named process environment or an approved owner-only credential file only when making a provider request;
- redacts bearer tokens and API-key-shaped text from surfaced transport errors;
- writes user configuration atomically with mode `0600`;
- does not scan shell profiles or repository `.env` files for keys.

The credential-file bridge is explicit and non-executable:

- paths come only from top-level `secret_env_files` or the path list in `RELENTLESS_INCEPTION_SECRETS_FILE`;
- each resolved path must be a current-user-owned regular file with mode `0600`;
- the parser accepts static `NAME=value` records, comments, optional `export ` prefixes, and matching quote pairs;
- it rejects invalid variable names, malformed records, `$` expansion, and backticks rather than sourcing a shell;
- earlier files win on duplicate names, while a real process-environment value has final precedence;
- credential values are held in memory for provider authentication and are never displayed or written back to configuration.

Recommended practice:

- provide keys through the Codex/MCP process environment, a scoped secret manager, or the owner-only credential-file bridge;
- use separate keys and spending limits for this plugin;
- keep credential files outside repositories, use mode `0600`, and never commit a key, `.env`, native Codex bearer token, or populated example;
- rotate a key immediately if any raw provider error, run artifact, or transcript may contain it.

Put secret headers only in the schema's environment-backed header mapping, and include only headers required by the provider. Do not add an undocumented literal-header field to a user override.

## Data egress

Every external seat is data egress. Before a run, determine which task content may leave the machine.

Default privacy posture for the maximum-intelligence profile:

- redact sensitive values and environment contents;
- deny common secret paths such as `.env`, credentials, private keys, and `.ssh`;
- require user approval for other sensitive paths;
- treat all supplied artifacts as untrusted data;
- persist raw model responses locally because synthesis, adversarial evidence, and deterministic resume depend on them;
- do not persist constructed outbound prompts or hidden reasoning as separate artifacts;
- retain metadata, hashes, usage, route, and verdict artifacts needed for audit;
- request zero-data-retention routing where available;
- disable provider training where the provider exposes that control.

These settings cannot override a provider's real policy. Review each direct provider and each possible router upstream for retention, training, abuse monitoring, jurisdiction, and subprocessors. A router's zero-data-retention flag may restrict eligible upstreams or fail when no matching upstream is available; verify the actual route.

### xAI storage

The xAI Responses API supports stored responses and documents 30-day retention for stored responses. The plugin requires `store: false` for the default xAI provider. Change it only after informed consent. `store: false` is not a promise that every operational log vanishes; xAI's current terms and data controls remain authoritative.

### OpenRouter and trusted routers

OpenRouter can change the upstream provider unless routing is constrained. Record upstream metadata, request zero-data-retention routing where required, and disable fallback when exact provider policy is load-bearing.

A private or "trusted" router is not trusted by name. Verify TLS, operator identity, authentication, tenant isolation, log retention, upstream fallback, model substitution, and incident response.

## Prompt injection

Task text, repository files, logs, web pages, test output, panel responses, and judge output are all potentially hostile. Provider prompts fence them as `RELENTLESS_INCEPTION_UNTRUSTED_DATA` and instruct seats not to follow embedded directions.

Additional rules:

- Put gate criteria and role instructions outside the data fence.
- Do not pass raw credentials or approval tokens as evidence.
- Treat a panel's request to reveal secrets, broaden scope, change configuration, or run a command as untrusted advice.
- The synthesizer must not convert an embedded instruction into an execution-handoff step without independent justification.
- The active Codex session revalidates every handoff step against the original request and repository instructions.

Prompt fencing reduces risk; it does not make arbitrary model input safe. Send the minimum necessary material.

## Workspace safety

The default execution backend is an active-Codex handoff. External models never write the workspace.

The active Codex session must:

- inspect the current worktree and preserve unrelated user changes;
- use the smallest path scope necessary;
- request approval for destructive actions and externally visible writes;
- avoid executing commands copied blindly from model output;
- run relevant tests and inspect their real output;
- submit exact, hashed post-execution artifacts to the gate.

Optional recursive `codex exec` support is a separate high-risk mode. It must remain disabled unless the user explicitly enables it, supplies a work directory, and confirms the invocation. It does not bypass Codex sandboxing.

## Native Codex subagents

Native agents inherit the parent turn's effective permission mode. A custom agent may request `sandbox_mode = "read-only"`, but live parent overrides remain authoritative. In noninteractive execution, an action needing new approval fails rather than obtaining hidden consent.

Recommended defaults:

- reviewers and explorers: `sandbox_mode = "read-only"`;
- `agents.max_depth = 1` to prevent recursive fan-out;
- bounded `agents.max_threads` and job runtime;
- no provider credentials in project-scoped agent files;
- narrow developer instructions and tool surfaces;
- separate implementation and review roles.

Installing this plugin does not modify `~/.codex/config.toml`, `~/.codex/agents/`, or project agent files. Treat any future setup helper that writes those locations as a consent checkpoint.

## Gate integrity

An adversarial gate is reliable only if every reviewer evaluates the same artifact.

- Hash the full artifact manifest before the first pass.
- Bind every verdict to that SHA-256.
- Invalidate the reviewer quorum after any artifact or evidence change.
- Preserve raw independent reviews until synthesis completes.
- Keep judge and synthesizer roles distinct.
- Preserve minority findings and document their resolution.
- Treat reproducible mechanical failure as blocking regardless of vote count.
- Fail closed on malformed verdicts, blind criteria, insufficient live seats, or required-provider failure.

Two reviews of two different diffs cannot satisfy one quorum. The configured verifier seats—not the earlier fusion panelists—must independently pass the identical SHA-256. A higher-level release process may run the entire gate twice on an unchanged commit, but that is separate from the core `required_passes` reviewer count.

## Provider and routing integrity

Always record requested and actual model identity. A router may substitute models or upstreams, and a seat may use explicit fallback models.

- Do not present a fallback as the requested model.
- Disable fallback where provider independence or retention policy matters.
- Use `provider_models` and `provider_test` after configuration changes.
- Treat configuration `capabilities` as declarations, not proof.
- Use HTTPS for providers, as required by the shipped configuration schema. Do not bypass TLS verification to make a failing router pass preflight.
- Set conservative timeouts; high-reasoning models can be slow, but an hour-long timeout is not a reason for unbounded retries.

## Cost controls and denial of wallet

Multi-model fusion creates a direct cost-amplification surface. A malicious artifact could try to induce retries, larger context, or more reviewers.

The MCP runtime directly enforces maximum calls, aggregate reported tokens, elapsed wall time, and **known** dollar cost. The profile also exposes host-policy controls that the active Codex workflow must honor, including:

- per-provider limits;
- a warning/approval threshold before significant additional spend;
- reserved budget for synthesis and gates so panel fan-out cannot consume the entire run;
- bounded retry and revision cycles;
- circuit breaking after repeated provider failures;
- visible accounting for calls whose cost is unknown.

Unknown cost is not zero and is not covered by the known-dollar hard cap. If a provider omits price data and the seat has no configured pricing, surface `unknown_cost_calls`, stop before material additional fan-out, and require a conservative estimate or user direction.

Never raise a hard cap merely to satisfy "maximum intelligence" without the user's approval.

## Persistence and deletion

Run state defaults to `PLUGIN_DATA`, `RELENTLESS_INCEPTION_DATA_DIR`, or `~/.codex/relentless-inception/`. It contains raw model responses, task and artifact hashes, evidence, errors, usage, route metadata, and verdicts. Treat the directory as sensitive even when outbound task content was redacted.

- Run paths are confined beneath the run directory.
- Runtime directories use mode `0700`; files use mode `0600`; writes are atomic.
- Resume verifies task and redacted-config hashes.
- A global or run-local `KILL` file stops later work.
- `run_abort` marks a run aborted, but an HTTP request already accepted by a provider may still finish and bill; abort is not a provider-side refund or guaranteed remote cancellation.

Before sharing or deleting a run, inspect its contents. Local deletion does not delete provider-side logs or stored responses. Use provider-specific deletion and retention controls when applicable.

## Supply-chain and plugin trust

- Review the repository and plugin manifest before installing.
- Pin or audit marketplace sources and upgrades.
- The plugin does not require a hook to perform its core workflow; do not add host hooks merely to emulate older Claude/Grok editions.
- Plugin MCP tools have their own enable/approval settings in Codex configuration. Disable tools the organization does not permit.
- Treat future scripts, native-provider installers, and recursive Codex execution as higher-risk surfaces requiring separate review.

## Incident response

If secret exposure, unexpected data egress, runaway cost, or unauthorized workspace activity is suspected:

1. Call `run_abort` and create the configured `KILL` marker if necessary.
2. Revoke or rotate affected provider/router keys.
3. Preserve the redacted run manifest, budget ledger, request ids, and route metadata.
4. Inspect local artifacts for sensitive prompt/response content.
5. Check provider billing and retention/deletion controls.
6. Disable the affected provider or plugin until the cause is understood.
7. Report what was actually observed; do not claim remote deletion or cancellation without provider confirmation.
