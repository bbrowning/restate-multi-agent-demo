# Durable multi-agent code review on Restate

Orchestrating **real, interactive coding-agent TUIs** with
[Restate](https://docs.restate.dev/) durable workflows.

Give it a commit. It fans out **two independent code reviews** (Claude Opus 5
and Sonnet 5, both at low effort), waits for both, then runs a **third agent**
that merges them, de-duplicates, adjudicates where they disagree, and writes a
report for a human.

The interesting part is not the review — it is **how the agents run**. Each one
is a genuine interactive `claude` session living in its own **tmux pane**, in
its own **git worktree**, driven exactly the way a person drives it: spawned,
typed into, watched, and nudged when it stalls. No `claude -p` one-shots, no
Agent SDK, no API-only loop. You can `tmux attach` mid-run and watch a review
happen in front of you.

Restate's job is to make that orchestration **durable**: kill the orchestrator
mid-run and the workflow picks up exactly where it left off, while the agents
themselves keep working in their panes. Kill an *agent* and it is respawned
with `--resume` into the same worktree, so it still remembers what it was
doing. Each agent's workspace is checkpointed to a git sha and handed to the
next node.

```
ReviewWorkflow  (Workflow, key = run id)             fan-out / fan-in / report
     ├── AgentSession  (Workflow, key = "<run>-opus")       one agent's whole life
     ├── AgentSession  (Workflow, key = "<run>-sonnet")
     └── AgentSession  (Workflow, key = "<run>-combiner")
                │
                └── TmuxPane  (Virtual Object, key = session name)
                                                     serialized actuation on one pane
```

## What the output actually looks like

Real output from this demo, reviewing a refactor commit — see
[`docs/sample-report.md`](docs/sample-report.md) for the whole thing, and the
two raw inputs it merged
([opus](docs/sample-review-opus.md), [sonnet](docs/sample-review-sonnet.md)).

The merged report is not a concatenation. The third agent verifies claims
against the code and overrules a reviewer when it is wrong:

> ### B. (sonnet) Function-local import in `workspace_from_labels` — `labels.py:181`
> **Real observation, but sonnet's stated rationale is wrong.** […] Sonnet
> guessed it was a circular-import workaround; it is not. `session_env` imports
> only `paude.agents.base` at runtime and never imports `labels`, so hoisting
> the import to module scope would not create a cycle. That makes this
> incidental rather than intentional — a genuine (if trivial) nit worth
> hoisting, and one sonnet flagged for the wrong reason.

It also reports honestly when the two reviewers share nothing:

> ## 2. Agreed findings
> None. Both reviews concluded "no bugs found" on the non-test hunks […] There
> is no issue raised by both reviewers to de-duplicate.

## Quickstart

Requires Linux/macOS, Python 3.11+, [`uv`](https://docs.astral.sh/uv/), `git`,
`tmux`, and a working `claude` CLI on `PATH`. No Docker or Node needed.

```bash
./scripts/bootstrap.sh          # fetch restate-server + restate CLI, sync deps
./scripts/dev.sh up             # start server + SDK endpoint, register services
./scripts/run.sh 883ea4f        # review a commit (RD_REPO selects the repo)

tmux ls                         # rd-<run>-opus, rd-<run>-sonnet, rd-<run>-combiner
tmux attach -t rd-<run>-opus    # watch a real review happen; Ctrl-b d to detach
open http://localhost:9070      # Restate UI: per-invocation journals, step by step
```

Output lands in `runs/<run-id>/`: `report.md`, `reviews/opus.md`,
`reviews/sonnet.md`, plus each agent's `turns.jsonl`.

```bash
uv run python -m pytest tests/ -q   # 18 tests, ~2s, no LLM calls
./scripts/dev.sh status | logs | down
./scripts/clean.sh <run-id>         # release worktrees + panes for a run
```

`RD_REPO` defaults to `/pvc/workspace`, the repo this was developed against
([paude](https://github.com/bbrowning/paude)). It is used purely as a source of
real commits to review — no code or design is borrowed from it. Point `RD_REPO`
at any git repo.

## How a run works

1. **Pin the commit.** Resolve the sha and its parent once, so all three agents
   review exactly the same thing.
2. **Isolate.** Each agent gets `git worktree add --detach <dir> <sha>`,
   followed by `git reset --soft <parent>`. HEAD becomes the parent and the
   commit's change appears as **pending work in the working tree** — so
   `/code-review low` with no target reviews precisely that change, the way a
   human runs it on their own uncommitted work.
3. **Spawn.** `tmux new-session -d` running `claude --model … --effort low
   --permission-mode bypassPermissions --session-id … --settings '<json>'`.
   The generated settings install a `Stop` hook.
4. **Turn 1** — type `/code-review low` into the pane and supervise.
5. **Turn 2** — once that settles, type *"write the review you just produced to
   `<file>`"*. Splitting this out makes the **artifact**, not the screen, the
   proof of completion.
6. **Fan in.** Both reviewer futures go to `restate.gather`. The combiner then
   gets its own worktree and pane, reads both review files, and writes the
   merged report.
7. **Checkpoint.** Each agent's worktree is committed, freezing the tree as it
   left it; the sha goes into the journal and the result. The combiner starts
   from a reviewer's checkpoint rather than the raw commit — see
   [Workspace durability](#workspace-durability-checkpointing-a-filesystem-and-passing-it-on).
8. **Assemble.** Provenance header (commit, models, efforts, nudge counts,
   respawns, checkpoints, wall time) is prepended to the combiner's markdown.

## What Restate is doing, for people who haven't used it

Restate is a single binary that sits in front of your handlers and **journals
every step**. If your process dies, Restate re-invokes the handler from the top
and **replays the journal**: already-completed steps return their recorded
results instead of running again. The upshot is that your workflow is ordinary
sequential Python that happens to survive crashes, restarts and deploys.

Three primitives are used here, one per layer:

| Primitive | Used for | Why that one |
|---|---|---|
| **Workflow** | `ReviewWorkflow`, `AgentSession` | keyed, runs **exactly once per key**, holds state a shared handler can read live |
| **Virtual Object** | `TmuxPane` | per-key handler **serialization** — a free mutex over one pane |
| **Awakeable** | turn completion | an external process (our Stop hook) resolves it with one `curl` |

### The load-bearing rule: never wait inside a durable step

Restate's invoker defaults are `inactivity-timeout=1min` and
`abort-timeout=10min`. A 20-minute agent parked inside a single `ctx.run` gets
force-retried **forever**. This is the trap a first-time user falls into, and
it shapes the whole design.

So no step here ever waits for an agent. Every `ctx.run_typed` is a sub-second
tmux or git command, and all waiting is a durable timer raced against an
awakeable:

```python
outcome = await restate.select(
    done=done_future,                        # the Stop hook resolves this
    tick=ctx.sleep(timedelta(seconds=10)),   # durable timer; handler suspends
)
```

Because the workflow wakes every 10 seconds anyway, **supervision falls out of
the loop for free**: each tick probes the pane, records progress into workflow
state (visible in the UI and via a `status` handler), and nudges a stalled
agent. Supervision isn't bolted on — it *is* the wait.

Re-passing the same pending awakeable into `select` on each iteration is safe:
`wait_completed` checks `is_completed()` per future and only polls the
uncompleted handles.

### The pane is never the completion authority

Screen-scraping tells you whether an agent is *typing*, not whether it is
*done*, and certainly not whether it is *right*. Completion is decided in this
order:

1. **the expected file exists and is non-trivial** — proof
2. **the Stop hook resolved our awakeable** — fast and precise, but see below
3. **the pane has been quiet for two consecutive ticks** — a hint, nothing more

A turn that goes quiet *without* producing its artifact is a stall, and earns a
bounded nudge ("you have not written `<file>` yet…") before the workflow gives
up with a `TerminalError`.

### Multi-shot completion from a one-shot awakeable

Awakeables resolve once, but the `Stop` hook fires at the end of *every* turn.
So the workflow writes the current awakeable id to `<rundir>/awakeable.id`
before each turn, and `hooks/on_stop.sh` reads that file, consumes it, and
`curl`s the resolve endpoint. No extra Restate concepts required.

## Why bother? Restate vs. the alternatives

This demo exists to answer a specific question: **is a durable-execution engine
worth it for agent orchestration, versus the usual approaches?**

**vs. a shell script / Makefile.** Fine until something takes 20 minutes and
dies at minute 19. There is no way to resume, no record of which agent finished,
and parallel fan-out means juggling PIDs. You end up writing a state machine in
`jq` and text files.

**vs. [gascity](https://github.com/gastownhall/gascity)-style orchestration.**
gascity does the hard TUI part extremely well (this demo borrows its tmux
mechanics wholesale and credits them below), but its durability substrate is
flock'd JSON files plus a ~30-second reconcile tick. That works, and it is a lot
of code: a bead store, an event bus, a nudge queue with leases and attempt caps,
a health patrol, crash-loop quarantine. In Restate most of that is either free
or one primitive:

| gascity mechanism | Restate equivalent |
|---|---|
| per-session nudge lock (flock + `state.json`) | Virtual Object keyed by session name — exclusive handlers serialize automatically |
| nudge queue with `attempts` / `lease_until` / `deliver_after` | `RunOptions(max_attempts=…)` + `ctx.sleep` + journal replay |
| 30s reconcile tick to notice stalls | the supervision loop's own `ctx.sleep`, exact and per-agent |
| bead store as durable state | workflow K/V state + the journal |
| "did this step already happen?" bookkeeping | replay: completed steps return recorded results |

The honest trade: you take on a server process and a mental model (determinism
outside `ctx.run`, no bare `except Exception:`), and you give up gascity's
richer agent ecosystem. What you get is that **crash recovery stops being your
problem**.

**vs. [crucible](https://github.com/neuralmagic/crucible)** (Neural Magic / Red
Hat; Rust; an autonomous goal-directed research-loop engine). The closest
sibling to this demo, and worth being precise about because it overlaps in
shape while differing completely in substrate.

*Where it does the same thing:* crucible's `examples/adversarial-review` is
almost exactly this demo's graph — two isolated reviewers fanned out from one
candidate, joined by a gate, with per-task model and effort (Opus at high
effort blocking, Sonnet advisory via `required = false`). **The fan-out/fan-in
shape is not novel here**, and crucible's join semantics are better specified
than mine: `join = "passed"`, advisory tasks, budget-as-terminal-state, and
fail-closed truncation when a required task can never run. It even documents
the third "synthesize" agent that merges the reviewers — as a recipe, not a
shipped example.

*Where it differs fundamentally:* crucible drives `claude -p --output-format
stream-json` over **pipes** — `Stdio::piped()`, no pty, and zero tmux anywhere
in the repo. There is no live session to attach to and nothing to type into;
its human-in-the-loop is `STEER.md`, appended between iterations. That is a
deliberate, defensible choice for an unattended optimization loop. It is simply
a different thing from "watch the agent work and nudge it," which is what this
demo is about.

*Where crucible is well ahead:* a **frozen judge** the agent is structurally
prevented from tuning (the engine hands the agent a `World`, never a `Judge`),
capability admission that refuses a graph before dispatch, budget as a
first-class terminal state, deny-by-default egress with a host-side credential
broker, Kubernetes sandbox pods, and a real OTel/S3/PR/Slack observability
stack. None of that exists here.

*On durability, the comparison is the interesting part.* crucible hand-rolls
what Restate provides: an append-only `state/session.jsonl` as source of truth,
a `--resume` path with a recovery classifier that names how a run died
(`DiedMidTurn`, `DiedDeciding`, `DiedInPlanTask`, …), a content-hash-keyed step
ledger, and — notably — `flock(2)` on a sidecar file plus write-tmp-then-rename
for its agent-session ledger, the same mechanism this README attributes to
gascity. Their own ADRs are candid about the limits: the step ledger
"deliberately does not touch" the plan executor, `died_in_plan_task` is "coarse
by construction" because task results are batched, and the ledger's storage is
an `emptyDir` today so replay "covers a broker restart within a live pod, not
pod death." That is precisely the class of work a durable-execution runtime
does for you.

*Maturity, stated plainly:* ~5 weeks old at the time of writing, 259 commits,
effectively one author, one release, 8 stars — and extremely active (44 commits
in a week). Neither a warning nor an endorsement; just don't read it as widely
deployed, or as abandoned.

**vs. Temporal / Airflow.** Same durable-execution family. Restate is a single
self-contained binary with no external database (a ~45MB download, ~200MB on
disk), which is why this whole demo bootstraps on a box with no Docker and no
Node. Airflow in particular is built for scheduled DAGs of batch tasks, not for
a long-lived interactive process you type into.

**vs. an agent framework (LangGraph, the Agents SDK, Restate's own agent
integrations).** Those orchestrate *API calls*. This demo deliberately drives
**the actual agent harness** — the same binary, TUI, skills, hooks, permission
modes and settings a human uses. If your reason for multi-agent work is "I want
what I get in my terminal, but three of them, supervised," an API-level
framework does not give you that.

### Two kinds of death, two kinds of recovery

**The orchestrator dies.** Restate's own job. Invocations go to `backing-off`,
the panes keep working, and when the endpoint returns the journal replays and
the run continues.

**An agent dies.** Restate cannot help here — a dead pane is outside its world
— so the workflow handles it, using state that outlived the process:

- the **git worktree** is still on disk
- the harness's **own transcript** is still on disk, under the session id we
  pinned at launch (`--session-id`), which is why its path is predictable
- **finished artifacts** were written *outside* the worktree by design

So a dead pane is respawned with `harness.resume_argv()` — for Claude,
`--resume <uuid>` in the same worktree — and the current turn is re-asked using
the nudge wording. Verified by killing a reviewer's pane mid-review: it came
back and finished writing findings it had produced *before* it was killed, so
this restores the agent's memory, not just its process. Bounded by
`MAX_RESPAWNS`, and reported as `respawns=1` in the run result.

A harness that returns `None` from `resume_argv` (the codex stub) fails
honestly instead of respawning an agent with amnesia.

### The payoff, demonstrated

```bash
./scripts/run.sh 883ea4f
tmux kill-session -t rd-app        # kill the orchestrator mid-run
./scripts/dev.sh restart           # bring it back
```

The agent panes keep running (tmux is the process substrate). The invocations
sit in `backing-off`. When the endpoint returns, Restate replays the journals
and the workflow carries on to completion. This first happened by accident
during development and recovered unaided; it is now a deliberate test.

Re-submitting a run id is a no-op — `409 the workflow method was already
invoked`. Exactly-once per key, for free.

Inspect anything at any time:

```bash
./bin/restate invocations list
curl -s localhost:9070/query --json '{"query":"SELECT target_service_key, status FROM sys_invocation"}'
curl -s -X POST --max-time 5 localhost:8080/restate/call/ReviewWorkflow/<run>/status
```

## Workspace durability: checkpointing a filesystem and passing it on

A distinct question from crash recovery: **can you freeze the filesystem inside
an agent's workspace and hand that exact state to the next node?** Worth
answering carefully, because the honest answer starts with a "no".

### Restate does not do this, and that is the right layer for it not to

Restate journals **values** — small JSON — and its own guidance is to keep
blobs out of the journal. It has no concept of a filesystem, a volume, or a
workspace. So there is no "how Restate does it" to evaluate.

What Restate gives you is a **durable, replay-deterministic pointer**. You
supply the bytes. The split matters more than it first appears: because journal
entries replay identically, a checkpoint id recorded on the first attempt is
the *same* id after a crash, so the downstream node reconstructs the *same*
tree rather than something merely equivalent.

### What this demo does

Each agent's workspace is already a git worktree, so a commit *is* a
content-addressed snapshot of the whole tree — cheap (objects are shared with
the source repo) and reducible to a 40-byte pointer.

```python
# end of AgentSession: freeze the tree as this agent left it
checkpoint_sha = await ctx.run_typed(
    "checkpoint-workspace",
    lambda: worktree.checkpoint(spec["workdir"], f"...{spec['agent_id']}"),
)
```

```python
# ReviewWorkflow: the combiner starts FROM a reviewer's frozen workspace
inherit_from = next(r for r in results if r["agent_id"] == inherit_id)
start_commit = inherit_from["checkpoint"] or meta["sha"]
```

The combiner's worktree is created at that sha and soft-reset to the original
parent, so it sees the reviewed change **plus anything the reviewer left on
disk**, all as pending work. Verified by reflog on a real run:

```
0098a69 HEAD@{2}:                      # created at opus's checkpoint
8d6eb1c HEAD@{1}: reset: moving to …   # soft-reset to the original parent
8b3616b HEAD@{0}: commit: reviewdemo checkpoint: r6/combiner
```

`checkpoint()` returns `None` when the agent changed nothing, and the run falls
back to the pristine commit — no empty snapshots. Set `inherit_workspace` to
`null` in the request to disable the handoff entirely.

### How crucible does it, and how the two compare

crucible's answer is also git, applied differently. Its `World` abstraction
defines `snapshot` as a git commit and `restore` as `git reset --hard`, which
is what powers its keep-or-discard loop. Between plan tasks it uses **one
shared checkout**; a task marked `isolation = "worktree"` actually gets a
`git clone --local --no-checkout` (hardlinked objects, despite the flag name),
and — importantly — **an isolated task's edits are discarded**. What leaves an
isolated task is its declared structured output, not its tree. For untracked
derived files it has `[workspace].carry_forward` (git-exclude plus sparing them
from the discard) and `[[workspace.artifact]]` to publish to S3 or a PR.

On resume, crucible is explicit that reconstructing a task's workspace is a
**non-goal**. RFC-0002 `C-PLAYBOOK-RESUME`:

> "It MUST NOT attempt to reconstruct the workspace as some particular task
> left it: when concurrent tasks were in flight at the interruption, no such
> state is well defined, and a task's declared output is the only part of its
> work the contract ever promised would survive."

That is a reasonable contract, not a bug — it buys them a simple invariant. But
it does mean the two systems answer the question differently:

| | crucible | this demo (Restate) |
|---|---|---|
| Snapshot mechanism | git commit (`World::snapshot`) | git commit (`worktree.checkpoint`) |
| Between parallel nodes | shared checkout; **isolated task edits discarded** | explicit checkpoint sha handed to the next node |
| Untracked/derived files | `carry_forward` + artifact publish | **not handled** — see below |
| After a crash | pristine checkout + declared outputs, *not* the tree as left | the journaled sha replays identically → same tree |
| Who stores the bytes | git, plus S3 for artifacts | git (Restate stores only the pointer) |

### So: is it good enough?

**If your checkpoint content is git-trackable — yes, and the replay determinism
is a genuine edge.** A journaled sha survives orchestrator death and gives the
downstream node bit-identical state, which is exactly what crucible's resume
contract declines to promise.

**If it is not git-trackable, neither system solves it and this demo solves it
less.** Large build outputs, `node_modules`, virtualenvs, caches, binaries: git
is a poor snapshot store for those, and there is no overlayfs, container image
commit, or volume snapshot anywhere in either project. crucible at least has
`carry_forward` and artifact publishing as a partial answer; this demo has
nothing. The Restate-idiomatic fix is unchanged in shape — put the bytes in a
content-addressed store (S3, an OCI layer, a btrfs/ZFS snapshot) and journal
the digest — but that store is something you would still have to build.

**The constraint to design around** is that pointer discipline is mandatory,
not advisory. Restate state is not a blob store, so "just return the workspace
from the handler" is never the answer.

## What we learned driving a real TUI

Every item below was **observed on a live Claude Code 2.1.260 session**, not
guessed. They are the reason the code looks the way it does, and they are the
part most likely to save you time if you build something similar.

- **The ready prompt is `❯` followed by U+00A0**, a non-breaking space.
  Matching `"❯ "` with an ASCII space silently never fires.
- **Claude pre-fills the input box with a suggested follow-up** after a turn
  (e.g. `show the diff of that commit`). It is a real editable draft, not grey
  placeholder text — so a bare `Enter` submits *Claude's* idea instead of
  yours. Hence `C-u` before every send.
- **`/code-review` dispatches a background sub-agent** and the session returns
  to idle within seconds while the review is still running. Two consequences,
  both of which bit on the first run: the Stop hook alone is *not* proof a turn
  finished, and "not busy" cannot be read as "my keystrokes never landed". The
  first run double-submitted the review and then nagged the agent to save a
  review it had not finished. Fixed by requiring sustained quiet for turns with
  no artifact, and by accepting an emptied input box as submit confirmation.
- **A detached tmux pane silently drops pasted input.** Force a redraw with
  `resize-pane -y -1/+1` before sending. (Straight from gascity.)
- **Reviewing a commit needs a soft reset.** In a clean checkout `git diff` is
  empty, so a harness that falls back to "review the current diff" reviews
  *nothing at all* — and reports success.
- **A workflow's shared handler blocks if that workflow hasn't started yet.**
  Polling the combiner's `status` before the reviewers finish hangs the poller.
  Every status request needs a timeout.
- **Plain dataclasses are not JSON-serializable by the SDK's `DefaultSerde`.**
  Handler I/O here is plain dicts.
- **Claude's own `--worktree` / `--tmux` flags can't be used for this.**
  `--tmux` requires `--worktree`, and `--worktree`'s base ref is a *setting*
  (`fresh`|`head`) with no per-invocation base — so it cannot check out an
  arbitrary commit. There is also no shell-level "send a prompt to a running
  session" (`claude --bg --resume` forks if the session is busy). Hence tmux.

## Adding another harness

The workflow layer never mentions a specific agent CLI. A harness supplies
launch argv, busy/ready patterns, submit keys, a completion strategy, and a
review command:

```python
class CodexHarness:
    name = "codex"
    busy_patterns  = (re.compile(r"esc to interrupt", re.I),)
    ready_patterns = (re.compile(r"^[>❯▌]\s", re.M),)
    submit_keys    = ("Enter",)
    done_strategy  = "quiescence"   # no turn-end hook: watch the pane instead
```

`done_strategy` is the important axis. Claude can push a precise turn-end
signal via its `Stop` hook; a harness without hooks falls back to pane
quiescence, and the artifact check keeps that honest.

## Limitations, and what is not proven

- **`harness/codex.py` is untested.** There is no codex binary on the machine
  this was built on. Its patterns are *guesses* that should be replaced by
  reading a live TUI, the way the claude ones were. It exists to show the seam
  is real — not to claim codex works.
- **TUI scraping will rot.** The busy/ready regexes are tied to a specific
  Claude Code build. They are confined to the adapter and used only for
  liveness, never as proof of completion, which is the mitigation — but expect
  to re-derive them.
- **`bypassPermissions`** is used so sessions never block on a prompt. That is
  safe *here* because each agent is confined to a disposable worktree, but it
  is a real choice to make consciously.
- **Single machine.** Panes are local to one tmux server. Distributing this
  would mean a remote pane provider (gascity has one; this does not).
- **Checkpoints only capture git-trackable files.** Untracked build output,
  virtualenvs and caches are not snapshotted, and there is no content-addressed
  store for them. See the workspace-durability section for what that would take.
- **Cost is not metered.** Three low-effort sessions per run is cheap, but
  nothing here enforces a budget.

## Layout

```
src/reviewdemo/
  app.py                  restate.app([...]) served by hypercorn on :9080
  tmux.py                 generic primitives; knows nothing about agents
  worktree.py             worktree add/remove + the soft-reset trick
  objects/pane.py         TmuxPane virtual object: spawn/await_ready/submit/probe/kill
  workflows/agent.py      AgentSession: one agent's turns + the supervision loop
  workflows/review.py     ReviewWorkflow: fan-out, gather, combine, assemble
  harness/                the seam: claude.py (real), codex.py (untested stub)
  hooks/on_stop.sh        Stop hook -> resolves the awakeable, appends turns.jsonl
scripts/                  bootstrap / dev / run / clean
tests/test_harness.py     18 tests: pane classification, checkpoints, real tmux
docs/                     sample outputs from a real run
```

Roughly 1,600 lines of Python and shell including tests and scripts, a good
share of it comments explaining the sharp edges above.

## Credits

The tmux mechanics — wake-before-send, clear-the-draft, literal send with a
paste-buffer fallback, busy-indicator-first idle detection, submit confirmation
with bounded re-send — are taken from
[gascity](https://github.com/gastownhall/gascity), which worked them out the
hard way. This demo's contribution is showing what those mechanics look like
when a durable-execution engine owns the state instead of flock'd JSON.
