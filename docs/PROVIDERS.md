# Providers and Models

Relentless Inception has two provider paths with different capabilities:

- **Plugin providers** call external APIs from the bundled MCP server and return text or structured deliberation artifacts. These seats have no Codex workspace tools.
- **Native Codex providers** are optional user-level Codex configuration. When compatible, a model can participate in Codex's tool loop under Codex's sandbox.

Configure the first with the `relentless-inception-config` skill and MCP tools. Configure the second manually and only with explicit consent.

## Plugin provider configuration

The merged plugin configuration comes from:

1. the shipped `plugins/relentless-inception/config/default.json`;
2. an optional user override at `RELENTLESS_INCEPTION_CONFIG`, or otherwise `PLUGIN_DATA/config.json` / `~/.codex/relentless-inception/config.json`.

Overrides are deep-merged and written atomically with mode `0600`. `config_schema` is the authoritative displayable settings catalog; `config_show` always returns a redacted view.

### Supported adapter contracts

| Provider type | Protocol used by the plugin | Typical use |
|---|---|---|
| `xai_responses` | OpenAI-style Responses at the configured xAI endpoint | Direct Grok 4.5 panel, judge, synthesizer, or verifier. |
| `openai_responses` | OpenAI Responses | Direct OpenAI seat. |
| `anthropic_messages` | Anthropic Messages with `x-api-key` authentication | Direct Anthropic panel, judge, or synthesizer. |
| `openai_compatible_chat` | Chat Completions-compatible JSON | Trusted/private router exposing a compatible interface. |
| `openrouter_chat` | OpenRouter Chat Completions plus routing fields | Arbitrary OpenRouter model seats. |
| `openrouter_fusion` | OpenRouter Chat Completions with its Fusion plugin | Optional server-side fusion experiment. |

The current adapter does not implement every vendor's proprietary endpoint. Use the matching transport contract; do not point an OpenAI-compatible type at an unrelated proprietary API and call it supported.

Provider records name an environment variable in `api_key_env`; they never contain the secret value. Seats refer to providers and exact model ids.

### Supplying provider credentials

The plugin resolves the API key named by `api_key_env` from the MCP process environment first. When that environment is difficult to populate safely, it can read one or more explicit credential files from top-level `secret_env_files` or from the path list in `RELENTLESS_INCEPTION_SECRETS_FILE` (separated with the operating system path separator).

Every credential file must be a regular file owned by the current user with mode `0600`. Its format is static `NAME=value`: blank lines and `#` comments are ignored, an optional `export ` prefix and one matching pair of quotes are accepted, and values containing shell expansion (`$`) or command substitution (backticks) are rejected. The parser never invokes a shell. Earlier files win when a name appears more than once; an actual process-environment value still overrides every file value.

For example, a private file may contain `XAI_API_KEY=...`, while the provider configuration contains only `"api_key_env": "XAI_API_KEY"`. The key value is never returned by `config_show`, `config_get`, `doctor`, or `provider_test`. Keep credential files outside repositories and native agent files. The bridge supplies the primary API key only; provider-specific extra headers still follow the installed schema's environment-backed header contract.

Authenticated completion and model-discovery requests do not follow redirects. A redirect fails at the configured origin before the adapter can forward `Authorization` or environment-backed headers to another URL. Configure `base_url` as the final intended HTTPS origin rather than relying on redirection.

## Direct xAI and Grok 4.5

The current xAI values are:

- API base: `https://api.x.ai/v1`
- Responses endpoint: `POST /responses`
- model id: `grok-4.5`
- aliases: `grok-4.5-latest` and `grok-build-latest`
- credential environment variable: `XAI_API_KEY`
- reasoning effort: `low`, `medium`, or `high`; default/highest is `high`

Use the exact API id `grok-4.5` for direct xAI seats. `grok-4.5-latest` is the native Grok Build alias used by the companion plugin's subagents; the Codex MCP runtime deliberately pins the exact API id. Shipped defaults never fall back to an older Grok model.

Grok 4.5 has a 500,000-token context window and accepts text and image input with text output. It supports reasoning, structured outputs, and function calling. Its reasoning cannot be disabled. The plugin's external panel path uses non-streaming HTTP and does not grant Grok local tools, even though the underlying xAI API supports tools and streaming. See xAI's [Grok 4.5 model page](https://docs.x.ai/developers/models/grok-4.5), [reasoning guide](https://docs.x.ai/developers/model-capabilities/text/reasoning), and [function-calling guide](https://docs.x.ai/developers/tools/function-calling).

The old Grok-specific Relentless Inception plugin used `xhigh` in some examples. Do not copy that value here: the current official Grok 4.5 Responses contract accepts only `low`, `medium`, and `high`.

A minimal provider/seat override fragment is:

```json
{
  "providers": {
    "xai_direct": {
      "enabled": true,
      "type": "xai_responses",
      "base_url": "https://api.x.ai/v1",
      "api_key_env": "XAI_API_KEY",
      "store": false,
      "request_timeout_seconds": 900,
      "max_retries": 2
    }
  },
  "seats": {
    "grok45_verifier": {
      "enabled": true,
      "provider": "xai_direct",
      "model": "grok-4.5",
      "reasoning_effort": "high"
    }
  }
}
```

Use `config_set` rather than copying a large fragment over an existing user configuration. Use `provider_models` to query the authenticated `GET /models` result because availability can vary by account or region.

### Storage and cache affinity

xAI Responses can be stored provider-side and xAI documents 30-day response retention for stored responses. Relentless Inception defaults `store` to `false`; changing it should require informed user consent. xAI recommends a stable `prompt_cache_key` for Grok 4.5 cache affinity. Use that option only if it is exposed by the installed configuration schema. Cache affinity can reduce repeated-input cost, but it is not a privacy guarantee and does not replace the provider's retention policy. See xAI's [Responses comparison](https://docs.x.ai/developers/model-capabilities/text/comparison).

## OpenRouter

For plugin seats, use:

- provider type `openrouter_chat`;
- base URL `https://openrouter.ai/api/v1`;
- credential environment variable `OPENROUTER_API_KEY`;
- provider-prefixed model ids, such as `x-ai/grok-4.5`.

OpenRouter's current model catalog confirms the exact Grok id on its [Grok 4.5 page](https://openrouter.ai/x-ai/grok-4.5/api). `provider_models` returns the current routed catalog and supported-parameter metadata.

OpenRouter can route one model id to different upstream providers. For reproducible or privacy-sensitive gates:

- constrain upstreams through configured `provider_preferences`;
- require parameter support;
- disable provider fallback when exact provenance matters;
- request zero-data-retention routing where available;
- record actual route metadata and generation id;
- treat a route change as relevant to independence and reproducibility.

The plugin also supports an optional `openrouter_fusion` transport. It delegates fan-out to OpenRouter and therefore reduces local visibility into per-seat behavior. Keep client-orchestrated fusion as the default when same-artifact enforcement, raw-panel preservation, or per-seat provenance is load-bearing.

### Native Codex through OpenRouter

Codex custom model providers currently support only the Responses wire protocol; the former Chat Completions wire is no longer a fallback. OpenRouter offers an OpenAI-compatible Responses endpoint at `https://openrouter.ai/api/v1/responses`, but OpenRouter documents it as **beta and stateless-only**. Codex 0.145 custom providers also advertise namespace tools, so a native OpenRouter configuration is experimental: the routed endpoint must accept the emitted tool types and pass streaming and multi-turn function-call continuation tests on the installed Codex build. See the [OpenRouter Responses documentation](https://openrouter.ai/docs/api/reference/responses/overview).

The example `native-codex-openrouter-provider.toml.example` is a candidate configuration, not a compatibility guarantee.

## Trusted or private routers

The shipped trusted-router slot uses the generic `openai_compatible_chat` plugin adapter. Replace its base URL, credential env name, model ids, and routing metadata with values from the router operator.

For the plugin path, require:

- HTTPS, as required by the shipped configuration schema;
- bearer authentication from a dedicated environment variable;
- Chat Completions-compatible request and response JSON;
- stable requested-versus-actual model reporting;
- structured-output compatibility if a seat returns a gate schema;
- documented retention, logging, jurisdiction, and fallback behavior.

For the native Codex path, the bar is higher. The router must provide an OpenAI-compatible **Responses** API with SSE streaming, reasoning fields used by the chosen model, function-call outputs, and multi-turn continuation. A Chat-Completions-only router can still be an external panelist but cannot be a native Codex provider today.

Use `native-codex-trusted-router-provider.toml.example` only after the operator confirms that contract.

## Direct OpenAI

Use provider type `openai_responses`, base URL `https://api.openai.com/v1`, and `OPENAI_API_KEY`. Choose a model id returned by live discovery and compatible with the configured reasoning effort and output schema.

The plugin's direct OpenAI seat is independent from Codex's ChatGPT subscription. API billing and retention follow the API account, not the Codex login.

## Direct Anthropic

Use provider type `anthropic_messages`, base URL `https://api.anthropic.com/v1`, and `ANTHROPIC_API_KEY`. The adapter sends `POST /messages`, uses `x-api-key` plus the configured `anthropic-version`, and extracts text content blocks.

The plugin maps any non-`none` seat reasoning setting to Anthropic adaptive thinking. It does not claim that `low`, `high`, or another cross-provider label has identical semantics across vendors. Strict gate JSON is still validated locally after the response; the current Anthropic path does not rely on an OpenAI `response_format` field.

Native Codex custom providers currently require the Responses wire protocol, so the native Anthropic Messages endpoint cannot be used directly as a Codex provider. Use Anthropic as an external plugin seat, or use a router that supplies a sufficiently compatible Responses API and passes the native smoke test.

## Seat fallbacks and provenance

A seat can list fallback models, but fallback occurs only when the seat explicitly enables it. Record:

- requested provider/model;
- actual provider/model;
- route metadata;
- latency and request id;
- input, output, reasoning, and cached tokens when returned;
- provider-reported or configured estimated cost;
- the error that caused fallback.

Never present a fallback response under the original model's name. For high-assurance review, prefer a failed required seat over a silent substitution.

During `fuse` or `adversarial_gate`, an HTTP-success response that fails JSON or semantic validation is still potentially billable. Before considering model fallback, the runtime persists a failure response artifact and records returned usage/cost, or unknown cost when the response cannot be decoded. That accounting can latch a blocking budget stop; when it does, no fallback request is sent.

## Provider preflight

Use this sequence after any provider or seat change:

1. `config_validate`
2. `doctor`
3. `provider_models`
4. `provider_test`
5. one small structured-output fusion or gate in a disposable task

`provider_test` is a tiny, tool-free `PONG` probe for ordinary seats with local seat-level model fallback disabled. It proves a basic completion path only; it does not prove cost reporting, long-context behavior, structured output, every reasoning effort, tool calling, router-level routing behavior, or native Codex compatibility. The tool refuses `openrouter_fusion` seats before dispatch because one Fusion request can invoke multiple inner models and is not a bounded low-cost connectivity probe. Test that path only with an explicitly budgeted disposable fusion run.

## Native Codex provider setup

Codex user-level provider definitions live in `~/.codex/config.toml`. Project `.codex/config.toml` cannot override machine-local provider or authentication fields. Codex's custom-provider `wire_api` currently accepts only `responses`; Chat Completions is not a supported fallback. Refer to the official [configuration reference](https://developers.openai.com/codex/config-reference/) and [configuration schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json).

The examples directory contains:

- `native-codex-agent-limits.toml.example`;
- `native-codex-xai-provider.toml.example`;
- `native-codex-openrouter-provider.toml.example`;
- `native-codex-trusted-router-provider.toml.example`;
- `native-codex-grok-reviewer-agent.toml.example`.

Provider snippets must be manually merged into `~/.codex/config.toml`; do not overwrite the file. The following is a future-compatibility retest shape, not a recommendation to register Grok on the tested build. Codex 0.145 uses this shape for limits and role registration:

```toml
[agents]
max_concurrent_threads_per_session = 4
max_depth = 1
job_max_runtime_seconds = 1800
interrupt_message = true

[agents.grok45_reviewer]
description = "Independent read-only Grok 4.5 adversarial reviewer."
config_file = "/absolute/path/to/.codex/agents/grok45_reviewer.toml"
nickname_candidates = ["Axiom", "Kepler", "Turing"]
```

For a future retest, replace the placeholder with the absolute path to `~/.codex/agents/grok45_reviewer.toml`. The referenced file is a Codex config layer: keep model/provider selection, reasoning effort, the read-only/no-approval posture, tool-feature and inherited-MCP disables, and `developer_instructions` there, while keeping `description`, `config_file`, and `nickname_candidates` in the main role table. The bundled `native-codex-agent-limits.toml.example` and `native-codex-grok-reviewer-agent.toml.example` show the two halves. Do not leave the role registered after a failed smoke test.

### Verified Codex 0.145 xAI boundary

Codex 0.145 custom Responses providers advertise/send tools with `type: "namespace"` by default. A local xAI probe rejected that tool type with HTTP 422; xAI's documented built-in tool types include `web_search` and `x_search`, while custom tools use `function`. Changing `wire_api` to Chat Completions is not a workaround because current Codex removed that wire option. The default namespace capability is visible in Codex's current [provider source](https://github.com/openai/codex/blob/main/codex-rs/model-provider/src/provider.rs), and xAI documents its accepted shapes under [tools](https://docs.x.ai/developers/tools/overview) and [function calling](https://docs.x.ai/developers/tools/function-calling).

A direct foreground probe found one narrow partial success. With web search disabled, skill instructions omitted, all known tool-producing features disabled, and every inherited MCP server disabled, Grok 4.5 returned a correct streamed text response. That did **not** establish custom-agent compatibility. An end-to-end Codex parent→`grok45_reviewer` spawn subsequently failed with HTTP 422 even in an isolated, tool-minimal configuration; xAI reported that the request body did not match any accepted `ModelInput` variant.

The spawned-text failure is already blocking. In a separate probe Grok selected a shell function and Codex executed it, but xAI also rejected the continuation with `Could not decode the compaction blob`. Disabling remote compaction and raising the automatic-compaction threshold did not repair the continuation. Do not register or select this native role on the tested Codex 0.145 build. Direct xAI seats inside the Relentless Inception MCP runtime remain the operational Grok deliberation, fusion, and provider-tool path because they use the plugin's own Responses adapter.

The custom-agent config layer merges with the parent, but Codex first deserializes the role file on its own. Disabling plugins/apps is not enough when the user's main config defines MCP servers. For every inherited server, repeat the matching transport discriminator (`command` for stdio or `url` for HTTP) alongside `enabled = false`; an enabled-only partial table fails with `invalid transport`. Do not copy literal credentials. Revisit the list whenever the main configuration changes, run `codex --strict-config doctor --json`, and refuse to register the role if the standalone layer is malformed or any inherited tool remains exposed. The bundled role example shows both transport shapes.

After upgrading Codex or xAI, verify before registering the native role:

1. a parent-spawned custom-agent streamed response with no tools;
2. a function call issued by the model;
3. Codex executing the function under the expected sandbox;
4. a second response after `function_call_output` continuation;
5. timeout behavior on a high-reasoning request;
6. requested and actual model identity.

The first text step means a real parent-spawned custom-agent turn, not a foreground session with the same provider. If it fails, remove or disable the native role registration and use Grok 4.5 through the external MCP panel. If spawned text passes but any tool step fails, retain reasoning-only mode and keep every tool surface disabled.
