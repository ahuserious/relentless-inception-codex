# Release Evidence

This page defines exactly what was observed for version 0.1.4 and what remains unverified. The public evidence package is immutable at [`ahuserious/codex-fusion-artifact@limited-cost-2026-07-20`](https://github.com/ahuserious/codex-fusion-artifact/tree/limited-cost-2026-07-20).

## Claim boundary

The campaign was deliberately limited by API cost. It is an engineering acceptance sample, not a model leaderboard or a statistically powered comparison. No repeated seeds, matched solo baseline, confidence interval, blinded grader, or significance test was run.

## Environment

| Surface | Observed value |
|---|---|
| Plugin version | `0.1.4` |
| Tested source commit | `eaba350bc49cecd5e4ef56e76b0a3f5c188be326` |
| Codex CLI | `0.145.0-alpha.18` |
| Active host model | `openai/gpt-5.6-sol`, effort `xhigh` |
| Source plugin tree SHA-256 | `1f9722b7659edd643fa62a1e36ae6ad6e008a0e46535668eab2af2665818ccaa` |
| Installed cache tree SHA-256 | `1f9722b7659edd643fa62a1e36ae6ad6e008a0e46535668eab2af2665818ccaa` |
| Offline suite | 215 tests passed; 1 Pier-dependent test skipped under system Python |

The matching source/cache tree hashes cover relative paths, executable modes, and per-file hashes while excluding bytecode and `.DS_Store`.

## Live provider matrix

| Provider surface | Offline/mock coverage | Live campaign | Release claim |
|---|---:|---:|---|
| Direct xAI Responses | Yes | Yes | Exact Grok 4.5 orchestration completed |
| Direct OpenAI Responses | Yes | No | Implemented, not live-accepted here |
| Direct Anthropic Messages | Yes | No | Implemented, not live-accepted here |
| OpenRouter chat | Yes | No funded credential | No live claim |
| OpenRouter native Fusion | Yes | No funded credential | No live claim; cheap probe intentionally refuses fan-out |
| Trusted/private OpenAI-compatible router | Yes | No | Contract/mock coverage only |
| Native custom Grok role inside Codex | Compatibility tests only | Failed on Codex 0.145/xAI input boundary | Not shipped as a default native role |

## Current direct-xAI fusion

Run `codex-0144-frontier-smoke-001` completed from `2026-07-20T05:26:55Z` to `05:32:39Z`.

| Property | Retained value |
|---|---:|
| Calls | 10 |
| Requested/actual model | exact `grok-4.5` for all calls |
| Panel/judge/synthesizer | 3 / 1 / 1 |
| Initial gate | 1 `PASS`, 1 `NEEDS_WORK`; rejected |
| Amendment | 1 synthesizer call |
| Second gate | 2/2 `PASS` |
| Input/output/reasoning tokens | 54,021 / 27,039 / 9,297 |
| Total/cached tokens | 81,060 / 1,280 |
| Known cost | $0.268100 |
| Unknown-cost calls | 0 |

This proves the enforced live path completed with exact model provenance and a real amendment cycle. Because every external seat was Grok 4.5, it proves role-diverse multi-agent deliberation, not a benefit from cross-model external diversity.

## Task-harness outcomes

| Host/harness/task | Task result | RI evidence result | Current-release binding |
|---|---|---|---|
| Codex / Terminal-Bench / `fix-git` (`codex-final-003`) | Reward 1.0 | Six lifecycle run directories; 17 historical direct-xAI calls; visible 2/2 gates | Historical only: mixed Grok 4.5/4.3 profile and drifted contract; current validator rejects it |
| Codex / DeepSWE/Pier / `anko-default-function-arguments` (`codex-final-006`) | Reward 0; 119/119 pass-to-pass, 0/2 fail-to-pass | Zero RI calls | Negative trace: oversized preflight stopped before fusion; post-fix physical rerun not performed |

The historical Terminal-Bench external-seat ledger reports $0.32157745; its Codex host reports $1.410944. The DeepSWE host reports $0.380207. Those selected costs are not a full development-spend accounting.

## Why the failures stay public

The Terminal-Bench reward cannot override a stale evidence contract. The DeepSWE partial score cannot be rounded into task success, and a post-failure source fix cannot be called a physical pass without rerunning the paid harness. Publishing both traces makes those boundaries auditable.

## Public/private split

The artifact publishes exact safe standalone response receipts, selected manifests/ledgers, harness verifier outputs, derived summaries, checksums, and jigs. It withholds auth state, credential values, raw sessions, local config, full trajectories, job logs, ephemeral secret paths, and files containing absolute local paths.
