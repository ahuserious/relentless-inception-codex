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

## Direct xAI and Grok 4.5

The current xAI values are:

- API base: `https://api.x.ai/v1`
- Responses endpoint: `POST /responses`
- model id: `grok-4.5`
- aliases: `grok-4.5-latest` and `grok-build-latest`
- credential environment variable: `XAI_API_KEY`
- reasoning effort: `low`, `medium`, or `high`; default/highest is `high`

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

Codex custom model providers currently support only the Responses wire protocol. OpenRouter offers an OpenAI-compatible Responses endpoint at `https://openrouter.ai/api/v1/responses`, but OpenRouter documents it as **beta and stateless-only**. A native Codex configuration is therefore experimental: it must pass streaming and multi-turn function-call continuation tests on the installed Codex build. See the [OpenRouter Responses documentation](https://openrouter.ai/docs/api/reference/responses/overview).

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

## Provider preflight

Use this sequence after any provider or seat change:

1. `config_validate`
2. `doctor`
3. `provider_models`
4. `provider_test`
5. one small structured-output fusion or gate in a disposable task

`provider_test` proves a basic completion path only. It does not prove cost reporting, long-context behavior, structured output, every reasoning effort, tool calling, or native Codex compatibility.

## Native Codex provider setup

Codex user-level provider definitions live in `~/.codex/config.toml`. Project `.codex/config.toml` cannot override machine-local provider or authentication fields. Codex's custom-provider `wire_api` currently accepts only `responses`. Refer to the official [configuration reference](https://developers.openai.com/codex/config-reference/).

The examples directory contains:

- `native-codex-xai-provider.toml.example`;
- `native-codex-openrouter-provider.toml.example`;
- `native-codex-trusted-router-provider.toml.example`;
- `native-codex-grok-reviewer-agent.toml.example`.

Provider snippets must be manually merged into `~/.codex/config.toml`; do not overwrite the file. The agent example belongs at `~/.codex/agents/grok45_reviewer.toml`. Keep the reviewer read-only and keep `model_reasoning_effort = "high"` for Grok 4.5.

After restarting Codex, verify:

1. a plain streamed response;
2. a function call issued by the model;
3. Codex executing the function under the expected sandbox;
4. a second response after `function_call_output` continuation;
5. timeout behavior on a high-reasoning request;
6. requested and actual model identity.

If any step fails, remove or disable the native agent and use Grok 4.5 through the external MCP panel instead.
