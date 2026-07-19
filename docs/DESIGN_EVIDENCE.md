# Design evidence and decisions

## Fusion topology

The canonical client pipeline is independent reports → mechanical checks → anonymous comparative diagnosis → fresh synthesis → independent exact-hash verification. It intentionally does not use majority voting or open-ended debate.

The TrustedRouter artifact showed that synthesis capability can move the result by roughly eighteen points with an otherwise fixed panel, while a controlled 23-task role-isolation experiment found about `+8.02` to `+9.24` fusion gain with a Sonnet synthesizer and about `+2.22` to `+4.36` with a Haiku synthesizer. This motivates spending capability on the synthesizer, using a cheaper independent judge by default, and leaving every role configurable.

Other adopted findings:

- preserve supported lone-minority findings because weak synthesis can erase the only correct answer;
- prefer three to five high-quality independent reports; scaling tends to flatten around seven;
- distinct roles and context bundles diversify errors more reliably than temperature alone;
- keep research/tool use separate from a no-tools synthesis turn;
- validate HTTP-successful empty, malformed, or schema-invalid responses locally;
- use a quality floor and explicit degradation record instead of counting every successful HTTP call as a live expert;
- use client orchestration for mixed direct providers because OpenRouter native Fusion does not expose per-inner-seat routing.

OpenRouter native Fusion remains an optional fast path. Its panel and judge can use OpenRouter-hosted web tools, but the client pipeline is the source of truth for cross-provider routing, artifacts, budgets, and execution gates.

## Workflow ideas incorporated

From batch-create-eval:

- decompose into independently verifiable units;
- keep build and evaluation evidence explicit;
- use a clean feature branch and avoid silent main-branch work;
- run deterministic checks before claiming completion.

From Gigaprompt:

- state the objective, constraints, evidence bar, failure conditions, and final verification contract;
- retain a stable requirements trace rather than relying on conversational memory;
- make handoffs self-contained and bind release claims to evidence.

From Exaflop:

- use genuinely different analytical personas;
- converge on one dominant synthesis while preserving materially different alternatives and minority evidence;
- bound the loop and make stopping conditions explicit.

From the Codex adversarial-review contract:

- review immutable artifact hashes;
- separate author and reviewer roles;
- require fresh independent passes and mechanical evidence;
- never reinterpret FAIL as PASS without an amended artifact and a new review.

## Security boundary

External API panelists are not native Codex subagents. They receive only the explicit task/context supplied to the fusion tool and optional provider-hosted tools. The active Codex host owns workspace inspection, writes, commands, and approvals. This boundary is structural, not prompt-only.

## Known boundaries

- A plugin cannot currently install or select an arbitrary native Codex provider per spawned agent through its manifest. Personal agent/provider TOMLs are therefore opt-in templates and must be smoke-tested on the installed Codex version.
- Cost enforcement is exact when a router reports cost and estimated from configured token pricing otherwise. Provider pricing can change; catalog refresh and ledger reconciliation remain necessary.
- Raw model outputs are persisted locally for evidence and resume. Constructed prompts and hidden reasoning are not persisted by this runtime.
- The runtime supports provider-hosted search/code tools, not arbitrary local tool proxying to external seats.
