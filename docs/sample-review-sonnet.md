# Code Review — `/code-review low`

**Scope:** Working-tree diff (staged + unstaged) at time of review:
- `src/paude/backends/labels.py`
- `src/paude/backends/podman/helpers.py`
- `src/paude/backends/podman/session_setup.py`
- `tests/test_labels.py` (new)
- `tests/test_upgrade.py`

## Summary

No bugs found. The diff is a straight move/consolidation of label-parsing
helpers (`_parse_agent_providers`, `_parse_providers`,
`_encode_json_label`/`_decode_json_label`, `encode_agent_providers`,
`encode_providers`) from `podman/helpers.py` into the new
`src/paude/backends/labels.py`, plus new `SessionSpec`/`LabeledSession`/
`spec_from_labels`/`read_labels` wrapper code. Logic is unchanged from the
original implementations (verified line-by-line against the prior
`helpers.py` hunks), call sites are updated consistently, and the
`tests/test_upgrade.py` import fix matches the new module location.

No inverted conditions, off-by-ones, missing awaits, or dropped guards were
found in the non-test hunks.

## Findings

### 1. `src/paude/backends/labels.py:83-97` — broad exception swallowing in `decode_json_label`
- **Severity:** Informational (not a new bug)
- **Rationale:** `decode_json_label` catches `binascii.Error, UnicodeError, json.JSONDecodeError, ValueError` on the base64 decode path and falls back to parsing the value as raw JSON. This is pre-existing behavior carried over unchanged from the original `_decode_json_label` in `podman/helpers.py`, not a regression introduced by this diff. Flagged only for visibility since broad exception catching can mask genuinely malformed labels.

### 2. `src/paude/backends/labels.py:187-198` — local (non-top-level) import inside `workspace_from_labels`
- **Severity:** Informational
- **Rationale:** `workspace_from_labels` does `from paude.backends.session_env import decode_path` inside the function body rather than at module scope. It's the only non-top-level import introduced in this diff. Not a functional bug, but worth noting as a likely circular-import workaround — if intentional, no action needed; if incidental, consider restructuring imports to avoid the cycle.

## Conclusion

No correctness issues identified at `low` review effort. The two items above
are informational/pre-existing and do not block the change.
