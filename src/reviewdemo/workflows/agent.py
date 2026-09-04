"""AgentSession -- one agent's whole life, as a durable workflow.

The load-bearing rule: **no durable step ever waits for an agent.** Every
ctx.run_typed here is a sub-second tmux/git command; all waiting is ctx.sleep
raced against an awakeable. That matters because Restate's invoker defaults are
inactivity=1min / abort=10min -- a 20-minute agent parked inside a single
ctx.run would be force-retried forever. Because we never block, supervision
(watch, nudge, give up) falls out of the loop instead of being bolted on.

Completion is decided by the *artifact*, not the screen. The Stop hook is a
fast path; going quiet is a hint; the file existing is the proof.
"""

from __future__ import annotations

import os
from datetime import timedelta

import restate

from .. import tmux, worktree
from ..harness import get as get_harness
from ..objects import pane as pane_obj

# Generous relative to any single step, purely as belt-and-braces: nothing in
# this workflow should ever sit in one step for minutes.
agent = restate.Workflow(
    "AgentSession",
    inactivity_timeout=timedelta(minutes=15),
    abort_timeout=timedelta(minutes=30),
    journal_retention=timedelta(days=3),
)

TICK_S = 10
IDLE_TICKS_FOR_DONE = 2      # consecutive quiet reads before believing a turn ended
MAX_NUDGES_PER_TURN = 2


def _read_if_ready(path: str, min_chars: int = 40) -> dict:
    """A turn's real output: did it actually produce the artifact we asked for?"""
    if not path:
        return {"present": True, "chars": 0}
    if not os.path.isfile(path):
        return {"present": False, "chars": 0}
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {"present": False, "chars": 0}
    return {"present": len(text.strip()) >= min_chars, "chars": len(text)}


@agent.main()
async def run(ctx: restate.WorkflowContext, spec: dict) -> dict:
    """spec keys: run_id, agent_id, harness, model, effort, repo, commit,
    workdir, rundir, outfile, turns[{text, expect_file, nudge}], deadline_s
    """
    session = f"rd-{spec['run_id']}-{spec['agent_id']}"
    harness = get_harness(spec["harness"])
    rundir = spec["rundir"]
    ingress = spec.get("ingress", "http://localhost:8080")

    ctx.set("agent", spec["agent_id"])
    ctx.set("session", session)
    ctx.set("phase", "preparing")

    # --- isolate: one disposable checkout per agent ----------------------
    session_uuid = str(ctx.uuid())
    await ctx.run_typed(
        "make-worktree",
        lambda: (
            os.makedirs(rundir, exist_ok=True),
            worktree.add(spec["repo"], spec["workdir"], spec["commit"],
                         soft_reset_to=spec.get("base")),
        )[1],
        restate.RunOptions(type_hint=str),
    )

    # --- spawn the real TUI ----------------------------------------------
    argv = harness.launch_argv(
        model=spec["model"], effort=spec["effort"], rundir=rundir,
        session_uuid=session_uuid, ingress=ingress,
    )
    ctx.set("phase", "spawning")
    await ctx.object_call(
        pane_obj.spawn, key=session,
        arg={"harness": harness.name, "cwd": spec["workdir"], "argv": argv,
             "env": harness.env(rundir=rundir, ingress=ingress)},
    )
    await ctx.object_call(pane_obj.await_ready, key=session, arg={"harness": harness.name})

    # --- drive the conversation, one supervised turn at a time -----------
    nudges_total = 0
    elapsed = 0
    for index, turn in enumerate(spec["turns"]):
        ctx.set("phase", f"turn-{index}")
        got = await _run_turn(
            ctx, spec, harness, session, index, turn, elapsed_start=elapsed
        )
        elapsed = got["elapsed"]
        nudges_total += got["nudges"]

    # --- collect: the artifact is the authority ---------------------------
    outfile = spec["outfile"]
    final = await ctx.run_typed(
        "collect", lambda: _read_if_ready(outfile), restate.RunOptions(type_hint=dict)
    )
    if not final["present"]:
        raise restate.TerminalError(
            f"{spec['agent_id']}: finished its turns but produced no {outfile}", 500
        )

    ctx.set("phase", "done")
    ctx.set("chars", final["chars"])
    return {
        "agent_id": spec["agent_id"],
        "ok": True,
        "outfile": outfile,
        "chars": final["chars"],
        "session": session,
        "nudges": nudges_total,
        "elapsed_s": elapsed,
        "model": spec["model"],
        "effort": spec["effort"],
    }


async def _run_turn(
    ctx: restate.WorkflowContext, spec: dict, harness, session: str,
    index: int, turn: dict, elapsed_start: int,
) -> dict:
    """Send one prompt, then supervise until it produces what we asked for.

    Three completion signals, in order of trustworthiness:
      1. the expected file exists and is non-trivial  -- proof
      2. the Stop hook resolved our awakeable         -- fast, precise
      3. the pane has been quiet for two ticks        -- a hint, nothing more
    A turn that ends without its artifact is a stall, and earns a bounded nudge.
    """
    rundir = spec["rundir"]
    expect_file = turn.get("expect_file") or ""
    deadline = spec.get("deadline_s", 1800)
    use_hook = harness.done_strategy == "hook"

    done_fut = None
    if use_hook:
        # Arm BEFORE submitting, so a fast turn cannot finish before we listen.
        # The hook consumes this file, which is how a one-shot awakeable gives
        # a multi-shot signal across turns.
        awk_id, done_fut = ctx.awakeable(type_hint=dict)
        await ctx.run_typed(
            f"arm-{index}",
            lambda: _write(os.path.join(rundir, "awakeable.id"), awk_id),
            restate.RunOptions(type_hint=str),
        )

    await ctx.object_call(
        pane_obj.submit, key=session,
        arg={"harness": harness.name, "text": turn["text"]},
    )

    elapsed = elapsed_start
    idle_streak = 0
    nudges = 0
    hook_done = False

    while True:
        # Once the hook has fired, its future stays completed and select would
        # spin, so drop it and fall back to plain ticks.
        if done_fut is not None and not hook_done:
            outcome = await restate.select(
                done=done_fut, tick=ctx.sleep(timedelta(seconds=TICK_S), name=f"t{index}-{elapsed}")
            )
            if outcome[0] == "done":
                hook_done = True
            else:
                elapsed += TICK_S
        else:
            await ctx.sleep(timedelta(seconds=TICK_S), name=f"t{index}-{elapsed}")
            elapsed += TICK_S

        snap = await ctx.object_call(
            pane_obj.probe, key=session, arg={"harness": harness.name}
        )
        if snap["state"] == "dead":
            raise restate.TerminalError(f"{session}: agent process died mid-turn", 500)

        idle_streak = idle_streak + 1 if snap["state"] != "busy" else 0
        artifact = await ctx.run_typed(
            f"check-{index}-{elapsed}",
            lambda: _read_if_ready(expect_file),
            restate.RunOptions(type_hint=dict),
        )

        ctx.set("phase", f"turn-{index}:{snap['state']}")
        ctx.set("progress", {
            "turn": index, "state": snap["state"], "elapsed_s": elapsed,
            "nudges": nudges, "artifact_chars": artifact["chars"],
            "tail": snap["tail"],
        })

        # Deadline first, so every path below is bounded.
        if elapsed - elapsed_start > deadline:
            await ctx.object_call(pane_obj.kill, key=session, arg={})
            raise restate.TerminalError(
                f"{session}: turn {index} exceeded {deadline}s", 504
            )

        if not expect_file:
            # No artifact to prove anything, so the screen is all we have -- and
            # the Stop hook alone is NOT enough here: /code-review dispatches a
            # background sub-agent and the session returns to idle within
            # seconds while the review is still running. Requiring sustained
            # quiet is what stops us racing ahead to the next turn.
            if idle_streak >= IDLE_TICKS_FOR_DONE:
                return {"elapsed": elapsed, "nudges": nudges}
            continue

        settled = hook_done or idle_streak >= IDLE_TICKS_FOR_DONE
        if settled and artifact["present"]:
            return {"elapsed": elapsed, "nudges": nudges}

        if settled:
            # It stopped without delivering. Nudge, bounded -- then give up
            # rather than poking a wedged agent forever.
            if nudges >= MAX_NUDGES_PER_TURN:
                raise restate.TerminalError(
                    f"{session}: went idle {nudges} times without writing {expect_file}", 504
                )
            nudges += 1
            if use_hook:
                awk_id, done_fut = ctx.awakeable(type_hint=dict)
                await ctx.run_typed(
                    f"rearm-{index}-{nudges}",
                    lambda: _write(os.path.join(rundir, "awakeable.id"), awk_id),
                    restate.RunOptions(type_hint=str),
                )
                hook_done = False
            await ctx.object_call(
                pane_obj.submit, key=session,
                arg={"harness": harness.name,
                     "text": turn.get("nudge") or turn["text"]},
            )
            idle_streak = 0


def _write(path: str, value: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value


@agent.handler()
async def status(ctx: restate.WorkflowSharedContext) -> dict:
    """Live progress, readable while `run` is still going."""
    return {
        "agent": await ctx.get("agent", type_hint=str),
        "session": await ctx.get("session", type_hint=str),
        "phase": await ctx.get("phase", type_hint=str),
        "progress": await ctx.get("progress", type_hint=dict),
    }
