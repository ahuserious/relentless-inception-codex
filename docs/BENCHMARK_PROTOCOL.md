# Benchmark Protocol

Relentless Inception uses physical task harnesses as integration tests, not as marketing scores. A complete claim requires three independent layers to agree:

1. the harness verifier reports the task reward;
2. every required fusion and lifecycle gate has exact receipt-bound evidence;
3. the current validator accepts the pinned contract, source hashes, event order, model roster, and outcome.

A pass at one layer cannot substitute for another.

## Pinned surfaces

The final source pins:

- Codex `0.145.0-alpha.18`, model `openai/gpt-5.6-sol`, effort `xhigh`;
- Harbor `0.20.0` at commit `459ff6ec99417589b7f679d14ddf3b3f0ae4f1dc`;
- Terminal-Bench source commit `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c` and per-task image digests;
- Pier `0.3.0`;
- DeepSWE source commit `6db64a40f3318d8659238ff34a8cc4b491c49205`, task base commits, expected pass-to-pass/fail-to-pass totals, and image digests;
- plugin, harness runtime, runner, and validator SHA-256 values in `bench/pins.json`.

The runner checks image, source, and artifact hashes before a paid attempt. It creates a fresh private evidence directory, mounts the plugin/runtime read-only, injects xAI credentials through an owner-only ephemeral file, runs one attempt with no harness retries, writes an immutable contract/index, and invokes the fail-closed validator.

## No-oracle boundary

External seats receive the task plus bounded mechanical evidence gathered by the active host. They do not receive solution patches, hidden verifier logic, workspace tools, or direct filesystem access. The active Codex host alone may inspect/mutate the task workspace and run commands. Provider-hosted web/code tools are not local workspace tools.

## Lifecycle acceptance

The validator expects one fusion plan and plan, pre-execution, post-execution, final, and summarize lifecycle gates. It binds each expected invocation to exact prompt/schema hashes, reserved attempts, normalized responses, ledger entries, reviewer roster, and event chronology. Missing or drifted paths, secret leakage, duplicate fusion, unsupported model IDs, incomplete receipts, negative verdicts, and mismatched mechanical evidence fail closed.

## Current limited campaign

Only the outcomes listed in [release evidence](RELEASE_EVIDENCE.md) are completed physical observations. The target protocol allows additional tasks and repeat attempts, but unexecuted jigs are not evidence. The public [artifact repository](https://github.com/ahuserious/codex-fusion-artifact/tree/limited-cost-2026-07-20) retains the selected results and opt-in wrappers.

## Reproduction safety

Benchmark runs are billable and resource-intensive. Use a fresh output directory, review every pin and mount, confirm Docker capacity, and set an explicit budget. The public wrappers refuse to start without `--execute`. Never publish the private output tree wholesale; create a reviewed allowlist and retain failures rather than deleting them.
