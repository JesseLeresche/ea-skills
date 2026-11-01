# Plan 003: promote refuses a promotion that leaves a duplicate id in approved

**What:** The promotion gate reports an error when a selected staging file holds an id that an approved file at a different path also holds.
**Why:** Today that promotion passes, then `validate` fails with ID001 on the approved zone. The gate should catch what the move will cause.
**Risk:** significant (changes what `promote` accepts; no change to `validate`, `compile` or staging reads)
**Size:** estimated 2 source files (`dsl.py`, `validate.py`), 1 doc, about 12 new tests, no new commands or rule ids
**Touches:** this fork only. `promote` dry run and real run get stricter; golden outputs unchanged.
**Recommendation:** approve with the defaults below

## Needs your decision

1. Where does the check live? **Default:** the promotion gate (`validate_promotion`), fed by a record that `dsl.load_merged` keeps. Not in `load_merged` itself: staging validation deliberately treats a same-id staging concept as an update proposal (docs/RULES.md, "Zone semantics"), and `elements set` relies on that.
2. Rule id? **Default:** reuse `ID001`, error severity, with a longer message than `validate` prints. After the move `validate` reports exactly ID001, so the gate says the same thing earlier. No new id, so RULES.md only gets a note.
3. Warn earlier, in `validate --zone staging`? **Default:** no. It would change staging output that agents read today. I can raise it as a separate task if you want it.

---

<details>
<summary>Approach</summary>

Reproduced on a copy of `eval/example`: `model/staging/dup.yaml` holding `actor-b2b-customer` (also in `model/approved/business.yaml`) gives `0 error(s) -- PASS`, `Would move model/staging/dup.yaml -> model/approved/dup.yaml`. After a real move, `validate` fails with ID001.

Cause: `dsl.load_merged` removes only approved files that staging *shadows by path*. For every other approved file it lets the staging concept replace the approved one by id, in memory. That is right for staging reads, wrong for a move that leaves both files in `approved/`.

Fix: while merging, `load_merged` records each id that a staging document redefines from an approved file that is **not** shadowed: `(id, approved_path, staging_path)` on a new `Model.cross_file_replacements` list. Nothing that reads the model today looks at it, so staging reads, `compile`, `render` and `validate --zone staging` behave as before. `validate_promotion` turns each record into an ID001 error against the staging file. All five buckets (elements, relationships, views, stakeholders, concerns) go through one code path.

`promote` needs no change: a failing gate already moves nothing, in dry run and real. Partial promotion is covered, because the merge only sees the selected staging files.

Message (shape):
`duplicate identifier 'X': model/staging/a.yaml would leave it defined twice in approved/, also in model/approved/b.yaml. Fix one of: (1) put the replacement at the same relative path as the approved file (model/staging/b.yaml, which replaces that file whole, so carry its other content over; for an element, `elements set` does this), or (2) remove the old definition from the approved file by staging a copy of it without X.`

</details>

<details>
<summary>Files and interfaces</summary>

- `easkills/dsl.py`: `Model.cross_file_replacements: list[tuple[str, Path, Path]]`, filled in `load_merged`.
- `easkills/validate.py`: `validate_promotion` adds the ID001 findings. `validate`, `check_identifiers` and the finding text for ordinary duplicates are untouched.
- `docs/RULES.md`: one paragraph under ID001 / "Zone semantics". `CHANGELOG.md` entry (appended).
- `modelwrite.Workspace.overlay` copies `load_merged`'s merge. It does not use the new list, so it needs no change.
- No CLI flag, exit code or JSON schema change. Exit stays 1 when the gate blocks.

</details>

<details>
<summary>Tests</summary>

Written first and shown red, then green. Full-suite counts before (1500 passed) and after go in the PR.

- Element at a new staging path with an approved id: gate fails, ID001 names the id, the staging file and the approved file, and both fixes. Dry run and real run both exit 1; the real run moves nothing.
- The same for a relationship, a view, a stakeholder and a concern.
- Same id at the same relative path (a real replacement): still passes, as today.
- Staging file that replaces an approved file and drops the id: still passes.
- Partial promotion: a colliding file that is not selected does not block; a selected one does.
- A staging file with a fresh id: passes.
- The two fixes from the message each turn the gate green (copy to the approved path; stage the approved file without the element).
- `validate --zone staging` output on a colliding copy is byte-identical to before.
- Existing tests, including the golden and contract tests, pass unchanged.

</details>

<details>
<summary>Risks and what this will not do</summary>

- A promotion that passed before can now be blocked. It was already producing an invalid approved zone, so this is the intent.
- Will not change staging validation, `load_zone`, `compile` or `render`.
- Will not add a command that performs fix 2 for relationships; the message describes it.
- Will not touch `.github/workflows/` or upstream.
- Conflict surface: `dsl.load_merged` and `validate_promotion` only; no open PR touches them.

</details>

<details>
<summary>Acceptance criteria</summary>

- [ ] The reproduction (`dup.yaml` on a copy of `eval/example`) makes `promote --dry-run` and `promote` exit 1 with the id, both files and both fixes in the output, and moves nothing.
- [ ] Same-path replacement and fresh-id promotions behave exactly as before.
- [ ] `validate --zone staging` output is unchanged.
- [ ] Suite: 1500 passed before; after = 1500 + new tests, 0 failed.
- [ ] Golden outputs unchanged.

</details>
