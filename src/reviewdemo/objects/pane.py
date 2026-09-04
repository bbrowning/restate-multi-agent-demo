"""TmuxPane -- a Restate Virtual Object keyed by tmux session name.

Why a Virtual Object rather than plain functions: Restate serialises exclusive
handlers per key. That gives us, for free, the per-session mutex that gascity
has to build out of flock'd JSON -- two workflows can never interleave
keystrokes into the same pane. `probe` is a *shared* handler, so status polling
still runs concurrently with an in-flight submit.

Everything here is harness-agnostic; behaviour comes from the Harness adapter.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import restate

from .. import tmux
from ..harness import classify, get as get_harness, prompt_empty

pane = restate.VirtualObject("TmuxPane")

# How long to wait for a freshly spawned TUI to start accepting keys.
READY_TIMEOUT_S = 90
# gascity's numbers, and they hold up: re-send at most a few times, and only
# while the pane is still idle.
SUBMIT_MAX_SENDS = 3
SUBMIT_CONFIRM_POLLS = 5
SUBMIT_CONFIRM_INTERVAL_S = 0.4


@pane.handler()
async def spawn(ctx: restate.ObjectContext, req: dict) -> dict:
    """Start the agent detached. Idempotent: replays do not start a second one.

    req: {harness, cwd, argv, env}
    """
    session = ctx.key()

    async def _spawn() -> dict:
        tmux.new_session(session, req["cwd"], req["argv"], req.get("env") or {})
        return {"session": session, "started": True}

    out = await ctx.run_typed("tmux-new-session", _spawn, restate.RunOptions(type_hint=dict))
    ctx.set("cwd", req["cwd"])
    ctx.set("harness", req["harness"])
    return out


@pane.handler()
async def await_ready(ctx: restate.ObjectContext, req: dict) -> dict:
    """Block until the TUI is accepting input.

    Uses ctx.sleep between polls, so the handler suspends rather than holding
    the invocation open -- this is why a slow-starting TUI costs nothing.
    """
    session = ctx.key()
    harness = get_harness(req["harness"])
    waited = 0
    while waited < READY_TIMEOUT_S:
        state = await ctx.run_typed(
            f"classify-{waited}",
            lambda: classify(harness, tmux.capture(session, 60)),
            restate.RunOptions(type_hint=str),
        )
        if state in ("ready", "busy"):
            return {"ready": True, "state": state, "waited_s": waited}
        dead = await ctx.run_typed(
            f"dead-{waited}", lambda: tmux.pane_dead(session), restate.RunOptions(type_hint=bool)
        )
        if dead:
            raise restate.TerminalError(f"{session}: agent exited before becoming ready", 500)
        await ctx.sleep(timedelta(seconds=3), name=f"wait-ready-{waited}")
        waited += 3
    raise restate.TerminalError(f"{session}: TUI never became ready in {READY_TIMEOUT_S}s", 504)


@pane.handler()
async def submit(ctx: restate.ObjectContext, req: dict) -> dict:
    """Type a prompt into the pane and confirm it was actually submitted.

    This is the single most failure-prone operation in the whole demo, so it is
    one durable step -- never partially replayed -- and it is self-correcting:
    if a retry finds the pane already busy, the earlier attempt landed and we
    return instead of double-submitting.

    req: {harness, text}
    """
    session = ctx.key()
    harness = get_harness(req["harness"])
    text = req["text"]

    async def _submit() -> dict:
        pane_text = tmux.capture(session, 60)
        if classify(harness, pane_text) == "busy":
            # A previous attempt already landed (or the agent is mid-turn).
            return {"delivered": True, "sends": 0, "note": "already busy"}

        for attempt in range(1, SUBMIT_MAX_SENDS + 1):
            # A detached TUI silently drops pasted input until it redraws.
            tmux.wake_pane(session)
            await asyncio.sleep(0.15)
            # Claude pre-fills the box with a *suggested* follow-up after each
            # turn. Without this clear, Enter submits Claude's idea, not ours.
            tmux.send_keys(session, "C-u")
            await asyncio.sleep(0.15)
            tmux.send_literal(session, text)
            await asyncio.sleep(0.5)

            # Phase 1: did the text actually reach the input box? If not, the
            # pane was not listening -- retry rather than pressing Enter on an
            # empty box.
            if prompt_empty(harness, tmux.capture(session, 60)) is True:
                continue

            tmux.wake_pane(session)
            for key in harness.submit_keys:
                tmux.send_keys(session, key)
                await asyncio.sleep(0.1)

            # Phase 2: confirm it left the box. Busy is the strongest signal,
            # but a turn that dispatches a background sub-agent goes idle at
            # once -- so an emptied input box counts as delivered too.
            for _ in range(SUBMIT_CONFIRM_POLLS):
                await asyncio.sleep(SUBMIT_CONFIRM_INTERVAL_S)
                current = tmux.capture(session, 60)
                if classify(harness, current) == "busy":
                    return {"delivered": True, "sends": attempt, "via": "busy"}
                if prompt_empty(harness, current) is True:
                    return {"delivered": True, "sends": attempt, "via": "box-cleared"}
            if tmux.pane_dead(session):
                raise restate.TerminalError(f"{session}: pane died during submit", 500)

        # Some harnesses have no readable busy indicator; hand-off is the best
        # signal available. Say so rather than pretending it is confirmed.
        return {"delivered": False, "sends": SUBMIT_MAX_SENDS, "note": "unconfirmed"}

    out = await ctx.run_typed(
        "submit-prompt", _submit, restate.RunOptions(type_hint=dict, max_attempts=2)
    )
    turns = (await ctx.get("turns", type_hint=int)) or 0
    ctx.set("turns", turns + 1)
    return out


@pane.handler(kind="shared")
async def probe(ctx: restate.ObjectSharedContext, req: dict) -> dict:
    """Read-only liveness snapshot. Shared, so it runs during a submit.

    Liveness only. This never decides that work is *finished* -- the produced
    artifact does. Screen-scraping tells you whether an agent is typing, not
    whether it is right.
    """
    session = ctx.key()
    harness = get_harness(req["harness"])

    def _snap() -> dict:
        if tmux.pane_dead(session):
            return {"state": "dead", "tail": "", "lines": 0}
        text = tmux.capture(session, 60)
        lines = [ln for ln in text.split("\n") if ln.strip()]
        return {
            "state": classify(harness, text),
            "tail": "\n".join(lines[-6:])[-1200:],
            "lines": len(lines),
        }

    return await ctx.run_typed("capture", _snap, restate.RunOptions(type_hint=dict))


@pane.handler()
async def kill(ctx: restate.ObjectContext, req: dict) -> dict:
    session = ctx.key()
    return await ctx.run_typed(
        "kill-session",
        lambda: {"killed": (tmux.kill_session(session), True)[1]},
        restate.RunOptions(type_hint=dict),
    )
