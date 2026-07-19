# Configuration

Relentless Inception ships with a strict `maximum_intelligence` profile: independent high-effort Grok analysis, structured comparison, strong generative synthesis, fail-closed adversarial gates, bounded API spend, private local run artifacts, and an explicit handoff to the active Codex session for execution.

The configuration is intentionally complete and displayable. Use the MCP configuration tools or edit a small user override; do not edit the shipped default because plugin upgrades may replace it.

Configuration spans two enforcement layers. The MCP runtime directly enforces provider enablement, model calls and fallbacks, retries/circuits, concurrency, liveness and quality floors, identity hiding, strict judge/verdict schemas, artifact hashes, reviewer quorum, amendment count, core budgets, atomic state, resume, and kill checks. The active Codex skill enforces workspace-sensitive policies: evidence collection, path redaction before egress, named plan/pre/post/final checkpoints, projected-spend approval, native-agent selection, execution sandbox/approval behavior, tests, diff review, and shipping criteria. A field under `native_codex`, `privacy`, `evidence`, gate `stages`, execution, or shipping policy does not grant the MCP subprocess new Codex permissions. Fields such as `fallback_seats` and adaptive specialist strategy are available to the host workflow; the runtime itself performs model fallbacks and bounded synthesis amendments, not arbitrary cross-provider seat spawning.

## Files and precedence

The runtime deep-merges configuration in this order:

1. [`plugins/relentless-inception/config/default.json`](../plugins/relentless-inception/config/default.json)
2. the file named by `RELENTLESS_INCEPTION_CONFIG`, when set
3. otherwise `PLUGIN_DATA/config.json`, `RELENTLESS_INCEPTION_DATA_DIR/config.json`, or `~/.codex/relentless-inception/config.json`

Objects merge recursively. Arrays replace the entire inherited array. A user override therefore needs to contain only changed fields, but changing `fusion.panel`, a fallback list, or any other array must restate the complete desired list.

The authoritative field catalog is [`plugins/relentless-inception/schemas/config.schema.json`](../plugins/relentless-inception/schemas/config.schema.json). The MCP tools expose the same surface:

- `config_show` returns the merged, redacted configuration.
- `config_schema` returns field descriptions and constraints.
- `config_get` reads one dotted path.
- `config_set` atomically updates one user override path and writes it with mode `0600`.
- `config_validate` runs structural and cross-reference validation.

`schema_version` must remain `1` for this release.

## Credentials

Configuration stores environment-variable names, never credential values:

```json
{
  "providers": {
    "xai_direct": {
      "api_key_env": "XAI_API_KEY"
    }
  }
}
```

Set credentials in the environment inherited by Codex and the plugin MCP process. The optional top-level `secret_env_files` array can name explicit private files containing static `NAME=value` lines. `RELENTLESS_INCEPTION_SECRETS_FILE` supplies one additional path without changing JSON.

Credential-file safeguards are deliberately narrow:

- the path must resolve to a regular file owned by the current user;
- permissions must be `0600`;
- only static `NAME=value` assignments are accepted;
- shell expansion, command substitution, and executable syntax are rejected;
- values are never returned by configuration tools or written to run metadata.

The shipped default is `"secret_env_files": []`. Installation may create a user-local override, but a machine-specific path never belongs in the repository. `header_env` similarly maps an HTTP header name to an environment-variable name; do not use literal authorization headers.

## Default model topology

The required client-orchestrated path is:

| Stage | Default seat | Provider/model | Effort | Tools |
|---|---|---|---|---|
| Panel | `grok45_researcher` | xAI direct `grok-4.5` | high | xAI-hosted `web_search`, `x_search`, `code_interpreter` |
| Panel | `grok45_adversary` | xAI direct `grok-4.5` | high | xAI-hosted `web_search`, `x_search` |
| Panel | `grok43_constraint_auditor` | xAI direct `grok-4.3` | high | none |
| Judge | `grok43_judge` | xAI direct `grok-4.3` | low | none |
| Synthesizer | `grok45_synthesizer` | xAI direct `grok-4.5` | high | none |
| Gate | `grok45_verifier` plus constraint auditor | xAI direct | high | none |

Provider-hosted server tools are not Codex tools. They cannot read the local workspace, run repository tests, use MCP/connectors, or mutate files. The active Codex session must gather local evidence such as diffs and test output and supply that bounded evidence to external seats. Judge, synthesizer, and gate calls remain tool-less by default so their outputs are based on the exact supplied artifact.

Two optional OpenRouter panel seats use current catalog IDs:

- `openai/gpt-5.6-sol-pro`, with `openai/gpt-5.6-sol` available only as an explicitly enabled fallback;
- `anthropic/claude-opus-4.7`, with a recorded Sol fallback that is also disabled by default.

They are included only after both the seat and `openrouter` provider are enabled. `max_panel_seats: 5` bounds fan-out, so the first two enabled optional seats join the three required xAI seats. The TrustedRouter seat is demonstrated in a separate mixed-panel example because all three optional seats cannot fit simultaneously under that cap.

## Providers

Every provider has an independent enable flag, base URL, credential environment variable, timeouts, retries, concurrency cap, retryable HTTP statuses, declared capabilities, and optional routing controls. The current stdlib HTTP adapter uses `request_timeout_seconds` as the full request/socket timeout; `connect_timeout_seconds` remains a displayed portability hint for adapters that can separate connection and read timeouts.

| Type | Protocol | Intended use |
|---|---|---|
| `xai_responses` | xAI Responses | Direct Grok 4.5/4.3 seats and xAI server tools |
| `openai_responses` | OpenAI Responses | Direct OpenAI models |
| `anthropic_messages` | Anthropic Messages | Direct Claude models |
| `openai_compatible_chat` | Chat Completions-compatible | TrustedRouter, a private gateway, or another compatible provider |
| `openrouter_chat` | OpenRouter Chat Completions | Per-seat routed models and provider preferences |
| `openrouter_fusion` | OpenRouter Fusion plugin | Optional server-managed Fusion fast path |

Disabled providers remain visible so users can configure them without changing the schema. Enabling a provider does not prove that the key, model, route, reasoning effort, or structured output is live; run `doctor`, `provider_models`, and `provider_test` after changes.

Common controls include:

- `connect_timeout_seconds` and `request_timeout_seconds`;
- `max_retries`, `max_concurrency`, and `retry_statuses`;
- endpoint paths such as `responses_path`, `messages_path`, `chat_path`, and `models_path`;
- `store: false` for xAI/OpenAI provider-side response storage;
- `prompt_cache_key_enabled` for cache affinity, which is not a privacy guarantee;
- `header_env` for additional environment-backed headers;
- `capabilities`, which are declarations to compare with live behavior. The built-in `provider_test` is a small completion probe, not a full tools/streaming/continuation certification suite.

### Direct xAI pricing

The fallback cost estimator uses the current published base rates when xAI does not report a request cost:

| Model | Input / 1M | Cached input / 1M | Output / 1M |
|---|---:|---:|---:|
| Grok 4.5 | $2.00 | $0.30 | $6.00 |
| Grok 4.3 | $1.25 | $0.20 | $2.50 |

Grok 4.5 documents higher pricing above 200,000 input tokens. The shipped seats set `base_rate_input_limit_tokens: 200000` and `above_base_rate_behavior: unknown_cost_fail_closed`. When the provider reports exact cost, that value wins. Otherwise the runtime refuses to treat the lower base rate as authoritative above the threshold. You can instead configure explicit `long_context_*_per_million_usd` rates or deliberately select `use_base_rate`, but the latter is not a safe upper bound.

Pricing changes over time. Verify rates against the [xAI model catalog](https://docs.x.ai/developers/models) before relying on configured estimates.

### OpenRouter provider routing

The provider-level `provider_preferences` object exposes OpenRouter's current controls:

- `order`, `only`, `ignore`, and `allow_fallbacks`;
- `require_parameters` so structured-output or reasoning parameters are not silently dropped;
- `data_collection`, `zdr`, and `enforce_distillable_text`;
- `quantizations`;
- `sort`, including cross-boundary `partition: "none"`;
- soft throughput/latency preferences;
- `max_price` hard ceilings.

Throughput and latency preferences reprioritize routes; they do not exclude slow routes. Use hard allow/deny and privacy fields for policy boundaries. A seat may provide a complete `provider_routing` override.

`enforce_distillable_text` is false by default. OpenRouter defines it as a filter for models whose authors permit their outputs to train another model; it is not a general privacy control and excludes several frontier models. Enable it only when the output is actually destined for a training or distillation dataset. The separate `data_collection: "deny"` and `zdr: true` defaults remain the privacy controls.

OpenRouter's `models` fallback behavior responds to routing-level errors. It does not catch a successful HTTP response containing empty text, invalid JSON, an unexplained refusal, or a weak answer. Relentless Inception applies its semantic quality floor and rescue policy after transport success.

Record actual model/provider route metadata. Never label a fallback result as the requested model.

## Seats

A seat is one fresh API request template. Important fields are:

- `provider` and exact provider-native `model`;
- `role`: `panel`, `judge`, `synthesizer`, or `verifier`;
- `persona` and `context_bundle` for meaningful diversity;
- `reasoning_effort` or an explicitly supported reasoning token limit;
- visible output and timeout limits;
- `tool_policy` and `server_tools`;
- a named structured-output contract;
- `allow_model_fallbacks`, `fallback_models`, and cross-provider `fallback_seats`;
- optional router controls and fallback pricing.

Temperature is `null` for the default reasoning seats. Do not use temperature as the primary diversity mechanism: benchmark results found that changing temperature did not remove correlated blind spots. Use different capable models, adversarial roles, and evidence bundles.

Fallback models should not have a higher unknown price than the seat's estimate. The shipped `maximum_intelligence` profile disables every model fallback so an unavailable requested model fails visibly instead of becoming a silent quality downgrade. The fallback lists remain displayable configuration: an operator can explicitly enable a cheaper Grok 4.3 or routed-model fallback in a less strict profile, with the requested and actual model retained in provenance.

## Fusion

`profiles.<name>.fusion.engine` selects:

- `client_orchestrated` — canonical path with arbitrary direct providers, raw response preservation, individual validation, and per-seat provenance;
- `openrouter_native` — optional OpenRouter-managed fast path using `native_fusion_seat`.

The client path performs:

1. independent panel answers before peer exposure;
2. identity anonymization and deterministic order randomization;
3. strict structured comparative diagnosis;
4. a fresh strongest-seat generative synthesis using raw panel reports as primary evidence;
5. independent verification.

The following invariants are fixed true in schema because turning them off would no longer be this fusion method:

- independent first pass and initial-response preservation;
- no majority vote or score averaging;
- supported minority findings preserved;
- raw panels remain available to synthesis;
- open-ended repeated debate is forbidden.

The default requires two live responses but also sets `allow_degradation: false`. Consequently every required panel seat, and every optional seat that was actually enabled and launched, must return a valid response. `min_live_seats` remains useful for custom profiles, but it does not convert a missing configured seat into success under `maximum_intelligence`.

`quality_floor` directly detects short/empty responses, leaked tool markup, and common unexplained boilerplate refusals; the active Codex evidence policy decides whether a longer report contains substantive claims. `adaptive_escalation` describes the host's bounded response to blind spots, contradictions, schema failures, empty responses, or mechanical failures. The runtime automatically performs only configured model fallbacks and bounded post-gate synthesis amendments; it does not invent or spawn an unlisted specialist seat.

### Native OpenRouter Fusion

The default keeps native Fusion disabled. Enabling it requires:

- provider `openrouter_native_fusion`;
- seat `openrouter_native_fusion_seat`;
- `fusion.engine: "openrouter_native"`;
- `fusion.native_fusion_seat: "openrouter_native_fusion_seat"`.

The current example uses `x-ai/grok-4.5`, `openai/gpt-5.6-sol-pro`, and `anthropic/claude-opus-4.7`, with Opus 4.7 as the comparative model. Verify every ID with `provider_models`; catalog IDs are temporal.

OpenRouter's current Fusion parameters live inside the seat's `fusion` object: `analysis_models`, comparative `model`, `preset`, `max_tool_calls`, `max_completion_tokens`, `reasoning`, and panel `temperature`. Legacy top-level `reasoning_effort`/temperature examples for the Fusion plugin are stale.

`tool_choice` is required in the profile display surface so the outer request cannot silently skip mandatory Fusion. Native Fusion can reduce local visibility into inner calls and does not document per-inner-seat upstream provider preferences. Use client orchestration when provider provenance, mixed direct providers, exact raw-panel evidence, or same-artifact enforcement is load-bearing.

Official references: [Fusion plugin](https://openrouter.ai/docs/guides/features/plugins/fusion), [provider selection](https://openrouter.ai/docs/guides/routing/provider-selection), [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), and [usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

## Adversarial gates

The profile declares fail-closed plan, pre-execution, post-execution, final, and summarize checkpoints for the active Codex workflow. A full `fuse` call automatically gates its synthesized candidate once per amendment round, and `adversarial_gate` applies the same exact-hash reviewer contract to an explicitly supplied artifact. The active Codex skill invokes those tools at the other named checkpoints and supplies their required local evidence.

Gate rules include:

- two independent valid reviewer passes over the same artifact hash;
- a confirmed mechanical failure blocks regardless of model agreement;
- blind criteria require targeted review before pass;
- malformed verdicts are failures;
- the artifact author is excluded from independent review;
- a failure can change to pass only through a fresh amendment reviewer checking the original blocking issues;
- revision cycles are bounded.

External gate seats have `tool_policy: "none"`. They evaluate supplied diffs, test output, provenance, and other evidence. The active Codex session runs local checks; a remote reviewer cannot truthfully claim it executed a repository command.

## Budgets

Every profile exposes hard limits for:

- model calls;
- aggregate, input, visible output, and reasoning tokens;
- server-tool calls;
- wall time;
- total USD and per-provider USD;
- user approval threshold;
- warning fraction;
- synthesis/gate budget reserve.

The default hard stop is $100 per run and reserves 30% of the call budget for synthesis and gates. The `$25` approval threshold is a host-facing checkpoint: the active Codex skill must ask before projected spend crosses it because a noninteractive MCP server cannot open an approval prompt. These values are intentionally visible and user-configurable. Rescue cannot override a hard stop, and unknown cost is never treated as zero.

OpenRouter native Fusion costs the parallel panel calls plus its comparative/final work in addition to the outer request. Increasing panel size scales cost approximately linearly. Benchmark scaling showed a typical quality knee around four strong samples and a plateau around seven, so the schema caps native Fusion at eight seats.

## Privacy and persistence

External API seats are data egress. The default:

- redacts sensitive values, environment contents, and Git credentials;
- denies common credential and private-key paths;
- requires approval for other sensitive paths;
- fences all artifacts as untrusted data;
- disables provider training where an enforceable control exists;
- asks OpenRouter for zero-data-retention routes;
- keeps xAI/OpenAI provider-side `store` false;
- never persists hidden reasoning;
- does not persist constructed raw prompts.

Raw visible provider responses **are persisted locally** because the synthesizer needs the original evidence, gates bind to the exact artifact, and crash resume must not silently re-run costly calls. They live only under the private plugin run directory: directories are mode `0700`, files are mode `0600`. Ordinary observability logging remains `metadata_only`; the private response artifact is a separate required run-state object. Provider retention is independent and governed by provider policy.

Do not select a metadata-only/non-resumable response mode until the runtime explicitly implements one. Deleting local run state does not delete provider logs.

## Evidence

Evidence policy controls source quality, citation verification, deterministic code checks, command/exit-code recording, test output, input/output hashes, minimum source count, and treatment of unverifiable claims.

Self-reported model confidence is disabled because it is not calibrated across providers. Mechanical evidence and verified sources outrank confidence or vote count. Supported minority findings remain attached to their evidence through judge, synthesis, gate, and execution handoff.

## Rescue

Transport retry and semantic rescue are separate. The runtime enforces bounded transport retries, model fallbacks, semantic response validation, and a per-provider circuit breaker. The host workflow interprets `fallback_seats`, targeted specialist strategy, and human-handoff thresholds:

- retry the same route only for connection, timeout, rate-limit, or server errors;
- use model/provider fallback for empty content, invalid schema, unsupported parameters, explicit policy refusal, context overflow, or provider-tool failure;
- trip a circuit breaker after repeated failures;
- preserve failure provenance;
- stop and report on budget exhaustion;
- hand off after bounded failure cycles.

`allow_degraded_single_provider` and `allow_single_live_seat` are both false in the maximum-intelligence profile. Panel collapse or configured-seat loss fails closed; the runtime does not quietly call a lower-quality single answer “fusion.”

## Codex execution and native agents

The plugin's external seats deliberate. They do not execute.

`native_codex` records host preferences such as executor model, reasoning effort, reviewer models, parallelism, and whether pre/post gates are mandatory. `profiles.<name>.execution` defines the handoff contract: fused-plan requirement, sandbox request, user approvals, unrelated-change preservation, tests, diff review, bounded repair cycles, and completion evidence.

This is `host_handoff`, not an MCP provider. The plugin cannot impersonate or programmatically spawn a native Codex agent. The active Codex task decides whether to delegate, applies its live sandbox and approval policy, and remains responsible for every workspace or external mutation.

External models may never write the workspace. Destructive actions and external writes require the same user authorization they would require without fusion.

## Examples

The JSON files under [`plugins/relentless-inception/examples`](../plugins/relentless-inception/examples) are override fragments intended to be deep-merged with the shipped default:

- `xai-grok45-direct.json` — direct high-effort Grok 4.5/4.3 fusion only;
- `openrouter-native-fusion.json` — optional server-managed OpenRouter Fusion fast path with a client fallback;
- `trusted-router-mixed-panel.json` — direct xAI plus a generic TrustedRouter panel seat.

Example fragments contain only environment-variable names. The TrustedRouter URL and model IDs must match the operator's current API contract; a router is not trustworthy merely because it is named trusted.

Test an example without copying credentials or replacing the default:

```bash
export RELENTLESS_INCEPTION_CONFIG="$PWD/plugins/relentless-inception/examples/xai-grok45-direct.json"
export XAI_API_KEY="...set outside repository..."
```

Then run `config_validate`, `doctor`, `provider_models`, and `provider_test`. Unset `RELENTLESS_INCEPTION_CONFIG` to return to the normal user override path.

## Local validation

From the repository root:

```bash
python3 -m json.tool plugins/relentless-inception/config/default.json >/dev/null
python3 -m json.tool plugins/relentless-inception/schemas/config.schema.json >/dev/null
for file in plugins/relentless-inception/examples/*.json; do python3 -m json.tool "$file" >/dev/null; done
```

Validate the schema and shipped default when `jsonschema` is available:

```bash
PYTHONPATH=plugins/relentless-inception python3 - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator

root = Path("plugins/relentless-inception")
schema = json.loads((root / "schemas/config.schema.json").read_text())
config = json.loads((root / "config/default.json").read_text())
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(config)

from relentless_inception.config import validate_config
errors = validate_config(config)
if errors:
    raise SystemExit("\n".join(errors))
print("configuration valid")
PY
```

Validate each example after deep merge:

```bash
PYTHONPATH=plugins/relentless-inception python3 - <<'PY'
import os
from pathlib import Path
from relentless_inception.config import load_config

for path in sorted(Path("plugins/relentless-inception/examples").glob("*.json")):
    os.environ["RELENTLESS_INCEPTION_CONFIG"] = str(path.resolve())
    load_config(include_user=True, validate=True)
    print("valid", path.name)
PY
```

Finally run the repository test suite. JSON and schema validation prove configuration structure, not provider credentials, current model availability, native Fusion behavior, retention policy, or model quality.

## Benchmark basis

The defaults follow the user's [TrustedRouter Fusion benchmark artifact](https://github.com/ahuserious/trustedrouter-fusion-artifact): the synthesizer is the highest-leverage seat, judge capability is usually a smaller lever once drafts are fixed, raw panels must survive into synthesis, lone-correct minority evidence must not be voted away, and diversity should come from capable models/personas/evidence rather than temperature alone.

Those are empirical defaults, not universal laws. Benchmark provider/model combinations on the user's actual task distribution and retain exact prompts, route provenance, model versions, evaluator configuration, and message order.
