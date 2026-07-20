---
name: relentless-inception
description: Run bounded multi-model deliberation in Codex, preserve minority findings, turn the fused result into an explicit execution handoff, and have the active Codex session implement and verify it. Use for "relentless inception", "maximum intelligence", "fuse several frontier models", difficult multi-step builds, or decisions where one model is not enough. Not for routine edits or cheap factual questions.
---

# Relentless Inception

Use this skill to improve a difficult Codex task with independent external model seats, structured comparison, a strong generative synthesis, and evidence gates. It is intentionally expensive when configured for frontier models.

This is a Codex orchestration contract, not an autonomous daemon. The MCP server runs provider calls and records deliberation artifacts. The active Codex session remains responsible for reading the repository, requesting approvals, editing files, running commands, and reporting results.

The shipped `maximum_intelligence` defaults admit no weaker automatic model substitution: the Codex host and native host handoff use `gpt-5.6-sol`, while every direct xAI fusion and gate seat uses exact `grok-4.5` at high effort. Other models and routers participate only after an explicit validated configuration change.

## Non-negotiable boundary

Keep these two mechanisms distinct:

- **External API panelists** are calls made by the Relentless Inception MCP server to xAI, OpenAI, OpenRouter, or another compatible router. They receive only the task and artifact material sent to the MCP tool. They do not become Codex subagents and do not receive Codex filesystem, shell, browser, connector, or approval capabilities.
- **Native Codex subagents** are sessions spawned by Codex. They inherit Codex tooling and the parent turn's live permission policy. Optional provider and agent TOML examples are manual, consent-based setup; installing this plugin does not register or rewrite native agents.

Never describe an external panelist as having inspected, tested, or changed the workspace unless the active Codex session supplied the corresponding evidence.

## When to use

Use this workflow when at least one is true:

- The task has several plausible architectures with materially different failure modes.
- The change is high-impact, difficult to reverse, or has resisted prior attempts.
- Cross-provider diversity is useful for finding correlated blind spots.
- The user explicitly requests multi-model fusion, Grok 4.5 participation, a consortium, or maximum available intelligence.
- "Done" requires implementation plus mechanical evidence, not a polished answer alone.

Prefer ordinary Codex execution when the task is a small edit, a simple explanation, or cheaper than the deliberation needed to discuss it.

## Workflow

### 1. Establish the contract

Before calling a paid provider:

1. Restate the goal in testable language.
2. List acceptance criteria and the evidence needed for each.
3. Identify the repository or artifact in scope and name anything explicitly out of scope.
4. State whether the request authorizes implementation or only analysis/review.
5. Confirm the selected profile's provider mix, hard budgets, and data-egress implications when they are not already clear from the user's request.
6. Apply the selected profile's `privacy`, `evidence`, `execution`, and `native_codex` policies before egress or delegation: enforce path allow/deny rules and approval checkpoints, collect the required evidence, and treat native model preferences as host instructions rather than MCP authority.

Do not hide a material assumption inside a panel prompt. Ask the user when different interpretations would produce different work.

### 2. Preflight the configured panel

Use the MCP tools in this order:

1. `config_show` to inspect the redacted active configuration.
2. `doctor` to check local configuration, paths, Python/runtime information, and credentials by presence without making a network request.
3. `provider_test` for any ordinary load-bearing seat that has not been tested in the current environment. It intentionally refuses OpenRouter Fusion because one request can fan out to multiple inner models; verify that path only with an explicitly budgeted disposable fusion run.

The live MCP tool schemas are authoritative for arguments. Do not invent arguments from this skill file. If a provider or required seat is unavailable, surface the exact missing capability. Do not silently substitute a weaker model, lower effort, smaller panel, or different provider unless the selected profile explicitly permits that degradation.

### 3. Build one deliberation packet

Give every independent seat the same core problem statement, acceptance criteria, constraints, and artifact snapshot. Add disjoint role instructions only to create useful perspectives, for example:

- first-principles planner;
- implementation and failure-path engineer;
- security and prompt-injection critic;
- mechanical evidence auditor;
- minority-finding advocate.

Separate facts from hypotheses. Include exact file paths, hashes, command output, or citations when a claim depends on them. Do not send secrets, unredacted credentials, unrelated user data, or an entire repository when a narrow evidence bundle is enough.

### 4. Fuse before executing

Call `fuse` with the task, the selected profile, and the deliberation packet accepted by its tool schema.

Treat the result as a structured proposal:

- independent panel responses remain individually attributable;
- the judge organizes consensus, contradictions, blind spots, and unique findings;
- the synthesizer produces a new answer rather than voting or averaging;
- lone-correct minority findings remain visible until disproved;
- the run ledger records requested and actual models, route metadata, token usage, estimated cost when available, and failures.

A confident synthesis is not proof. Inspect its gate result, dissent, assumptions, and `execution_handoff` before changing anything. The MCP runtime gate in `fuse` applies only to the synthesis candidate; it does not silently satisfy the named plan or pre-execution lifecycle gates.

### 5. Complete the host-owned pre-mutation gates

Treat `execution_handoff` as a persisted workflow packet. Its readiness fields have exact meanings:

- `ready_for_host_workflow: true` means the synthesis gate passed and the packet contains the required plan; Codex may start host-side review.
- `ready: false` or `mutation_authorized: false` means an enabled `plan` or `pre_execution` gate is still pending. Do not edit files or mutate external state.
- `lifecycle.pending_gates` names the stages that must pass first. `artifacts.required_checks.lifecycle_gates` supplies each stage's configured `required_evidence`.

For each enabled pending stage, in order:

1. Assemble an immutable stage manifest containing the exact fused plan, the original acceptance criteria, and every named evidence item. For `plan`, this normally includes the requirements trace and risk analysis. For `pre_execution`, it normally includes the approved fused plan and explicit workspace/scope boundaries.
2. Refuse to treat the stage as runnable if any configured `required_evidence` item is absent. Confirm that each reviewer seat's effective provider tools are no broader than the stage's host-owned `tool_policy`; the stage setting does not dynamically rewrite seat configuration. Do not let a reviewer vote compensate for missing evidence.
3. Call `adversarial_gate` over that exact manifest and its mechanical evidence. Enforce the stage's host `timeout_seconds`; if it expires, abort/stop the run and record a failed receipt rather than treating the timeout as a pass.
4. Retain a host-side receipt containing the stage name, manifest SHA-256, gate run id, pass/fail, reviewer quorum, evidence names, effective tool policy, and elapsed time.

Only after every pending plan/pre-execution receipt passes may the active Codex task authorize mutation in its own visible task state. The persisted handoff remains the historical pre-authorization packet; do not rewrite it to pretend it was originally ready.

### 6. Active-Codex execution

The active Codex session executes the returned handoff itself:

1. Reconcile the handoff with the actual repository and current user instructions.
2. Read the relevant implementation, callers, tests, and local instructions before editing.
3. Refuse or narrow any handoff step outside the user's authorized scope.
4. Make the smallest coherent implementation using Codex's normal tools and permission model.
5. Run proportionate tests, static checks, and a realistic usage path.
6. Save or report exact evidence, including failures and skipped checks.

If the original `fuse` response is no longer in context, use `execution_handoff` for the run id rather than reconstructing it from memory. Its `selected_profile`, hashed `execution_contract`, included artifacts, and budget remainder are frozen with the run; never substitute settings from whichever profile happens to be active later. Use `run_status` to inspect an active run and `run_abort` when the user requests a stop or a hard safety/budget condition fires.

External panelists never execute this step. A native Codex worker may assist only through normal Codex delegation and remains subject to the parent sandbox and approvals.

When `native_codex.enabled` is true, resolve configured `reviewer_roles` as named Codex agents before treating them as available. Their separate agent TOML is authoritative for provider/model selection. Never use a role listed in `reasoning_only_roles` to inspect the workspace, retrieve evidence, call tools, execute, or mutate; give it one complete immutable evidence packet for single-turn review. If a named role is absent or its current compatibility probe has not passed, report that fact and use only an available configured `reviewer_models` fallback.

### 7. Complete the post-mutation lifecycle gates

After execution, invoke the `relentless-inception-review` skill and the enabled later gates in `lifecycle.later_gates`. Each stage is a separate `adversarial_gate` call over an immutable manifest with all of that stage's configured evidence:

- `post_execution`: the exact diff or output paths, hashes, test commands/results, and requirement coverage;
- `final`: the current gate verdicts, cost ledger, provider/model provenance, known limitations, and unresolved dissent;
- `summarize`: the decisions, open risks, and verification state that must survive the final handoff or any context compaction.

Include the acceptance criteria and a hash for every reviewed artifact at every stage. Refuse a pass when a named evidence item is missing.

All reviewers in a gate must review the same snapshot. Reviewer seat names must be unique; for a `fuse`-produced artifact, configured author separation applies to the actual synthesis author. `required_passes` is the number of independent reviewer seats that must each return `PASS` for that one SHA-256. A changed artifact invalidates the quorum. Any completed `NEEDS_WORK` or `FAIL`, a reproducible mechanical failure, or a missing criterion blocks regardless of the numeric pass count.

When the gate fails and the user authorized implementation, repair only the blocking issue, rerun relevant verification, produce a new artifact hash, and gate again. When the user requested review only, report findings without modifying files.

### 8. Finish with evidence

Do not claim completion merely because the synthesis sounded strong. Finish only when:

- every acceptance criterion has evidence;
- the configured independent-review quorum holds on the current artifact hash;
- every enabled plan, pre-execution, post-execution, final, and summarize stage has a retained host receipt with its required evidence;
- no blocking minority finding remains unresolved;
- actual provider/model provenance and known cost uncertainty are reported;
- the active Codex session has re-read the original request and checked for scope drift.

## Failure and budget rules

- Provider authentication, timeout, schema, and quota failures remain visible in the run.
- A collapsed panel below `min_live_seats` fails closed.
- Unknown cost is not zero cost. Treat it conservatively and say when the provider omitted price data.
- Stop at configured call, token, wall-time, or dollar caps. Ask before raising a cap.
- No unbounded retries. Repeated identical failures require diagnosis or user direction.
- Do not let "maximum intelligence" override repository safety, external side-effect approvals, privacy, or the user's stated scope.

## What this skill inherits from its predecessors

- Relentless Inception: plan, phase, and handoff discipline with recoverable run state.
- Batch Create Eval: explicit acceptance criteria, independent work units, per-unit verification, and realistic shakedowns.
- Gigaprompt: evidence-first completion, context checkpoints, and stable-artifact review. A higher-level release workflow may additionally run the entire gate twice on one unchanged commit hash.
- Exaflop: genuinely different expert perspectives, cross-critique, and hard cost/time guards.
- TrustedRouter fusion work: spend strength on synthesis, keep the judge economical, and preserve minority findings instead of averaging them away.

The Codex adaptation deliberately omits unverified host hooks, automatic native-agent installation, and claims that an external API model can use local tools.

## Related surfaces

- Use `relentless-inception-config` to inspect or change providers, seats, profiles, gates, and budgets.
- Use `relentless-inception-review` for a hostile same-artifact gate without running a full new planning fusion.
