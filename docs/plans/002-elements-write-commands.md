# Plan 002: write commands for elements and relationships (staging only)

**What:** Add `elements new`, `elements set`, `elements retire` and `relations new`. They write only into `model/staging/`, so `promote` stays the one way into `approved/`.
**Why:** Agents can now read the model (plan 001) but still hand-write YAML to change it, which skips the relationship matrix and the provenance check until later.
**Risk:** significant (new write commands; staging file layout interacts with `promote`)
**Size:** 2 new files (`easkills/modelwrite.py`, `tests/test_modelwrite.py`), about 150 new tests, 4 commands
**Touches:** this fork only. Writes only under `model/staging/`; no rule, schema, `promote` or golden-output change.
**Recommendation:** approve with the defaults below

## Needs your decision

1. Where do new concepts go? **Default:** one file per concept, `model/staging/<id>.yaml`. A per-layer file looks tidier but is unsafe: promoting a staging file replaces the approved file of the same name, silently dropping everything else in it.
2. What does `retire` mean? **Default:** set `properties.lifecycle: retired`. The element and its relationships stay; deleting is not offered. The reason goes in a decision record (`decisions new --element ID`).
3. Should `new` stamp `owner` and `lastReviewed`? **Default:** no. `--owner` and `--last-reviewed` are optional. Staging only warns (GOV001/GOV002), and `promote` blocks until a human supplies them, as `promote.py` intends.
4. `elements set` on an element that is only in `approved/`: **Default:** copy that whole approved file into staging at the same relative path, then edit the one element. The path match is what makes staging replace it. Output names the file, and `promote` already flags a replacement.
5. Finding to act on separately: a staging element with an approved id, in a file at a different path, passes `promote` and then fails `validate` with ID001 (reproduced on `eval/example`). **Default:** I raise it as its own task; this plan avoids it.

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

- `elements new --type T --name N [--id ID] [--documentation D] [--property k=v]... [--owner O] [--last-reviewed YYYY-MM-DD]` plus evidence, below.
- `elements set ID FIELD VALUE`. Fields: `name`, `documentation`, `owner`, `lastReviewed`, `properties.KEY`. Not `id`, `type`, provenance or lists: changing a type changes which relationships are legal, so use retire plus new.
- `elements retire ID`. Already retired is a no-op, exit 0.
- `relations new --type T --from A --to B [--id ID] [--access-type X] [--directed true|false] [--modifier M]` plus evidence.

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
- Delete concepts, change `type`, `id`, provenance or list fields, or add `relations set` or `relations retire`.
- Stamp `owner` or `lastReviewed`, or invent a rationale.
- Resolve id prefixes or add a lock, cache or index.
- Touch upstream.

</details>

<details>
<summary>Acceptance criteria</summary>

- [ ] A new element and a new relationship reach `approved/` through `new`, `validate`, `promote` without hand-written YAML.
- [ ] An illegal relationship and a fabricated quote are each refused with nothing written.
- [ ] `set` and `retire` change one element and leave comments untouched.
- [ ] `approved/` is unchanged by every command; golden outputs and existing tests are unchanged.
- [ ] The PR carries real transcripts and full-suite counts before and after.

</details>
