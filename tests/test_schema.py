"""CFG-02: the asset profile is validated against a versioned schema, and a
malformed profile fails with errors that name the file and the line."""
import re
from pathlib import Path

import pytest
import yaml

from pdm_common.profile import ProfileError, load_profile
from pdm_common.schema import SCHEMA_VERSION, AssetProfile, validate_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
PROFILES = sorted((ROOT / "profiles").glob("*/asset_profile.yaml"))


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.parent.name)
def test_every_asset_profile_validates(path):
    text = path.read_text()
    model = validate_profile(yaml.safe_load(text), text, path)
    assert isinstance(model, AssetProfile)
    assert model.schema_version == SCHEMA_VERSION


def test_minimal_fixture_validates():
    profile = load_profile(FIXTURES / "minimal_asset_profile.yaml")
    assert profile["asset"]["id"] == "demo_pump"


def test_malformed_fixture_fails_on_exactly_the_marked_lines():
    path = FIXTURES / "malformed_asset_profile.yaml"
    marked = {i for i, line in enumerate(path.read_text().splitlines(), start=1)
              if not line.lstrip().startswith("#") and line.rstrip().endswith("# MALFORMED")}
    assert len(marked) == 8
    with pytest.raises(ProfileError) as info:
        load_profile(path)
    reported = {int(n) for n in re.findall(r"malformed_asset_profile\.yaml:(\d+):", str(info.value))}
    assert reported == marked


def _real_profile_without(tmp_path, section, key):
    source = PROFILES[0]
    data = yaml.safe_load(source.read_text())
    del data[section][key]
    path = tmp_path / "asset_profile.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_missing_key_is_named_with_its_parent_line(tmp_path):
    path = _real_profile_without(tmp_path, "topics", "status")
    with pytest.raises(ProfileError) as info:
        load_profile(path)
    message = str(info.value)
    assert "topics.status: Field required" in message
    topics_line = next(i for i, line in enumerate(path.read_text().splitlines(), start=1)
                       if line.startswith("topics:"))
    assert f"asset_profile.yaml:{topics_line}: topics.status" in message


def test_a_new_key_needs_a_schema_bump(tmp_path):
    data = yaml.safe_load(PROFILES[0].read_text())
    data["gate"]["cooldown"] = "1h"
    path = tmp_path / "asset_profile.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(ProfileError, match="gate.cooldown: Extra inputs are not permitted"):
        load_profile(path)


def test_old_schema_version_is_refused(tmp_path):
    data = yaml.safe_load(PROFILES[0].read_text())
    data["schema_version"] = "0.1"
    path = tmp_path / "asset_profile.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(ProfileError, match="schema_version"):
        load_profile(path)


def _minimal_with(tmp_path, old, new):
    text = (FIXTURES / "minimal_asset_profile.yaml").read_text()
    assert text.count(old) == 1, old
    path = tmp_path / "asset_profile.yaml"
    path.write_text(text.replace(old, new))
    return path


def _line_holding(path, fragment, occurrence=1):
    hits = [i for i, line in enumerate(path.read_text().splitlines(), start=1) if fragment in line]
    return hits[occurrence - 1]


def test_a_mapping_where_a_name_belongs_is_reported_not_raised(tmp_path):
    """A block-list typo ('- flow:') makes a dict; it must not crash the checks."""
    path = _minimal_with(tmp_path, "inputs: [flow, inlet_pressure]",
                         "inputs:\n    - flow:\n    - inlet_pressure")
    with pytest.raises(ProfileError) as info:
        load_profile(path)
    assert f"asset_profile.yaml:{_line_holding(path, '- flow:')}: detector.inputs[0]" in str(info.value)


def test_duplicate_keys_are_reported_at_the_copy_that_wins(tmp_path):
    path = _minimal_with(tmp_path, "gate: {span: 5min, min_fraction: 0.5, hold_off: 1h}\n",
                         "gate: {span: 5min, min_fraction: 0.5, hold_off: 1h}\n"
                         "gate: {span: 5min, min_fraction: 2.0, hold_off: 1h}\n")
    with pytest.raises(ProfileError) as info:
        load_profile(path)
    second = _line_holding(path, "gate:", occurrence=2)
    message = str(info.value)
    assert f"asset_profile.yaml:{second}: (document): duplicate key 'gate'" in message
    assert f"asset_profile.yaml:{second}: gate.min_fraction" in message


def test_an_impossible_date_names_its_line(tmp_path):
    path = _minimal_with(tmp_path, "campaign: [2021-01-01, 2021-01-31]",
                         "campaign: [2021-01-01, 2021-02-31]")
    with pytest.raises(ProfileError, match=rf"asset_profile\.yaml:{_line_holding(path, 'campaign')}: "):
        load_profile(path)
