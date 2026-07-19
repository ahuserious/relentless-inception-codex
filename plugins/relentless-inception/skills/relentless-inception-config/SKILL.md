---
name: relentless-inception-config
description: Display, validate, and safely change Relentless Inception providers, model seats, fusion profiles, adversarial gates, budgets, privacy controls, and routing. Use when the user asks to configure models/providers, add xAI or OpenRouter, inspect settings, test credentials, change a panel, or tune cost and review policy.
---

# Relentless Inception Configuration

Use the plugin's MCP configuration tools as the settings surface. Codex does not provide a general arbitrary plugin-settings panel, so this skill makes every supported setting inspectable and changeable without exposing credentials.

## Two separate configuration planes

Do not mix these:

1. **Relentless Inception external seats** live in the plugin's JSON configuration. They power `fuse` and `adversarial_gate` through the MCP server.
2. **Native Codex model providers and custom agents** live in user-owned `~/.codex/config.toml` and `~/.codex/agents/*.toml`. Installation is optional and manual. Never edit those files merely because the plugin was installed.

Changing the first plane does not create a native Codex subagent. Changing the second can affect Codex sessions beyond this plugin and therefore requires explicit user consent.

## Inspect before changing

Call:

1. `config_schema` for the complete current schema and descriptions.
2. `config_show` for the redacted merged configuration and active profile.
3. `config_get` for a focused dotted path when a section is large.
4. `config_validate` before and after a proposed change.

The tool schemas and returned JSON are authoritative. Do not assume a setting exists because an older Claude or Grok edition had it.

The major configuration groups are:

- `secret_env_files`: paths to approved owner-only static credential files used to bridge API keys into the plugin subprocess without putting secret values in plugin configuration;
- `providers`: transport type, base URL, endpoint paths, credential environment-variable name, timeouts, retry policy, storage, headers, and model discovery;
- `seats`: provider, exact model id, reasoning effort, output limit, price hints, fallback policy, and provider-routing preferences;
- `native_codex`: host-handoff preferences, preferred executor/reviewer models, named reviewer roles whose agent TOMLs choose provider/model, reasoning-only role boundaries, suggested concurrency, and required pre/post gates; these are instructions for the Codex host, not MCP authority to spawn agents;
- `profiles`: named end-to-end policies, each containing `fusion`, `gates`, `budgets`, `privacy`, `evidence`, `rescue`, `execution`, and `observability` sections;
- `active_profile`: the profile used when a tool call does not override it.

Use `config_schema` to discover any additional settings added by the installed version.

Some settings necessarily span enforcement layers. The MCP runtime enforces provider calls, response schemas, panel liveness, the synthesis reviewer hashes/quorum, core budgets, run hashes, mandatory private response/provenance persistence, and kill checks. The active Codex skill separately invokes every enabled plan, pre-execution, post-execution, final, and summarize stage with its configured `required_evidence`; those host-owned stages are not automatically completed by `fuse`. It also enforces workspace scope, path selection/redaction before egress, mechanical test collection, execution approvals, and native-agent handoff. In v0.1 the `observability` object is a disabled host-export preference surface, not an alternate runtime logger; canonical private run state is always written. Do not imply that a declarative host policy gives the MCP server new Codex permissions.

## Safe mutation workflow

For each requested change:

1. Show the current value with `config_get`.
2. Explain behavioral, cost, and privacy impact.
3. If the change adds data egress, changes a provider `base_url`, raises a hard budget, permits degradation, or changes native Codex files, obtain explicit confirmation. A base-URL change sends that provider entry's credential and selected task material to the new origin.
4. Call `config_set` for the smallest dotted-path change accepted by the tool schema.
5. Call `config_validate`.
6. Call `doctor`.
7. Use `provider_models` to confirm the live model id and `provider_test` to test every new load-bearing seat.
8. Show the redacted final value and state what was not tested.

Never write a credential value through `config_set`. Store only an environment-variable name such as `XAI_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, or a router-specific key name.

Supply the actual API key through the MCP process environment or the plugin's credential-file bridge. The bridge accepts paths from top-level `secret_env_files` and from the path-list environment variable `RELENTLESS_INCEPTION_SECRETS_FILE`. Each path must resolve to a regular file owned by the current user with mode `0600`. Files contain static `NAME=value` entries; blank lines, comments, optional `export ` prefixes, and matching quote pairs are accepted, while shell expansion and command substitution are rejected. The files are parsed as data and are never sourced or executed. Process-environment values take precedence over file values, and credential values are never returned by the settings tools. Use a user-local path outside the repository and do not put an actual key in a native Codex example or agent file.

## Provider choices

### Direct xAI / Grok 4.5

For external panel seats, use provider type `xai_responses`, base URL `https://api.x.ai/v1`, credential environment variable `XAI_API_KEY`, model `grok-4.5`, and reasoning effort `high` unless the user selects `low` or `medium`.

Grok 4.5 reasoning cannot be disabled and accepts only `low`, `medium`, or `high` through the xAI Responses API. Do not carry the old Grok plugin's `xhigh` claim into this configuration. Keep `store` false unless the user gives informed consent to provider-side response storage. If the installed schema exposes prompt-cache affinity, enable it only after explaining that it changes provider-side request handling, not the logical review contract.

### OpenRouter

Use `openrouter_chat` for ordinary routed seats and the exact model id published by OpenRouter, for example `x-ai/grok-4.5`. Use `openrouter_fusion` only when the user deliberately chooses OpenRouter's server-side fusion path.

OpenRouter routing can change the actual upstream provider. Inspect recorded route metadata and constrain the configured `provider_preferences` when provenance, retention, or jurisdiction matters. Never label a request "direct xAI" merely because its model id contains `x-ai/`.

### Trusted or private router

Use `openai_compatible_chat` only when the router implements the required Chat Completions request/response contract. Configure an HTTPS base URL, a dedicated credential environment variable, and environment-backed secret headers. The shipped schema requires HTTPS; do not weaken TLS to make an incompatible or misconfigured router appear healthy.

Run `provider_models` and `provider_test`; a successful `GET /models` alone does not prove structured output, reasoning, timeout, or routing compatibility.

### Direct OpenAI, Anthropic, and other providers

Use `openai_responses` for an OpenAI Responses-compatible endpoint. Use `anthropic_messages` for the native Anthropic Messages API with `ANTHROPIC_API_KEY`; the current adapter maps any non-`none` reasoning setting to Anthropic adaptive thinking rather than pretending every cross-provider effort label is equivalent. The plugin also supports compatible chat routers. Support for these contracts does not imply support for every vendor-specific API—state an unimplemented protocol honestly instead of relabeling it as compatible.

## Building a strong profile

A high-quality profile should:

- use independent seats from more than one model family or provider where available;
- give panelists different roles or context slices rather than relying on temperature for diversity;
- use an economical judge to organize evidence, not to decide the final verdict;
- use the strongest suitable synthesizer available within budget;
- avoid using the artifact author's exact model instance as its only reviewer or synthesizer;
- set `min_live_seats` high enough that provider failure cannot turn a consortium into a single opinion;
- preserve minority findings and require targeted re-review of blind spots;
- configure an atomic hard attempt ceiling plus observed-response stop thresholds for total tokens, wall time, tools, and dollars;
- make degradation opt-in and visible.

Do not promise that more seats always improve quality. Additional seats increase cost and can add correlated noise; use capability diversity and evidence coverage as the reason for each seat.

## Gate settings

Explain these choices when present in the schema:

- reviewer seat list and independence;
- required quorum or `required_passes`;
- fail-closed versus advisory behavior;
- mechanical verification requirements;
- same-artifact hash enforcement;
- maximum review iterations and timeouts.

`required_passes` is a same-run reviewer quorum: for example, `2` means two separate reviewer seats must independently pass the identical artifact SHA-256. It is not two sequential whole-gate rounds. A higher-level release workflow may deliberately run the whole gate twice on the same unchanged commit hash, but that is an additional host policy. Any edit, regenerated output, changed dependency lockfile, or replaced evidence log invalidates the prior quorum and any host-level round count.

## Native Codex opt-in setup

Only offer native setup when the user explicitly wants Grok or another provider to run as a true Codex subagent. A native role may be reasoning-only; it has Codex-managed tools only when the installed provider/tool loop has actually passed compatibility tests.

1. Show the relevant `examples/native-codex-*.toml.example` files.
2. Explain that provider definitions must be merged into user-level `~/.codex/config.toml`; project config cannot override machine-local provider/auth settings.
3. Explain that custom agent files belong under `~/.codex/agents/` or a trusted project's `.codex/agents/`.
4. Keep reviewer agents read-only and keep `[agents].max_depth = 1` unless recursive delegation is deliberately required.
5. Ask before writing either path.
6. After setup, restart or reload Codex and smoke-test a text response. Before exposing any tool, separately pass a streamed two-turn function-call/output continuation.

Native xAI/OpenRouter/trusted-router compatibility is protocol-level and version-sensitive. On the locally tested Codex 0.145/xAI combination, Grok 4.5 works only as a hardened single-turn reasoning reviewer: disable web search, shell, plugins/apps, skill/tool features, and every inherited MCP server, and give the role a complete immutable evidence packet. The first function call can execute but its result continuation fails, so never ask that role to inspect, retrieve, execute, or mutate. If the text smoke test fails too, use the model only as an external MCP panelist.

## Finish

Return:

- active profile;
- provider and seat matrix with requested model, effort, enabled state, and credential env name;
- judge and synthesizer;
- gate policy;
- the attempt ceiling, observed-usage stop thresholds, enforcement mode, and unknown-cost policy;
- storage/retention settings;
- doctor and provider-test results;
- any unverified or degraded capability.

Always redact secrets. A missing key should be reported by environment-variable name only.
