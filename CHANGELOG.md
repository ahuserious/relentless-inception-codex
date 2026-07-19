# Changelog

## 0.1.1 - 2026-07-19

- Hardened provider usage and cost accounting, budget-ledger restoration, concurrent snapshot persistence, and post-response gate stop checks so integrity failures remain fail-closed across resume.
- Added complete short- and long-context fallback pricing for every direct Grok 4.3 and Grok 4.5 seat, with the higher rate tier applied above 200,000 input tokens.
- Added explicit synthesis `mode` and `author_seat` provenance so cached client-orchestrated and native OpenRouter artifacts cannot be confused during author-separation checks.
- Bound every cached panel, judge, synthesis, amendment, and gate result to an exact prompt invocation, reserved provider attempt, full response hash, private raw-response artifact, and ledger entry; native fallback markers now require matching attempt evidence.
- Refused redispatch when an exact raw response survived a crash before ledger commit, rejected panel caps that could omit required seats, and expanded deterministic gate parsing for standard pytest and make failure output.
- Introduced internal budget-ledger snapshot schema v3. Pre-0.1.1 run directories remain preserved but are intentionally not resumable; restart with a new run ID because legacy ledgers and synthesis artifacts do not contain enough trustworthy information for a safe migration.

## 0.1.0 - 2026-07-19

- Initial Codex plugin marketplace package and bundled stdio MCP server.
- Direct xAI/OpenAI Responses, Anthropic Messages, OpenRouter chat/native Fusion, and generic OpenAI-compatible adapters.
- Independent panel, anonymous structured judge, minority-preserving synthesis, exact-hash adversarial gates, and amendment loop.
- Enforced concurrency, call/token/tool/cost/time budgets, retries, quality floor, degradation policy, kill switch, atomic evidence, and hash-checked resume.
- Displayable JSON Schema, validated private user overrides, safe environment/0600 secret indirection, provider diagnostics, and native Codex setup templates.
- Provider probes disable seat tools, tool policies, and local seat-level model fallback; OpenRouter Fusion probes are refused because one request can fan out to multiple inner models.
- Authenticated completion/model-discovery redirects are refused, and orchestrated HTTP-success semantic failures are persisted and accounted before fallback so budget latches remain authoritative.
- Each run ID has one cross-process active-owner lease; provider concurrency is not globally coordinated across distinct runs or processes.
- Profile and runtime validation reject duplicate panel, optional-panel, and reviewer entries plus required/optional panel overlap; every completed negative gate verdict overrides numeric quorum.
- Native Grok custom-agent templates are retained only as future-compatibility examples; tested Codex 0.145 defaults use Codex-native OpenAI executors/reviewers and external xAI Grok fusion seats.
- External Grok API/MCP participants are consistently described as seats rather than native Codex subagents, and Grok 4.5 cached-input fallback pricing is corrected to $0.50 per million tokens.
- Network-free unit suite and CI workflow.
