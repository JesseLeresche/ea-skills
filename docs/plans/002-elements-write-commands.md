# Plan 002: write commands for elements and relationships (staging only)

**What:** Add `elements new`, `set`, `retire`, `remove` and `relations new`, `remove`. They write only into `model/staging/`, so `promote` stays the one way into `approved/`.
**Why:** Agents can now read the model (plan 001) but still hand-write YAML to change it, which skips the relationship matrix and the provenance check until later.
**Risk:** significant (new write commands; staging file layout interacts with `promote`)
**Size:** 2 new files (`easkills/modelwrite.py`, `tests/test_modelwrite.py`), about 190 new tests, 6 commands
**Touches:** this fork only. Writes only under `model/staging/`; no rule, schema, `promote` or golden-output change.
**Recommendation:** approve with the defaults below

## Needs your decision

1. ✅ Approved. Where do new concepts go? **Default:** one file per concept, `model/staging/<id>.yaml`. A per-layer file looks tidier but is unsafe: promoting a staging file replaces the approved file of the same name, silently dropping everything else in it.
2. Soft and hard delete (revised after review). **Default:** `retire` is the soft delete: set `properties.lifecycle: retired`; element and relationships stay; reason goes in a decision record (`decisions new --element ID`). New `remove` is the hard delete: it takes the element (or, for `relations remove`, the relationship) out of staging. For an approved element it stages a copy of the approved file without it, so nothing leaves `approved/` until a human runs `promote`, which already lists dropped content. `remove` refuses while relationships still point at the element (remove those first) and needs `--confirm ID`. **Open:** is that the hard delete you meant, and do you want `relations remove` too?
3. Stamp `owner`, `lastReviewed`, and a creator (revised after review). **Default:** `owner` and `lastReviewed` are still not stamped: `--owner` and `--last-reviewed` are optional, staging only warns (GOV001/GOV002), and `promote` blocks until a human supplies them, as `promote.py` intends. New: `new` records who created the concept as `properties.createdBy`, from `--created-by NAME`, else `git config user.name`. It is written only when absent and `set` never changes it. No schema change is needed (`properties` is free-form). **Open:** an agent's git name is the bot, which is no use for "who to contact". Default: refuse when there is no `--created-by` and the git name is a bot; the agent passes the requesting person. Say if you'd rather allow the bot name.
4. ✅ Default. `elements set` on an element that is only in `approved/`: **Default:** copy that whole approved file into staging at the same relative path, then edit the one element. The path match is what makes staging replace it. Output names the file, and `promote` already flags a replacement.
5. ✅ Approved. Finding to act on separately: a staging element with an approved id, in a file at a different path, passes `promote` and then fails `validate` with ID001 (reproduced on `eval/example`). **Default:** I raise it as its own task; this plan avoids it.

---

<details>
<summary>Approach</summary>

Each command builds the would-be staging document in memory, appends it to the staging overlay (`dsl.load_merged`), runs the existing validator on it, and compares findings with the same overlay before the change. Any **new error** means exit 1 and nothing is written. Pre-existing problems never block. Warnings are printed; an approximate provenance quote (PROV004) on the new concept is refused too. Nothing duplicates a rule, so the checks cannot drift from `validate`.

Writes reuse the record core: `records.write_new` (no clobber, temp file then link) for new files and `records.replace_file` (atomic swap) for edits. `set` and `retire` edit one element's block surgically: comments and wording stay, and anything not plainly simple YAML is refused (`EditRefused`) after a re-parse check, as the core already does for decisions.

Conflicts: new concepts get their own file, so two agents cannot clash. `set` re-reads the file just before swapping it and refuses with "changed on disk, re-run" if its bytes moved. That is best effort, not a lock.

Module placement: a new `easkills/modelwrite.py`, registered into the same `elements` and `relations` groups. This departs from the note in plan 001 to extend `modelquery.py`. Keeping the read module free of write code keeps its read-only test meaningful.

</details>

<details>
<summary>Files and interfaces</summary>

All commands take `--root`, `--dry-run` (show the file and findings, write nothing) and a boolean `--json` (one document to stdout).

- `elements new --type T --name N [--id ID] [--documentation D] [--property k=v]... [--owner O] [--last-reviewed YYYY-MM-DD] [--created-by NAME]` plus evidence, below.
- `elements set ID FIELD VALUE`. Fields: `name`, `documentation`, `owner`, `lastReviewed`, `properties.KEY`. Not `id`, `type`, provenance or lists: changing a type changes which relationships are legal, so use retire plus new.
- `elements retire ID`. Already retired is a no-op, exit 0.
- `elements remove ID --confirm ID` and `relations remove ID --confirm ID`: hard delete, staging only (see decision 2). Refused if the id is unknown, if relationships reference the element, or if `--confirm` does not match.
- `relations new --type T --from A --to B [--id ID] [--access-type X] [--directed true|false] [--modifier M] [--created-by NAME]` plus evidence. `createdBy` goes in the relationship's `properties` if REL004 allows it; otherwise it is omitted and the output says so.

Evidence (new commands): either `--source FILE --quote TEXT` (repeatable pairs) or `--fact ID`, or `--assumed --rationale TEXT`. Neither, or `--assumed` without a rationale, is refused. A quote must exist in the named file: the existing PROV001-PROV008 checks decide, not new code.

Ids: default is the slug of the name for elements, `rel-<from>-<type>-<to>` (lowercase) for relationships. A collision in either zone is refused with suggestions. Never a numbered suffix.

Relationships: checked at creation against the vendored ArchiMate matrix with the existing REL001 text, including the legal alternatives and the swapped-direction hint. The optional attributes follow REL004.

Exit codes: 0 written (or dry run clean); 1 refused, nothing written (unknown id, validation, conflict, unsafe edit); 2 usage error.

Docs: `docs/CLI.md`, `README.md`, `docs/GETTING-STARTED.md`, `template/README.md` (the repo-docs test requires them), `CHANGELOG.md`.

</details>

<details>
<summary>Tests</summary>

Written first and shown red. Cases that matter:

- `new`: each element type and layer; id derivation and collisions in both zones; evidence matrix (quote, fact, assumed, none, assumed without rationale); fabricated quote refused (PROV003); missing or out-of-repo source refused; approximate quote refused.
- `relations new`: legal, illegal with alternatives, swapped direction hint, unknown endpoint, endpoint only in staging, attribute on the wrong type (REL004).
- `remove`: staging-only element taken out (file removed when empty); approved element staged as a copy without it, `approved/` untouched, `promote` lists it as dropped; refused with relationships pointing at it, with a wrong `--confirm`, with an unknown id; `relations remove` likewise.
- `createdBy`: from flag; from git name; bot name without flag refused; never overwritten by `set` (`properties.createdBy` refused as a field).
- `set` and `retire`: each field; comments and line endings kept; approved-only element copied to the same path; element already in staging edited in place; unsafe YAML refused; concurrent change refused.
- Never writes to `approved/` (whole-tree hash before and after); `--dry-run` writes nothing; a refusal leaves no file or directory.
- Round trip: `new` then `validate` then `promote` on a copy of `eval/example`, plus the same for a `set` on an approved element.
- `eval/golden` byte-identical; the existing 1195 tests stay green.

</details>

<details>
<summary>Risks and what this will not do</summary>

Risks:

- A copied approved file goes stale if the approved file changes before the copy is promoted; promote would then overwrite the newer content. `promote` already lists replaced files, and the command output names the copy. Detecting it automatically would mean changing `promote`, which is out of scope.
- One file per concept means many small files in `approved/` after promotion. A later tidy command could merge them; not part of this plan.
- The edit helper is new code on top of the core's primitives, so it carries the most risk. The re-parse check and the refusal rule limit that.
- Conflict surface: `cli.py` (two handler lines), `modelquery.py` (one call to register the new leaves), docs and changelog.

Will not:

- Write to `approved/`, or touch `promote`, validator rules, schemas or `.github/workflows`.
- Remove from `approved/` directly (a removal is staged and takes effect only on `promote`), change `type`, `id`, provenance or list fields, or add `relations set` or `relations retire`.
- Stamp `owner` or `lastReviewed`, or invent a rationale.
- Resolve id prefixes or add a lock, cache or index.
- Touch upstream.

</details>

<details>
<summary>Acceptance criteria</summary>

- [ ] A new element and a new relationship reach `approved/` through `new`, `validate`, `promote` without hand-written YAML.
- [ ] An illegal relationship and a fabricated quote are each refused with nothing written.
- [ ] `set` and `retire` change one element and leave comments untouched; `remove` of an approved element changes nothing in `approved/` until `promote`.
- [ ] Every new concept carries `properties.createdBy`.
- [ ] `approved/` is unchanged by every command; golden outputs and existing tests are unchanged.
- [ ] The PR carries real transcripts and full-suite counts before and after.

</details>
