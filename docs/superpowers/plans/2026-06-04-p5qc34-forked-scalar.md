# p5qc.3.4 — wire forked-scalar into deploy — Implementation Plan

**Goal:** Upgrade the `preserve_user_keys` overlay from blind live-wins to a
stored-base 3-way **scalar** merge `{scalar-base, live, tracked}`, so upstream
(tracked) changes to a key the user has NOT locally changed now propagate, while
the user's own edits are preserved; genuine 3-way conflicts honor `--auto` else
keep-live + warn.

**Opt-in by base presence:** base-absent (first run / unseeded) ⇒ today's exact
blind overlay behavior, then seed the scalar base. So existing
`preserve_user_keys` configs behave identically on first install; the 3-way
behavior begins on the second install once a base exists. Existing
preserve_user_keys e2e/unit tests must stay green.

**Scope:** SHALLOW `preserve_user_keys` paths whose leaf is a SCALAR. Non-scalar
preserve_user_keys leaves (whole list/dict replace) and `preserve_user_keys_deep`
keep their current overlay behavior this wave (the scalar primitives only address
scalar leaves). This is the `disposition is None` legacy-preserve path — it is a
DIFFERENT file set than p5qc.7's disposition files (mutual-exclusion guarantees
no overlap).

Spec: `~/.claude/plans/eager-bouncing-sunrise.md` (p5qc.3.4 section + pitfalls —
esp. ABSENT-with-`is`, batched `set_bases` not per-path loop, present:false vs
value:null, json-five derived key_value_pairs).

## Reused primitives (already on main)
- `scalar_merge.resolve_scalar(base, ours, theirs) -> ScalarResolution{outcome: TAKE/DELETE/CONFLICT, value}`; `ABSENT` (compare with `is`).
- `scalar_path.read_scalar_{yaml,jsonc}(doc, path) -> value|None|ABSENT`; `write_scalar_{yaml,jsonc}(doc, path, ScalarResolution)`; rejects list-suffix; `MergeTypeMismatch` on non-scalar leaf.
- `scalar_base_store.get_base(profile, file_id, path) -> typed|None|ABSENT`; `set_bases(profile, file_id, {path: value})` (BATCH — primary); `prune(profile, file_id, live_paths)`.
- `base_store`/install-loop byte-base lifecycle from p5qc.7 — mirror its read-before / write-after-live / loud-failure / prune ordering for the scalar store.

## Tasks
1. **Scalar overlay 3-way driver** (`setforge/scalar_overlay.py` NEW): a pure-ish
   function `resolve_scalar_overlay(suffix/dst, live_text, tracked_text,
   preserve_user_keys, base_lookup, auto) -> ScalarOverlayResult{merged_text,
   rebaseline: dict[path,value-or-ABSENT], conflicts: list[path], deferred: bool}`.
   For each preserve_user_keys path: read ours=live, theirs=tracked via
   `read_scalar_*`; base via `base_lookup(path)` (returns typed|None|ABSENT).
   - base is ABSENT (no stored base for this path) ⇒ FALLBACK to today's blind
     live-wins for that path (write ours into the merged doc), and mark it for
     seeding (rebaseline[path]=ours). This preserves first-run behavior.
   - base present ⇒ `resolve_scalar(base, ours, theirs)`:
     - TAKE/DELETE ⇒ apply via `write_scalar_*`; rebaseline[path]=resolved value
       (or ABSENT for DELETE) ONLY when resolved (always, for auto-take).
     - CONFLICT ⇒ honor `auto` (use-tracked→theirs / keep-live→ours) and
       rebaseline; `auto is None` ⇒ keep-live (ours) but DO NOT rebaseline
       (defer) and record the conflict path + deferred=True.
   - Non-scalar leaf (`MergeTypeMismatch` from read/write) ⇒ fall back to the
     existing blind overlay for that path (do not 3-way it); document.
   - ABSENT compared with `is`. Start from the live doc (parse once), apply
     resolutions, dump byte-faithfully (ruamel preserve_quotes / json-five).
   Tests: `tests/test_scalar_overlay.py` — auto-take both directions, conflict
   +auto, conflict bare (defer), base-absent fallback+seed, present:false vs
   null distinct, non-scalar fallback, YAML+JSONC.
2. **Wire into deploy** (`deploy._render_with_preserve_keys` / `copy_atomic`):
   thread `scalar_bases: dict[str, object] | None` (path→base value/ABSENT) and
   `merge_auto`; when present and preserve_user_keys non-empty, route through the
   driver instead of `jsonc.overlay_user_keys`/`yaml_merge.overlay`. Return
   `new_scalar_bases: dict[str,object] | None` + scalar conflicts on
   `DeployResult`. base-absent for ALL paths ⇒ behaves as today + seeds. Keep the
   no-preserve and deep-only paths unchanged. Tests: `tests/test_deploy_scalar.py`.
3. **Install-loop scalar-base lifecycle** (`cli/_install_helpers.py`): for each
   regular-file tracked_file with non-empty preserve_user_keys (and disposition
   is None), read scalar bases (one `get_base` per path or a bulk read) → pass to
   copy_atomic → after a successful live-write, batched
   `scalar_base_store.set_bases(profile, file_id, new_scalar_bases)` (NEVER a
   per-path loop); `scalar_base_store.prune(profile, file_id, live_paths)`; warn
   on deferred scalar conflicts. file_id = same sub_name convention as the byte
   base. Live-write FIRST then base-write (loud). Tests:
   `tests/test_install_scalar.py` (drive install CLI; seed/advance/defer/prune).
4. **e2e + regression** (`tests/docker/test_e2e_docker_forked_scalar.py`): install
   seeds scalar base; second install propagates an upstream scalar change to an
   unedited key; a user-edited key is preserved; same-key conflict keep-live+warn;
   `--auto=use-tracked` takes tracked. Confirm existing
   `test_e2e_docker_preserve_user_keys_overlay.py` + unit preserve tests stay green
   (base-absent = old behavior on first install).

## Verification
```sh
uv run pytest tests/ -q --no-cov
pre-commit run --all-files
uv run pytest tests/docker/test_e2e_docker_forked_scalar.py tests/docker/test_e2e_docker_preserve_user_keys_overlay.py -m e2e_docker --no-cov
```
