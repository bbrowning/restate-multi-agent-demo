"""Tests for the pure pieces: pane classification and tmux primitives.

No Restate and no LLM calls, so this runs in seconds. Everything here encodes
something observed on a live TUI -- see the comments for what and why.

    uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import time

import pytest

from reviewdemo import tmux
from reviewdemo.harness import classify, get, prompt_empty

CLAUDE = get("claude")

# Captured verbatim from Claude Code 2.1.260. Note U+00A0 after the chevron:
# matching on a plain ASCII space silently never fires.
IDLE_PANE = (
    "● Files changed in the last commit:\n"
    "✻ Worked for 3s · done 7:25 PM\n"
    "────────────────────\n"
    "❯\xa0\n"
    "────────────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
)
BUSY_PANE = IDLE_PANE.replace(
    "bypass permissions on (shift+tab to cycle) · ←",
    "bypass permissions on (shift+tab to cycle) · esc to interrupt · ←",
)
# After a turn Claude pre-fills the box with a *suggested* follow-up. It is a
# real editable draft, so a bare Enter would submit Claude's idea, not ours.
SUGGESTION_PANE = IDLE_PANE.replace("❯\xa0\n", "❯\xa0show the diff of that commit\n")


class TestClassify:
    def test_idle_pane_is_ready(self):
        assert classify(CLAUDE, IDLE_PANE) == "ready"

    def test_busy_indicator_detected(self):
        assert classify(CLAUDE, BUSY_PANE) == "busy"

    def test_busy_wins_over_ready(self):
        # A busy pane still shows the prompt box; the busy test must win, or we
        # would treat a working agent as finished.
        assert "❯" in BUSY_PANE
        assert classify(CLAUDE, BUSY_PANE) == "busy"

    def test_unknown_before_tui_paints(self):
        assert classify(CLAUDE, "loading...") == "unknown"


class TestPromptEmpty:
    def test_empty_box(self):
        assert prompt_empty(CLAUDE, IDLE_PANE) is True

    def test_box_with_text(self):
        assert prompt_empty(CLAUDE, SUGGESTION_PANE) is False

    def test_last_prompt_wins(self):
        # Earlier prompts scroll up into the transcript; only the live input
        # box matters.
        pane = "❯\xa0an old prompt\n  output\n" + IDLE_PANE
        assert prompt_empty(CLAUDE, pane) is True

    def test_unknown_when_no_prompt(self):
        assert prompt_empty(CLAUDE, "no prompt here") is None


class TestHarnessSeam:
    def test_registry(self):
        assert get("claude").name == "claude"
        assert get("codex").name == "codex"
        with pytest.raises(KeyError):
            get("nope")

    def test_claude_launch_flags(self):
        argv = CLAUDE.launch_argv(
            model="opus", effort="low", rundir="/r", session_uuid="u", ingress="http://x"
        )
        assert argv[0] == "claude"
        for flag, value in (("--model", "opus"), ("--effort", "low"),
                            ("--permission-mode", "bypassPermissions"),
                            ("--session-id", "u")):
            assert value == argv[argv.index(flag) + 1]

    def test_stop_hook_is_wired(self):
        hooks = CLAUDE.settings(rundir="/r", ingress="http://x")["hooks"]["Stop"]
        hook = hooks[0]["hooks"][0]
        assert hook["command"].endswith("on_stop.sh")
        assert hook["args"] == ["/r", "http://x"]

    def test_strategies_differ(self):
        # The seam exists precisely because harnesses differ here.
        assert get("claude").done_strategy == "hook"
        assert get("codex").done_strategy == "quiescence"


class TestTmux:
    """Real tmux, but against a plain shell -- no agent, no tokens."""

    session = "rd-pytest"

    def setup_method(self):
        tmux.kill_session(self.session)

    def teardown_method(self):
        tmux.kill_session(self.session)

    def test_spawn_send_capture_roundtrip(self):
        tmux.new_session(self.session, "/tmp", ["bash", "--norc", "-i"],
                         env={"RD_T": "marker"})
        assert tmux.has_session(self.session)
        time.sleep(0.8)
        tmux.wake_pane(self.session)
        tmux.send_literal(self.session, "echo VAL-$RD_T-$((6*7))")
        tmux.send_keys(self.session, "Enter")
        for _ in range(20):
            time.sleep(0.3)
            if "VAL-marker-42" in tmux.capture(self.session, 40):
                break
        assert "VAL-marker-42" in tmux.capture(self.session, 40)

    def test_new_session_is_idempotent(self):
        # Restate replays journalled steps; a replayed spawn must not start a
        # second agent.
        tmux.new_session(self.session, "/tmp", ["bash", "--norc", "-i"])
        tmux.new_session(self.session, "/tmp", ["bash", "--norc", "-i"])
        assert len([s for s in tmux.list_sessions("rd-pytest")]) == 1

    def test_large_literal_uses_paste_buffer(self):
        tmux.new_session(self.session, "/tmp", ["bash", "--norc", "-i"])
        time.sleep(0.5)
        tmux.send_literal(self.session, "y" * (tmux.MAX_LITERAL + 500))
        tmux.send_keys(self.session, "C-u")

    def test_pane_dead_when_absent(self):
        assert tmux.pane_dead("rd-definitely-not-here") is True
