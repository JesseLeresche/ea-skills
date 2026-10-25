"""The CLI is what skills and CI call; its exit codes are the contract."""

import json
import shutil

import pytest

from easkills import cli, genschema, oracle


def test_validate_exit_zero_on_clean_model(example_root, capsys):
    assert cli.main(["validate", "--root", str(example_root)]) == 0
    assert "PASS" in capsys.readouterr().out


def test_validate_exit_one_on_broken_model(broken_root, capsys):
    assert cli.main(["validate", "--root", str(broken_root)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_strict_mode_fails_on_warnings(broken_root, capsys):
    assert cli.main(["validate", "--root", str(broken_root), "--strict"]) == 1
    capsys.readouterr()


@pytest.fixture()
def warnings_only_root(example_root, tmp_path):
    """The clean example with one provenance quote paraphrased: PROV004, a warning."""
    root = tmp_path / "model-repo"
    shutil.copytree(example_root, root)
    business = root / "model" / "approved" / "business.yaml"
    text = business.read_text(encoding="utf-8")
    quote = "B2B customers place orders through the online order portal"
    assert quote in text
    business.write_text(text.replace(quote, "B2B customers place orders through the on-line order portal"), encoding="utf-8")
    return root


def _summary(out: str) -> str:
    return next(line for line in reversed(out.splitlines()) if "error(s)" in line)


def test_strict_summary_does_not_claim_a_pass_it_exits_one_for(warnings_only_root, capsys):
    assert cli.main(["validate", "--root", str(warnings_only_root), "--strict"]) == 1
    summary = _summary(capsys.readouterr().out)
    assert "treated as errors under --strict" in summary
    assert "FAIL" in summary
    assert "PASS" not in summary


def test_non_strict_summary_on_warnings_is_still_a_pass(warnings_only_root, capsys):
    assert cli.main(["validate", "--root", str(warnings_only_root)]) == 0
    summary = _summary(capsys.readouterr().out)
    assert summary.endswith("PASS")
    assert "--strict" not in summary


def test_strict_summary_is_unchanged_on_a_clean_model(example_root, capsys):
    assert cli.main(["validate", "--root", str(example_root), "--strict"]) == 0
    assert _summary(capsys.readouterr().out) == "0 error(s), 0 warning(s) -- ✓ PASS"


def test_strict_summary_is_unchanged_when_there_are_real_errors(broken_root, capsys):
    cli.main(["validate", "--root", str(broken_root)])
    plain = _summary(capsys.readouterr().out)
    assert cli.main(["validate", "--root", str(broken_root), "--strict"]) == 1
    assert _summary(capsys.readouterr().out) == plain


def test_validate_writes_json_report(example_root, tmp_path, capsys):
    report_path = tmp_path / "report.json"
    assert cli.main(["validate", "--root", str(example_root), "--json", str(report_path)]) == 0
    capsys.readouterr()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["zone"] == "approved"


def test_compile_writes_exchange_file(example_root, tmp_path, capsys):
    out = tmp_path / "model.xml"
    assert cli.main(["compile", "--root", str(example_root), "--out", str(out)]) == 0
    assert "XSD validation passed" in capsys.readouterr().out
    assert out.exists()


def test_compile_refuses_invalid_model(broken_root, tmp_path, capsys):
    out = tmp_path / "model.xml"
    assert cli.main(["compile", "--root", str(broken_root), "--out", str(out)]) == 1
    assert "Refusing to compile" in capsys.readouterr().out
    assert not out.exists()


def test_validate_facts_exit_zero_on_clean_register(example_root, capsys):
    assert cli.main(["validate-facts", "--root", str(example_root)]) == 0
    assert "PASS" in capsys.readouterr().out


def test_validate_facts_exit_one_on_broken_register(broken_root, capsys):
    assert cli.main(["validate-facts", "--root", str(broken_root)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_chunk_outputs_every_source(example_root, capsys):
    assert cli.main(["chunk", "--root", str(example_root)]) == 0
    out = capsys.readouterr().out
    assert "interview-operations-2026-07-15.md" in out
    assert "systems-inventory-2026-06.md" in out
    assert "c001" in out


def test_chunk_json_output(example_root, capsys):
    assert cli.main(["chunk", "--root", str(example_root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload and {"id", "file", "start_line", "end_line", "text"} <= set(payload[0])


def test_coverage_gate_passes_on_full_coverage(example_root, capsys):
    assert cli.main(["coverage", "--root", str(example_root), "--min-coverage", "100"]) == 0
    assert "100%" in capsys.readouterr().out


def test_coverage_gate_fails_below_threshold(broken_root, capsys):
    assert cli.main(["coverage", "--root", str(broken_root), "--min-coverage", "100"]) == 1
    assert "below" in capsys.readouterr().out


def test_oracle_info(capsys):
    assert cli.main(["oracle-info"]) == 0
    out = capsys.readouterr().out
    assert "3.2" in out
    assert "FAIL" not in out


@pytest.mark.parametrize("path,builder", genschema.SCHEMAS, ids=lambda value: getattr(value, "name", ""))
def test_generated_schemas_are_committed_and_current(path, builder):
    """A stale committed schema would silently accept what the validators reject.

    Parametrized over the same registry ``gen-schema`` writes, so every generated
    schema is covered by construction rather than by remembering to add a line here.
    """
    assert path.is_file(), f"missing generated schema {path.name}: run 'python -m easkills gen-schema'"
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == builder(), f"{path.name} is stale: run 'python -m easkills gen-schema'"


def _tampered_pins(tmp_path):
    """A pin file that no oracle file can match (simulates drift or tampering)."""
    pins = tmp_path / "SHA256SUMS"
    pins.write_text(f"{'0' * 64}  relationships.xml\n", encoding="utf-8", newline="\n")
    return pins


@pytest.mark.parametrize("command", ["compile", "render", "docs", "gen-schema"])
def test_oracle_pin_drift_blocks_every_command_that_consumes_the_oracle(
    command, example_root, tmp_path, monkeypatch, capsys
):
    """Reading tampered primary-source data must refuse, not produce artifacts.

    ``validate`` reports drift as an ORACLE001 finding; these commands have no report
    to carry it, and ``gen-schema`` would write the authoring contract from the
    tampered matrix -- so they refuse before touching it.
    """
    monkeypatch.setattr(oracle, "CHECKSUMS", _tampered_pins(tmp_path))
    argv = [command]
    if command != "gen-schema":
        argv += ["--root", str(example_root), "--out", str(tmp_path / "out")]
    assert cli.main(argv) == 1
    assert "ORACLE001" in capsys.readouterr().out


def test_schema_enumerates_every_oracle_concept():
    schema = genschema.load_schema(genschema.SCHEMA_PATH)
    assert set(schema["$defs"]["element"]["properties"]["type"]["enum"]) == set(oracle.element_types())
    assert set(schema["$defs"]["relationship"]["properties"]["type"]["enum"]) == set(oracle.relationship_types())
