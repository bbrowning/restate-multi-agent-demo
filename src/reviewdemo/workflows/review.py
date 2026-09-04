"""ReviewWorkflow -- fan out two reviewers, fan in, then synthesise.

This is the layer that would be a pile of bash and a reconcile tick elsewhere.
Here it is ordinary sequential Python: the two reviewers are just two futures
handed to restate.gather, and the whole thing survives the process dying.

Nothing in this module mentions a specific agent CLI.
"""

from __future__ import annotations

import json
import pathlib
import os
from datetime import timedelta

import restate

from .. import worktree
from ..harness import get as get_harness
from . import agent as agent_wf

review = restate.Workflow(
    "ReviewWorkflow",
    inactivity_timeout=timedelta(minutes=15),
    abort_timeout=timedelta(minutes=30),
    journal_retention=timedelta(days=3),
)

# .../<repo>/src/reviewdemo/workflows/review.py -> parents[3] is the repo root.
RUNS_ROOT = os.environ.get(
    "RD_RUNS_ROOT", str(pathlib.Path(__file__).resolve().parents[3] / "runs")
)

DEFAULT_REVIEWERS = [
    {"agent_id": "opus", "harness": "claude", "model": "opus", "effort": "low"},
    {"agent_id": "sonnet", "harness": "claude", "model": "sonnet", "effort": "low"},
]
DEFAULT_COMBINER = {
    "agent_id": "combiner", "harness": "claude", "model": "opus", "effort": "low",
}

SAVE_TURN = (
    "Write the complete code review you just produced to {outfile} as GitHub-flavored "
    "markdown. Include every finding with its file:line, severity, and rationale. "
    "If you found no issues, say so explicitly. Write the file, then stop."
)
SAVE_NUDGE = (
    "You have not written {outfile} yet. Write your full review there now as markdown, "
    "then stop."
)

COMBINE_TURN = (
    "You are merging two independent code reviews of the same commit ({sha} — {subject}).\n\n"
    "Review A (from {a_model}): {a_file}\n"
    "Review B (from {b_model}): {b_file}\n\n"
    "Read both files. The commit itself is checked out in your working directory, so verify "
    "claims against the real code rather than trusting either reviewer.\n\n"
    "Write a single human-readable report to {outfile} as markdown with these sections:\n"
    "1. Verdict — two sentences: is this commit safe to ship?\n"
    "2. Agreed findings — issues BOTH reviewers raised, de-duplicated, most severe first. "
    "Merge differing wordings of the same issue into one entry.\n"
    "3. Unique findings — raised by only one reviewer, labelled with which one, and your "
    "judgement on whether it is real.\n"
    "4. Inconsistencies — places the two reviews contradict each other, with your adjudication.\n"
    "5. Noise — anything you judge a false positive, and why.\n\n"
    "Cite file:line. Do not invent findings neither reviewer made. Write the file, then stop."
)
COMBINE_NUDGE = "You have not written {outfile} yet. Write the merged report there now, then stop."


@review.main()
async def run(ctx: restate.WorkflowContext, req: dict) -> dict:
    """req: {repo, commit, level?, reviewers?, combiner?, ingress?}"""
    run_id = ctx.key()
    repo = req.get("repo", "/pvc/workspace")
    level = req.get("level", "low")
    ingress = req.get("ingress", "http://localhost:8080")
    reviewers = req.get("reviewers") or DEFAULT_REVIEWERS
    combiner = req.get("combiner") or DEFAULT_COMBINER

    root = os.path.join(RUNS_ROOT, run_id)

    # --- pin the commit once, so every agent reviews the same thing -------
    meta = await ctx.run_typed(
        "resolve-commit",
        lambda: _resolve(repo, req["commit"], root),
        restate.RunOptions(type_hint=dict),
    )
    ctx.set("commit", meta["sha"][:12])
    ctx.set("subject", meta["subject"])
    ctx.set("phase", "reviewing")

    # --- fan out ----------------------------------------------------------
    # Two ctx.workflow_call futures, not awaited yet: that is the entire
    # parallelism story. gather() blocks until both are done, and survives a
    # restart of this process because each is its own durable invocation.
    futures = []
    for spec_in in reviewers:
        spec = _reviewer_spec(
            run_id, spec_in, repo, meta, root, level, ingress, req.get("deadline_s", 1800)
        )
        futures.append(
            ctx.workflow_call(agent_wf.run, key=f"{run_id}-{spec_in['agent_id']}", arg=spec)
        )

    settled = await restate.gather(*futures)
    results = [await f for f in settled]     # re-raises any reviewer's failure
    ctx.set("phase", "combining")
    ctx.set("reviews", [{"agent": r["agent_id"], "chars": r["chars"]} for r in results])

    # --- fan in: synthesise ----------------------------------------------
    a, b = results[0], results[1]
    # Hand the combiner a workspace, not just files: it starts from whichever
    # reviewer's frozen checkpoint is named (default: the first), so anything
    # that reviewer left on disk travels with it. Set inherit_workspace=null to
    # start from the pristine commit instead.
    inherit_id = req.get("inherit_workspace", reviewers[0]["agent_id"])
    inherit_from = next((r for r in results if r["agent_id"] == inherit_id), None)
    combiner_spec = _combiner_spec(
        run_id, combiner, repo, meta, root, a, b, ingress, req.get("deadline_s", 1800),
        inherit_from=inherit_from,
    )
    combined = await ctx.workflow_call(
        agent_wf.run, key=f"{run_id}-{combiner['agent_id']}", arg=combiner_spec
    )

    report = await ctx.run_typed(
        "assemble-report",
        lambda: _assemble(root, meta, results, combined),
        restate.RunOptions(type_hint=dict),
    )

    ctx.set("phase", "done")
    return {
        "run_id": run_id,
        "commit": meta["sha"],
        "subject": meta["subject"],
        "report": report["path"],
        "reviews": [r["outfile"] for r in results],
        "agents": [
            {"agent": r["agent_id"], "model": r["model"], "effort": r["effort"],
             "chars": r["chars"], "nudges": r["nudges"],
             "respawns": r.get("respawns", 0), "elapsed_s": r["elapsed_s"],
             "checkpoint": r.get("checkpoint")}
            for r in results + [combined]
        ],
    }


@review.handler()
async def status(ctx: restate.WorkflowSharedContext) -> dict:
    return {
        "phase": await ctx.get("phase", type_hint=str),
        "commit": await ctx.get("commit", type_hint=str),
        "subject": await ctx.get("subject", type_hint=str),
        "reviews": await ctx.get("reviews", type_hint=list),
    }


# --------------------------------------------------------------------------
# plain helpers (no ctx use: these run inside ctx.run_typed)
# --------------------------------------------------------------------------

def _resolve(repo: str, commit: str, root: str) -> dict:
    sha = worktree.resolve(repo, commit)
    meta = worktree.describe(repo, sha)
    meta["base"] = worktree.diff_base(repo, sha)
    os.makedirs(os.path.join(root, "reviews"), exist_ok=True)
    with open(os.path.join(root, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
    return meta


def _reviewer_spec(run_id, spec_in, repo, meta, root, level, ingress, deadline) -> dict:
    agent_id = spec_in["agent_id"]
    harness = get_harness(spec_in["harness"])
    outfile = os.path.join(root, "reviews", f"{agent_id}.md")
    return {
        "run_id": run_id, "agent_id": agent_id, "harness": spec_in["harness"],
        "model": spec_in["model"], "effort": spec_in["effort"],
        "repo": repo, "commit": meta["sha"], "base": meta["base"],
        "rundir": os.path.join(root, agent_id),
        "workdir": os.path.join(root, agent_id, "wt"),
        "outfile": outfile, "ingress": ingress, "deadline_s": deadline,
        "turns": [
            # Turn 1: the review itself. The worktree is soft-reset to the
            # parent, so the commit under review IS the pending change.
            {"text": harness.review_command(level), "expect_file": None},
            # Turn 2: make the result durable. Splitting this out is what makes
            # the artifact -- not the screen -- the completion authority.
            {"text": SAVE_TURN.format(outfile=outfile),
             "expect_file": outfile,
             "nudge": SAVE_NUDGE.format(outfile=outfile)},
        ],
    }


def _combiner_spec(run_id, combiner, repo, meta, root, a, b, ingress, deadline,
                   inherit_from=None) -> dict:
    """Build the combiner's spec.

    `inherit_from` is a reviewer result whose workspace checkpoint the combiner
    should start from -- filesystem state handed from one node to the next. We
    check out that agent's frozen commit and soft-reset to the *original*
    parent, so the combiner sees the reviewed change plus whatever that agent
    left behind, all as pending work. Falls back to the raw commit when the
    agent changed nothing (checkpoint is None).
    """
    agent_id = combiner["agent_id"]
    outfile = os.path.join(root, "combined.md")
    start_commit = meta["sha"]
    inherited = None
    if inherit_from and inherit_from.get("checkpoint"):
        start_commit = inherit_from["checkpoint"]
        inherited = f"{inherit_from['agent_id']}@{start_commit[:12]}"
    text = COMBINE_TURN.format(
        sha=meta["sha"][:12], subject=meta["subject"],
        a_model=a["model"], a_file=a["outfile"],
        b_model=b["model"], b_file=b["outfile"], outfile=outfile,
    )
    return {
        "run_id": run_id, "agent_id": agent_id, "harness": combiner["harness"],
        "model": combiner["model"], "effort": combiner["effort"],
        "repo": repo, "commit": start_commit, "base": meta["base"],
        "inherited_workspace": inherited,
        "rundir": os.path.join(root, agent_id),
        "workdir": os.path.join(root, agent_id, "wt"),
        "outfile": outfile, "ingress": ingress, "deadline_s": deadline,
        "turns": [{"text": text, "expect_file": outfile,
                   "nudge": COMBINE_NUDGE.format(outfile=outfile)}],
    }


def _assemble(root: str, meta: dict, results: list, combined: dict) -> dict:
    """Prepend provenance to the combiner's markdown."""
    body = ""
    if os.path.isfile(combined["outfile"]):
        body = open(combined["outfile"], encoding="utf-8", errors="replace").read()

    lines = [
        f"# Code review: {meta['sha'][:12]} — {meta['subject']}",
        "",
        f"- **Commit** `{meta['sha']}`",
        f"- **Author** {meta['author']}  ·  **Date** {meta['date']}",
        f"- **Files changed** {len(meta['files'])}",
        f"- **Diff base** `{meta['base'][:12]}`",
        "",
        "Produced by independent reviews, merged by a third agent. Each agent ran as a real "
        "interactive session in its own tmux pane and its own git worktree, orchestrated by "
        "a Restate durable workflow.",
        "",
        "| Agent | Model | Effort | Review chars | Nudges | Respawns | Wall time | Workspace checkpoint |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in results + [combined]:
        lines.append(
            f"| {r['agent_id']} | {r['model']} | {r['effort']} | {r['chars']} | "
            f"{r['nudges']} | {r.get('respawns', 0)} | {r['elapsed_s']}s | "
            f"{(r.get('checkpoint') or '-')[:12]} |"
        )
    lines += ["", "---", "", body]

    path = os.path.join(root, "report.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return {"path": path, "chars": len("\n".join(lines))}
