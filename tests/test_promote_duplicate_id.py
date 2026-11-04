"""The promotion gate and `validate --zone staging` refuse an id that staging redefines from an
approved file at a *different* path (plan 003).

Promotion is a rename onto the mirrored path, so only a staging file with the same relative path
replaces an approved file. A staging file at a new path that holds an approved id would leave two
definitions in `approved/` and `validate` would then fail with ID001. The gate says so first.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from easkills import cli, dsl, promote, validate

TODAY = date(2026, 8, 1)

# bucket, id in eval/example, approved file, file-local section name
CASES = [
    ("elements", "app-erp-core", "application.yaml"),
    ("relationships", "rel-baseline-aggregates-wms", "migration.yaml"),
    ("views", "layered-overview", "views.yaml"),
    ("stakeholders", "stakeholder-cio", "stakeholders.yaml"),
    ("concerns", "concern-portfolio-rationalisation", "stakeholders.yaml"),
]


@pytest.fixture()
def repo(tmp_path, example_root) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(example_root, target)
    (target / "model" / "staging").mkdir(exist_ok=True)
    return target


def approved_item(repo: Path, bucket: str, concept_id: str, file_name: str) -> dict:
    data = yaml.safe_load((repo / "model" / "approved" / file_name).read_text(encoding="utf-8"))
    return next(item for item in data[bucket] if item["id"] == concept_id)


def stage_copy(repo: Path, bucket: str, concept_id: str, file_name: str, as_name: str = "dup.yaml") -> Path:
    item = approved_item(repo, bucket, concept_id, file_name)
    path = repo / "model" / "staging" / as_name
    path.write_text(yaml.safe_dump({bucket: [item]}, sort_keys=False), encoding="utf-8")
    return path


def id001(report) -> list:
    return [f for f in report.errors if f.code == "ID001"]


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("bucket, concept_id, file_name", CASES)
def test_the_gate_refuses_an_id_redefined_from_a_differently_named_file(repo, bucket, concept_id, file_name):
    stage_copy(repo, bucket, concept_id, file_name)
    result = promote.promote(repo, dry_run=True, today=TODAY)
    assert not result.ok
    findings = id001(result.report)
    assert [f.concept for f in findings] == [concept_id]
    assert findings[0].file == "model/staging/dup.yaml"
    assert f"model/approved/{file_name}" in findings[0].message


@pytest.mark.parametrize("bucket, concept_id, file_name", CASES)
def test_a_real_promotion_moves_nothing_when_the_gate_refuses(repo, bucket, concept_id, file_name):
    stage_copy(repo, bucket, concept_id, file_name)
    before = tree(repo)
    result = promote.promote(repo, today=TODAY)
    assert not result.ok and not result.moved
    assert tree(repo) == before


def test_the_message_names_the_id_both_files_and_both_fixes(repo):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    message = id001(promote.promote(repo, dry_run=True, today=TODAY).report)[0].message
    assert "app-erp-core" in message
    assert "model/staging/dup.yaml" in message and "model/approved/application.yaml" in message
    assert "same relative path" in message
    assert "without" in message


def test_cli_promote_and_dry_run_exit_1_and_print_the_finding(repo, capsys):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    for extra in (["--dry-run"], []):
        assert cli.main(["promote", "--root", str(repo), *extra]) == 1
        out = capsys.readouterr()
        assert "ID001" in out.out + out.err and "app-erp-core" in out.out + out.err
    assert (repo / "model" / "staging" / "dup.yaml").exists()


@pytest.mark.parametrize("bucket, concept_id, file_name", CASES)
def test_validate_in_staging_reports_it_too(repo, bucket, concept_id, file_name):
    stage_copy(repo, bucket, concept_id, file_name)
    report = validate.validate(repo, zone="staging", today=TODAY)
    assert [f.concept for f in id001(report)] == [concept_id]
    assert not report.ok


def test_cli_validate_staging_exits_1_on_it(repo, capsys):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    assert cli.main(["validate", "--root", str(repo), "--zone", "staging"]) == 1
    assert "ID001" in capsys.readouterr().out


def test_approved_validation_is_not_affected(repo):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    assert validate.validate(repo, zone="approved", today=TODAY).ok


def test_same_relative_path_is_a_real_replacement_and_still_passes(repo):
    approved = (repo / "model" / "approved" / "application.yaml").read_text(encoding="utf-8")
    (repo / "model" / "staging" / "application.yaml").write_text(
        approved.replace("Publishes the order API.", "Publishes the order API (reviewed)."), encoding="utf-8"
    )
    assert not id001(validate.validate(repo, zone="staging", today=TODAY))
    result = promote.promote(repo, today=TODAY)
    assert result.ok and result.moved, result.report.render()


def test_a_fresh_id_is_not_affected(repo):
    item = dict(approved_item(repo, "elements", "app-erp-core", "application.yaml"), id="app-erp-core-two", name="ERP Core Two")
    (repo / "model" / "staging" / "fresh.yaml").write_text(yaml.safe_dump({"elements": [item]}, sort_keys=False), encoding="utf-8")
    assert not id001(validate.validate(repo, zone="staging", today=TODAY))
    assert not id001(promote.promote(repo, dry_run=True, today=TODAY).report)


def test_a_colliding_file_that_is_not_selected_does_not_block_the_selected_one(repo):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    item = dict(approved_item(repo, "elements", "app-erp-core", "application.yaml"), id="app-erp-core-two", name="ERP Core Two")
    good = repo / "model" / "staging" / "fresh.yaml"
    good.write_text(yaml.safe_dump({"elements": [item]}, sort_keys=False), encoding="utf-8")
    assert not id001(promote.promote(repo, files=[good], dry_run=True, today=TODAY).report)
    bad = repo / "model" / "staging" / "dup.yaml"
    assert id001(promote.promote(repo, files=[bad], dry_run=True, today=TODAY).report)


def test_fix_one_put_the_replacement_at_the_approved_path(repo):
    item = approved_item(repo, "elements", "app-erp-core", "application.yaml")
    shutil.copy(repo / "model" / "approved" / "application.yaml", repo / "model" / "staging" / "application.yaml")
    assert not id001(promote.promote(repo, dry_run=True, today=TODAY).report)
    assert item["id"] == "app-erp-core"


def test_fix_two_stage_the_approved_file_without_the_id(repo):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    approved = yaml.safe_load((repo / "model" / "approved" / "application.yaml").read_text(encoding="utf-8"))
    approved["elements"] = [e for e in approved["elements"] if e["id"] != "app-erp-core"]
    (repo / "model" / "staging" / "application.yaml").write_text(yaml.safe_dump(approved, sort_keys=False), encoding="utf-8")
    result = promote.promote(repo, dry_run=True, today=TODAY)
    assert not id001(result.report), result.report.render()


def test_the_model_records_each_cross_file_replacement(repo):
    stage_copy(repo, "elements", "app-erp-core", "application.yaml")
    model, _docs, _config = dsl.load_merged(repo, zone_label="staging")
    assert [(i, a.name, s.name) for i, a, s in model.cross_file_replacements] == [("app-erp-core", "application.yaml", "dup.yaml")]
    plain, _docs, _config = dsl.load(repo, "approved")
    assert plain.cross_file_replacements == []
