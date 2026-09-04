# Code Review — r6 (opus)

**Effort level:** low (1 diff pass, no verify, max 4 findings)
**Scope:** `git diff main...HEAD` + `git diff HEAD`, non-test files only
**Files reviewed:**
- `src/paude/backends/podman/backend.py` (modified)
- `src/paude/backends/podman/resources.py` (new)

Test/fixture changes (`tests/fakes.py`, `tests/test_session_resources.py`, `tests/test_upgrade.py`) were excluded per the low-effort review scope.

## Findings

**None.**

No runtime-correctness bugs, duplicated helpers, or dead code were found in the reviewed hunks.

## Notes on what was checked

The diff is a refactor that extracts session resource teardown out of `PodmanBackend` into a new `SessionResources` collaborator:

- `_rollback_session_resources` → `SessionResources.rollback_create`
- `_cleanup_session_resources` → `SessionResources.cleanup_all`
- new `teardown_for_rebuild` for the upgrade/backup path

Specifically verified:

- **Behavior preservation.** `rollback_create` and `cleanup_all` reproduce the removed methods' steps in the same order, including the `config.proxy_image` guard, the `volume_reused` guard, the `volume_exists` checks on proxy volumes, and the trailing `remove_secret(GCP_ADC_SECRET_NAME)`.
- **Shared helper extraction.** `_remove_proxy_and_network` factors out the proxy-container + network removal common to `rollback_create` and `teardown_for_rebuild`; both prior call sites used the same force/tolerant semantics, so the collapse is safe.
- **No destructive-default regression.** `teardown_for_rebuild` deliberately does *not* touch the workspace volume, the proxy auth volume, or the credential secrets — the state a rebuild must preserve. The three teardowns stay as separate named verbs rather than one flag-driven path.
- **Imports and dead code.** `network_name` and `GCP_ADC_SECRET_NAME` were dropped from `backend.py` along with their only remaining uses; no stale references remain. `auth_volume_name` / `ca_volume_name` moved from a function-local import to a module-level import from `helpers.py`, where both are defined.
- **Runner methods exist.** `container_running` (`src/paude/container/runner.py:368`) and `get_container_image` (`src/paude/container/runner.py:434`) back the new read accessors.
