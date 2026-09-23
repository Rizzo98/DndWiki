"""The fixture loader, and the property that makes fixtures safe to remove.

The first half of this file builds its OWN fixture in tmp_path. That is the part
that matters for the long run: when evals/fixtures/ is emptied the harness keeps
full coverage, because nothing here depends on a recording being present.

The last test grades the real fixture's stored baseline - offline, with no model -
and asserts that the deterministic checks actually fire on the draft that shipped.
It skips itself when that fixture is gone, which is the intended end state.
"""

from __future__ import annotations

import json

import pytest
import yaml

from evals import checks, fixtures

REAL_FIXTURE = "bugie_inutili"


# --------------------------------------------------------------------------
# a fixture of our own, so these tests outlive the real ones
# --------------------------------------------------------------------------


def write_fixture(root, name: str = "synthetic", **overrides):
    """Build a minimal but complete fixture directory under 'root'."""
    path = root / name
    path.mkdir(parents=True)

    meta = overrides.get("meta", {"name": name, "session_id": "s-1", "campaign_id": "c-1"})
    expected = overrides.get(
        "expected",
        {"checks": [{"id": "beats", "kind": "min_beats", "applies_to": "run", "value": 1}]},
    )
    artifact = overrides.get(
        "artifact",
        {
            "session_id": "s-1",
            "utterances": [
                {
                    "id": "u_00001",
                    "start": 65.0,
                    "text": "Sto portando il pranzo a mio zio.",
                    "speaker": {"character_name": "Dungeon Master"},
                    "status": "auto_low",
                }
            ],
        },
    )
    baseline = overrides.get("baseline", {"summary": "a draft", "summary_blocks": []})

    (path / fixtures.META_NAME).write_text(yaml.safe_dump(meta), encoding="utf-8")
    (path / fixtures.EXPECTED_NAME).write_text(yaml.safe_dump(expected), encoding="utf-8")
    (path / fixtures.ATTRIBUTED_NAME).write_text(json.dumps(artifact), encoding="utf-8")
    (path / fixtures.BASELINE_NAME).write_text(json.dumps(baseline), encoding="utf-8")
    return path


# --- discovery ---------------------------------------------------------------


def test_an_empty_tree_is_a_valid_state_not_an_error(tmp_path):
    """The state the repository returns to once the fixtures are removed."""
    assert fixtures.available(tmp_path) == []
    assert fixtures.available(tmp_path / "does-not-exist") == []


def test_a_directory_without_a_manifest_is_not_a_fixture(tmp_path):
    """A half-deleted or scratch directory is ignored, never half-loaded."""
    (tmp_path / "leftovers").mkdir()
    (tmp_path / "leftovers" / fixtures.ATTRIBUTED_NAME).write_text("{}", encoding="utf-8")
    assert fixtures.available(tmp_path) == []


def test_discovery_finds_what_is_there_and_nothing_else(tmp_path):
    write_fixture(tmp_path, "alpha")
    write_fixture(tmp_path, "beta")
    (tmp_path / "not-a-fixture").mkdir()
    assert fixtures.available(tmp_path) == ["alpha", "beta"]


def test_discovery_needs_no_registry_to_update(tmp_path):
    """Adding a fixture is writing a directory; removing one is deleting it."""
    write_fixture(tmp_path, "alpha")
    assert fixtures.available(tmp_path) == ["alpha"]
    fixtures.drop("alpha", tmp_path)
    assert fixtures.available(tmp_path) == []


# --- loading -----------------------------------------------------------------


def test_load_reads_the_input_the_expectations_and_the_baseline(tmp_path):
    write_fixture(tmp_path, "alpha")
    fixture = fixtures.load("alpha", tmp_path)
    assert fixture.name == "alpha"
    assert fixture.session_id == "s-1"
    assert len(fixture.utterances()) == 1
    assert fixture.checks()[0]["id"] == "beats"
    assert fixture.baseline["summary"] == "a draft"


def test_a_missing_fixture_says_what_is_installed(tmp_path):
    write_fixture(tmp_path, "alpha")
    with pytest.raises(fixtures.FixtureError) as excinfo:
        fixtures.load("gamma", tmp_path)
    assert "not installed" in str(excinfo.value)
    assert "alpha" in str(excinfo.value)


def test_a_fixture_missing_a_part_names_the_part(tmp_path):
    path = write_fixture(tmp_path, "alpha")
    (path / fixtures.EXPECTED_NAME).unlink()
    with pytest.raises(fixtures.FixtureError) as excinfo:
        fixtures.load("alpha", tmp_path)
    assert fixtures.EXPECTED_NAME in str(excinfo.value)


def test_malformed_yaml_is_reported_as_such(tmp_path):
    path = write_fixture(tmp_path, "alpha")
    (path / fixtures.EXPECTED_NAME).write_text("checks: [unclosed", encoding="utf-8")
    with pytest.raises(fixtures.FixtureError) as excinfo:
        fixtures.load("alpha", tmp_path)
    assert "not valid YAML" in str(excinfo.value)


def test_malformed_json_is_reported_as_such(tmp_path):
    path = write_fixture(tmp_path, "alpha")
    (path / fixtures.ATTRIBUTED_NAME).write_text("{nope", encoding="utf-8")
    with pytest.raises(fixtures.FixtureError) as excinfo:
        fixtures.load("alpha", tmp_path)
    assert "not valid JSON" in str(excinfo.value)


def test_evidence_renders_lines_the_way_the_pipeline_view_does(tmp_path):
    """A judge must read the same certainty markers the extractor read."""
    write_fixture(tmp_path, "alpha")
    fixture = fixtures.load("alpha", tmp_path)
    (line,) = fixture.evidence(["u_00001"])
    assert line == "[u_00001 00:01:05] Dungeon Master: Sto portando il pranzo a mio zio."


def test_evidence_ignores_refs_the_fixture_does_not_have(tmp_path):
    write_fixture(tmp_path, "alpha")
    fixture = fixtures.load("alpha", tmp_path)
    assert fixture.evidence(["u_99999"]) == []


# --- removal -----------------------------------------------------------------


def test_drop_removes_the_whole_fixture_directory(tmp_path):
    path = write_fixture(tmp_path, "alpha")
    assert path.is_dir()
    fixtures.drop("alpha", tmp_path)
    assert not path.exists()


def test_drop_refuses_a_directory_that_is_not_a_fixture(tmp_path):
    """Deleting is guarded: the removal path must be impossible to half-do."""
    (tmp_path / "important").mkdir()
    (tmp_path / "important" / "data.txt").write_text("x", encoding="utf-8")
    with pytest.raises(fixtures.FixtureError):
        fixtures.drop("important", tmp_path)
    assert (tmp_path / "important" / "data.txt").is_file()


def test_size_is_reported_so_a_fixture_can_be_weighed(tmp_path):
    """--list shows the size because these recordings are not small files."""
    path = write_fixture(tmp_path, "alpha")
    (path / "filler.bin").write_bytes(b"x" * 200_000)
    assert fixtures.size_mb("alpha", tmp_path) > 0


# --------------------------------------------------------------------------
# the real fixture: the checks must fire on the draft that actually shipped
# --------------------------------------------------------------------------

_HAS_REAL = REAL_FIXTURE in fixtures.available()


@pytest.mark.skipif(not _HAS_REAL, reason=f"fixture {REAL_FIXTURE} is not installed")
async def test_the_shipped_draft_fails_the_checks_that_claim_it_does():
    """The self-test that keeps the eval honest, and it needs no model.

    A fixture asserts which checks the stored draft fails. If a check listed
    there comes out PASS, the check no longer detects the defect it was written
    for and the eval has quietly stopped measuring anything - which is a worse
    failure than a red run, so it is an error here.
    """
    fixture = fixtures.load(REAL_FIXTURE)
    known = fixture.known_baseline_failures()
    assert known, "the fixture must declare which checks the shipped draft fails"

    blocks = list(fixture.baseline.get("summary_blocks") or [])
    narrative = str(fixture.baseline.get("summary") or "")

    outcomes = await checks.evaluate(
        fixture.checks(),
        subject="baseline",
        narrative=narrative,
        blocks=blocks,
        evidence_for=fixture.evidence,
        judge=None,  # offline: judge checks report themselves as skipped
    )
    by_id = {outcome.id: outcome for outcome in outcomes}

    for check_id in known:
        assert check_id in by_id, f"{check_id} is asserted but not in expected.yaml"
        outcome = by_id[check_id]
        if outcome.status == checks.SKIPPED:
            continue  # needs the judge; --baseline --judge covers that
        assert outcome.status in checks.NOT_PASSED, (
            f"{check_id} was listed as a known failure of the shipped draft but "
            f"came out {outcome.status}: {outcome.detail}"
        )

    assert any(
        by_id[check_id].status in checks.NOT_PASSED
        for check_id in known
        if by_id[check_id].status != checks.SKIPPED
    ), "no deterministic check fires on the shipped draft: the eval measures nothing"
