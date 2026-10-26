# Plan 001: read commands for elements and relationships

**What:** Add `elements list`, `elements show` and `relations list` so agents find model content without reading YAML files.
**Why:** Today finding an element means reading the model files; only `impact` and `context` help, and only if you already know the id.
**Risk:** significant (new commands, one touch to shared code)
**Size:** 2 new files, about 100 new tests, 3 commands; error text of `impact` and `context` gains a hint
**Touches:** this fork only. Read-only: writes nothing, changes no rule, schema or golden output.
**Recommendation:** approve

## Needs your decision

1. Add a "did you mean ..." hint to the unknown-id errors of `impact` and `context`? **Default:** yes (error path only; existing wording kept).
2. Add three small public aliases in `records.py` so the new module can reuse its JSON and CLI plumbing? **Default:** yes (no behaviour change).

---

<details>
<summary>Approach</summary>

Elements and relationships live many-per-file in the DSL model, unlike decisions (one file per record), so they are not forced into the record core. A new module `easkills/modelquery.py` reads through `dsl.load_zone`, the same loader (with the staging overlay) that `validate`, `compile` and `impact --zone staging` use, so the commands cannot disagree with the validator. From `records.py` it reuses only the JSON printer, the failure shape and the `--root`/`--json` leaf parser. One load plus linear filtering per command; no caching or index files.

</details>

<details>
<summary>Files and interfaces</summary>

All commands take `--root` and a boolean `--json` (one document to stdout).

- `elements list [--zone approved|staging] [--layer L]... [--type T]... [--property k=v]... [--text Q] [--limit N]`
- `elements show ID`: full element, relationships in and out, decisions that name it, views that include it
- `relations list [--zone approved|staging] [--type T]... [--from ID]... [--to ID]... [--limit N]`

Filters: a repeated flag means any-of; different flags AND together; `--property` matches the exact value; `--layer` is case-insensitive; `--text` is a case-insensitive substring over id, name and documentation (not provenance quotes).

Rows: `elements list` gives id, type, layer, name, zone; `relations list` gives id, type, source, target, zone, plus `accessType`, `isDirected`, `modifier` when set. Order is deterministic. JSON adds `count`, `total` (before `--limit`), `notes` and `skipped` (unparseable files, so a broken file is never a silent miss).

Exit codes: 0 the query ran (including zero matches); 1 `show` of a missing id or a read failure; 2 usage error. A filter value that matches nothing known yields a note with suggestions, not an error.

Zones: `approved` is the default; `staging` is the same overlay as the validator. A row's `zone` says where it came from. Decisions are not zoned.

Id helper `suggest_ids` (prefix, substring, then difflib at cutoff 0.6, max 5, deterministic) backs `elements show` and the optional hint on `impact`/`context`. It never auto-resolves a prefix: `show` needs an exact id.

Docs updated: `docs/CLI.md`, `README.md`, `CHANGELOG.md` (appended), `docs/SKILL-COVERAGE.md` if it lists commands.

</details>

<details>
<summary>Tests</summary>

Red first: `tests/test_modelquery.py` is committed before the implementation.

- Each filter alone and combined; `--limit` and `total`; deterministic order across runs and file creation order.
- Zone overlay: staging replaces approved, shadowed files, new vs replacing.
- Golden JSON for the example model; notes and `skipped` for a broken file.
- `show`: relationships in and out, decisions, views, a relationship id, a missing id with suggestions, a staging replacement.
- `suggest_ids` unit tests; the `impact` and `context` hints; exit codes.
- Read-only: a test hashes the whole repo tree before and after every command and checks nothing appears.
- Baseline 1002 passing on main; about +100 expected. Golden outputs in `eval/golden` must stay byte-identical.

Performance: the 325-element model from the earlier spike (reproduced by import) loads through `dsl.load_zone` in about 0.16 s; real timings go in the PR.

</details>

<details>
<summary>Risks and what this will not do</summary>

Risks:

- Overlay semantics follow `dsl.load_zone`; an upstream change to it flows through automatically.
- `elements show` prints provenance quotes, so it can be long; `list` stays compact.
- Duplicate ids in a zone follow the loader's first-wins rule; `validate` reports them and a note surfaces it.
- Conflict surface: `cli.py` (one register call and one handler line), `docs/CLI.md`, `README.md`, `CHANGELOG.md`, one line each in `impact.py` and `contextpack.py`, three alias lines in `records.py`. The follow-up write-commands task should extend `modelquery.py` rather than add a parallel module.

Will not:

- Write anything (no new, set or retire; that is the next plan).
- Change validator rules or codes, schemas, or `gen-schema`.
- Touch `decisions` behaviour or `.github/workflows` or upstream.
- Add `relations show`, graph traversal (that is `impact`), or search over provenance.
- Auto-resolve id prefixes, or add any cache or index.

</details>

<details>
<summary>Acceptance criteria</summary>

- [ ] `elements list`, `elements show` and `relations list` work on `eval/example` and on the 325-element model, text and `--json`.
- [ ] Output is deterministic and read-only (repo tree unchanged after every command).
- [ ] Staging overlay matches `validate` and `compile`.
- [ ] Existing tests and golden outputs unchanged; new tests written first and shown red.
- [ ] Docs and changelog updated; PR carries a real transcript and timings.

</details>
