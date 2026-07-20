Treat the benchmark instruction as the exact task scope. Before any mutation,
perform a read-only preflight of the task-visible workspace. Capture the exact,
redacted command transcript needed to establish repository state, relevant
files, constraints, and available tests. Do not inspect evaluator, oracle,
solution, task-cache, or held-out material. Discover test commands but do not run
the suite during this preflight; required test execution belongs after the fix.
Keep the preflight bounded and make expected discovery absences successful shell
outcomes. Use `set -o pipefail` for pipelines. For a discovery tool whose
documented no-match status is 1, capture its actual `$?`, convert exactly 1 into
a status-zero observation such as `no matches (expected)`, and preserve status 2
or greater as a real failure. Never hard-code an exit-status marker. Redact only
secret values, never statuses or failure text. Do not
invoke a tool after discovering it is unavailable, and do not run
failure-oriented checks against an unmodified recovery candidate merely to
characterize it. Never suppress, normalize, or relabel a genuine repository,
mutation, command, or test failure. Finish this preflight quickly: combine
related read-only checks into a few guarded batches. The entire performed
preflight, including captured output, must satisfy both limits: at most 12
task-relevant command records and at most 12,000 characters. Each command or
pipeline inside a batch counts as one record, and batching does not reset the
count. Target at most 8,000 characters so serialization overhead cannot cross
the 12,000-character hard limit. Keep every command record below 2,500
characters and check the accumulated character count after the first wrapper
before issuing a second. Never dump a whole README, generated source file,
repository file inventory, or repo-wide search result. Read only task-relevant
spans, and cap discovery output at its source (normally no more than 40 lines
per record). Use at most two host-shell tool calls. In each
wrapper, store the returned `r.output` unchanged and emit it directly with
`text(r.output)`. Construct fusion evidence only by concatenating those stored
outputs in original order with no inserted separator. Never manually retype,
trim, normalize, summarize, or interpolate a transcript; perform any necessary
redaction inside the original shell command before capture. Before dispatch,
route every task-relevant command through a `run_record` helper that prints its
exact `$ <command>` before execution and its actual `[exit <status>]` afterward.
Always place the exit marker on a fresh line even when command output has no
trailing newline.
Each retained marker must correspond one-to-one and in order with the command
actually executed; never print a fabricated marker or run an unrecorded
task-relevant command. Before dispatch,
assert that this exact concatenation is at most 12,000 characters and contains
at most 12 command/status records; if not, stop without calling `fuse`. Pass that
same exact string to `fuse`. Use this helper verbatim in every evidence-producing
shell wrapper, followed only by ordered `run_record '<command>' || exit $?`
invocations:
```bash
set -o pipefail
run_record() {
  command_text=$1
  printf '$ %s\n' "$command_text"
  bash -o pipefail -c "$command_text"
  command_status=$?
  printf '\n[exit %d]\n' "$command_status"
  return "$command_status"
}
```
Do not enumerate an entire repository, dangling
objects, or whole history, and do not capture an unbounded diff. Prefer targeted
status, recent history, name/status summaries, and relevant file slices. Call
`fuse` as soon as those facts are sufficient for a safe plan.

Then call the `relentless-inception` MCP server's `fuse` tool. Its `task` must use
this exact two-line template; replace only the placeholder with the byte-identical
benchmark instruction and do not preserve Markdown wrapping as extra newlines:
```text
Produce a pre-execution plan for the active Codex host to fulfill the exact benchmark instruction below. The current artifact is reviewed as a plan; execution has not begun and execution evidence is not yet expected. Do not claim workspace inspection, commands, tests, or mutation beyond supplied mechanical evidence.
<EXACT BENCHMARK INSTRUCTION>
```
Pass the
read-only transcript in `mechanical_evidence`. Every literal nonzero exit in
that evidence is a real blocking failure; expected probe absences must instead
appear as guarded, status-zero transcript observations. Explain in `context`
that the active Codex host, not an external seat, will execute the approved plan.
Provider-hosted tools and code interpreters are isolated from this workspace.
Set `resume_run_id` to `benchmark-fuse` on the first call and every resume. Treat
it as successful only when the result reports `status: completed`, literal
`gate.passed: true`, and `execution_handoff.ready_for_host_workflow: true`. Under
this profile the immutable handoff must remain `status: awaiting_host_gates`,
`ready: false`, and `mutation_authorized: false`, with pending gates `plan` and
`pre_execution`; that is the expected successful state, not a blocker. Use the
fused artifact and handoff returned by that call directly. Do not call
`execution_handoff`, wait for its fields to change, or rewrite it. Do not call
`run_status` after a successful MCP result.

Do not begin execution until the fused artifact is complete and separate
`adversarial_gate` calls labeled `plan` and `pre_execution` have passed on that
exact artifact and the same evidence. Frame those two tasks explicitly as plan
coverage and execution-readiness reviews whose host execution is still pending.
Use these exact task templates, replacing the placeholder with the byte-identical
benchmark instruction and adding no other text:
```text
Lifecycle stage: plan
Review plan coverage for the exact original action request below. Host execution is still pending.
<EXACT BENCHMARK INSTRUCTION>
```
```text
Lifecycle stage: pre_execution
Review execution-readiness for the exact original action request below. Host execution is still pending.
<EXACT BENCHMARK INSTRUCTION>
```
Use `benchmark-plan` and `benchmark-pre-execution` as their respective
`resume_run_id` values. Make those calls immediately after fusion without any
intervening shell, file, or external-state operation. Once both return literal
`gate.passed: true`, retain those exact results as the active host's authorization
receipts; only then may host execution begin. Every `adversarial_gate` call must
contain exactly these four argument keys: `task`, `artifact`,
`mechanical_evidence`, and `resume_run_id`. Do not pass `context`, `profile`, or
any other key; `adversarial_gate` does not expose a context argument.

Then execute the approved plan with Codex. Preserve unrelated files and use only
task-visible project tests. Capture the exact diff, commands, exit codes, and
test output in the immutable Codex trajectory. After all recovery is complete,
run one bounded final-acceptance phase for repository state, intended diff or
postconditions, and required tests. After the final mutation, run every required
test or state check once; do not repeat a successful check or recapture an
identical transcript. If a check fails, preserve it, repair the issue, and only
then create one fresh replacement final-acceptance transcript. Pass only that exact final-acceptance
transcript in completed-work `mechanical_evidence`. Disclose every resolved
intermediate failure and its status-zero resolution evidence in the gate task or
context, while keeping its original nonzero transcript in the trajectory. Never
omit or reclassify an unresolved failure; if any final-acceptance check is
nonzero, do not ask a gate to pass.

The final-acceptance wrapper must store the untouched command output and emit
exactly `JSON.stringify({exit_code:r.exit_code, output:r.output})`.
Route every final-acceptance command through the same one-to-one `run_record`
instrumentation; the executed command, `$` marker, output, and actual exit status
must remain ordered and auditable, with every exit marker on a fresh line. If the shell
call yields a session, keep polling that same session inside the same wrapper,
concatenate each returned `r.output` in order into a variable named `transcript`,
and emit exactly `JSON.stringify({exit_code:r.exit_code, output:transcript})`.
Use the canonical loop shape `let transcript=r.output;
while(r.session_id){r=await tools.write_stdin({session_id:r.session_id,chars:"",
yield_time_ms:60000});transcript+=r.output;}` so the retained trajectory can prove
that no output chunk was omitted.
Store that unchanged output string for all completed-work gates. Do not retype,
trim, normalize, or reconstruct it.

Put exactly one concise `Resolved-failure ledger` in the completed-work artifact.
If no execution command failed, its entire body must be exactly `No resolved
failures.` Otherwise its entire body must be one fenced `json` array. Preserve
failure order and use exactly these keys for every entry: `command` (the exact
marker text without `$ `), `exit_code` (the actual nonzero integer), `cause`,
`corrective_action`, `resolution_command` (the exact later marker text without
`$ `), and `resolution_exit_code` (literal integer `0`). The resolution command
must appear later in the retained trajectory with that actual zero status. All
three completed-work gates must receive that byte-identical ledger. Keep original
raw nonzero transcripts in the immutable trajectory and outside final-acceptance
`mechanical_evidence`.

Structure the completed-work artifact with explicit sections named
`Evidence-backed final state`, `Trajectory context`, `Remaining risks`, and
`Resolved-failure ledger`. Every claim in the evidence-backed section must be
directly supported by the exact final-acceptance transcript. Facts supported
only by preflight or intermediate execution must be labeled as trajectory
context, never presented as final mechanical evidence. Enumerate material
remaining boundaries and risks, including local-only or unpropagated changes,
unrun deploy/build/render checks, retained recovery artifacts, and reliance on
earlier raw failure transcripts when applicable. If a category truly does not
apply, say so concisely instead of omitting the section.

After execution, run separate `post_execution`, `final`, and
`summarize` lifecycle gates through `adversarial_gate`, using the original action
request and actual execution evidence. Do not reuse the plan-review objective
for those completed-work gates. Begin each gate task with its exact label:
`Lifecycle stage: post_execution`, `Lifecycle stage: final`, or
`Lifecycle stage: summarize`. Use these exact task templates, replacing the
placeholder with the byte-identical benchmark instruction and adding no other text:
```text
Lifecycle stage: post_execution
Review actual execution for the exact original action request below. Execution is complete.
<EXACT BENCHMARK INSTRUCTION>
```
```text
Lifecycle stage: final
Review final acceptance for the exact original action request below. Execution is complete.
<EXACT BENCHMARK INSTRUCTION>
```
```text
Lifecycle stage: summarize
Review remaining risks for the exact original action request below. Execution is complete.
<EXACT BENCHMARK INSTRUCTION>
```
Use `benchmark-post-execution`,
`benchmark-final`, and `benchmark-summarize` as their respective `resume_run_id`
values. Build one concise completed-work artifact, at most 12,000 characters,
then reuse that byte-identical artifact and the byte-identical final-acceptance
evidence for all three completed-work gates. Only the lifecycle-labeled `task`
may differ. A lifecycle gate passes only when the completed MCP
call returns literal `gate.passed: true`; a false or missing `gate.passed` is a
failed gate. Do not add a status lookup after a passed call. Record every
Relentless Inception run id for the final response. Never
substitute a self-review for a required gate, and never blindly duplicate a
timed-out provider call. Call each required fusion or gate operation once unless
it returns an actual MCP error or timeout. A wrapper message saying `Script
running with cell ID` is not an MCP timeout. Immediately wait on that same cell,
using the longest permitted wait interval up to 60 seconds; do not spend a
commentary-only, discovery, planning, or status turn while the call remains
active. Reserve `run_status` for an actual MCP timeout or error, and call it at
most once per 60 seconds. If it reports `running`, wait before checking again and
never resume or redispatch. If it reports `completed`, call the same operation
with the same deterministic run id once to retrieve its cached result. Treat
`rejected`, `failed`, or `aborted` as blocking and never redispatch them. Once
`benchmark-summarize` passes, make no more tool or shell calls: immediately return
a concise answer with the work result, test result, and all six run ids.
