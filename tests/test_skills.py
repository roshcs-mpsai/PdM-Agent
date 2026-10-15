"""Skills for the diagnostic agent (T32; PR-11, AGT-06): pdm_common.skills and the skill text.

* The agent loads the two shared skills and the folder the profile names; a
  missing folder, SKILL.md or front-matter field stops it with a clear error.
* Versions are stable for the same text and change with any edit (tested on a
  copy in tmp_path).
* The text stays small enough for a system prompt, the shared skills stay
  asset-neutral, and no skill carries a date the agent could use as hindsight.
* The example brief in brief_writing passes pdm_common.brief.validate_brief
  against the results it cites (tests/fixtures/brief_example_results.json).
* Every check id, triage rule and tool name the skills mention exists.
"""
import copy
import json
import re
import shutil
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from pdm_common import schemas
from pdm_common.brief import validate_brief
from pdm_common.checks import KINDS as CHECK_KINDS
from pdm_common.checks import CheckResult, load_registry
from pdm_common.profile import load_profile
from pdm_common.skills import SkillError, load_skill, load_skills, system_text, versions

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
PROFILES = sorted(ROOT.glob("profiles/*/asset_profile.yaml"))
EXAMPLE_RESULTS = ROOT / "tests" / "fixtures" / "brief_example_results.json"

MAX_WORDS_PER_SKILL = 1200       # each skill goes into every model turn's system prompt
MAX_WORDS_TOTAL = 3500
TOOLS = ("get_telemetry", "attribute_anomaly", "get_maintenance_history", "search_knowledge", "run_check",
         "device_health")        # AGT-01
RUNTIME_NAMES = ("submit_brief", "incident_context")

CHECK_ID = re.compile(r"(?<![A-Za-z0-9_])C\d+(?![A-Za-z0-9_])")
RULE_ID = re.compile(r"(?<![A-Za-z0-9_])A\d+(?![A-Za-z0-9_])")
SNAKE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d+\.\d+\.\d+)@[0-9a-f]{12}$")
MONTH_DAY = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}\b")


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def skills(profile):
    return load_skills(profile, ROOT)


def _skill(skills, name):
    return next(s for s in skills if s.name == name)


def _signal_names() -> set:
    """Every signal of every asset profile: the CFG-03 name guard's list (tests/test_profiles.py)."""
    names = set()
    for path in PROFILES:
        p = yaml.safe_load(path.read_text())
        names |= {s["name"] for kind in ("analog", "digital") for s in p["signals"][kind]}
    return names


def _names_in(text: str, names: set) -> set:
    return {n for n in names if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text)}


def _strings(obj, out: list) -> list:
    """Every key and string value in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out)
    elif isinstance(obj, str):
        out.append(obj)
    return out


def _vocabulary(profile: dict, profile_path: Path, loaded) -> set:
    """Every snake_case name a skill may use: the tools, the fields of the schemas and of a check
    result, the profile's keys, checks and features, the triage rules' output and the skills' names."""
    texts = list(TOOLS + RUNTIME_NAMES) + [f.name for f in fields(CheckResult)] + list(CHECK_KINDS)
    for path in sorted((ROOT / "schemas").glob("*.json")):
        _strings(json.loads(path.read_text()), texts)
    _strings(profile, texts)
    features = (profile.get("references") or {}).get("features_file")
    if features:
        _strings(yaml.safe_load((profile_path.parent / features).read_text()), texts)
    _strings(load_registry(profile, ROOT).triage([]), texts)
    texts += [s.name for s in loaded]
    return set(SNAKE.findall("\n".join(texts)))


def _example_brief() -> dict:
    text = (ROOT / "skills" / "shared" / "brief_writing" / "SKILL.md").read_text()
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) == 1, "brief_writing holds exactly one JSON example"
    return json.loads(blocks[0])


# --- loading ----------------------------------------------------------------------------------------

def test_the_agent_loads_the_two_shared_skills_and_the_profiles_folder(profile, skills):
    assert [(s.name, s.kind) for s in skills] == [("brief_writing", "shared"), ("incident_triage", "shared"),
                                                  ("apu_metropt3", "asset")]
    asset = skills[-1]
    assert asset.path == ROOT / profile["references"]["skills_folder"]
    assert asset.asset == profile["asset"]["id"]
    assert asset.files[0] == "SKILL.md" and "inspection.md" in asset.files
    assert "# Inspection tasks and safety notes" in asset.text          # every .md of the folder is read
    for s in skills:
        assert s.text and not s.text.startswith("---") and "\nversion:" not in s.text   # no front matter
        assert re.fullmatch(r"[0-9a-f]{64}", s.sha256) and s.description
    assert versions(load_skills(profile)) == versions(skills)            # the default root is this checkout


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.parent.name)
def test_every_profile_loads_its_own_skill_folder(path):
    profile = load_profile(path)
    loaded = load_skills(profile, ROOT)
    assert [s.kind for s in loaded].count("shared") >= 2
    assert loaded[-1].kind == "asset" and loaded[-1].asset == profile["asset"]["id"]


def test_versions_and_the_system_text(skills):
    stamps = versions(skills)
    assert list(stamps) == [s.name for s in skills]
    assert all(STAMP.match(v) for v in stamps.values()), stamps
    text = system_text(skills)
    for s in skills:
        assert f'<skill name="{s.name}" kind="{s.kind}" version="{s.stamp}">\n{s.text}\n</skill>' in text
    assert text.count("<skill ") == text.count("</skill>") == len(skills)
    # in the order given, so the agent decides it
    assert text.index('"brief_writing"') < text.index('"incident_triage"') < text.index('"apu_metropt3"')
    assert system_text(skills[::-1]).startswith('<skill name="apu_metropt3"')
    with pytest.raises(SkillError, match="two skills are named"):
        versions(skills + skills[:1])


def test_versions_are_stable_and_change_when_a_file_changes(profile, tmp_path):
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    before = versions(load_skills(profile, ROOT))
    assert versions(load_skills(profile, tmp_path)) == before            # the checkout's place does not count
    folder = tmp_path / profile["references"]["skills_folder"]
    asset = folder.name

    def stamps():
        return versions(load_skills(profile, tmp_path))

    # an edit to the asset folder's second file changes the asset skill alone, and not its front-matter version
    p = folder / "inspection.md"
    p.write_text(p.read_text() + "\nOne more line.\n")
    after = stamps()
    assert after[asset] != before[asset] and after[asset].split("@")[0] == before[asset].split("@")[0]
    assert {k: v for k, v in after.items() if k != asset} == {k: v for k, v in before.items() if k != asset}
    # an edit to a shared skill changes that skill alone
    t = tmp_path / "skills" / "shared" / "incident_triage" / "SKILL.md"
    t.write_text(t.read_text().replace("You diagnose one incident.", "You diagnose a single incident."))
    changed = stamps()
    assert changed["incident_triage"] != before["incident_triage"]
    assert changed["brief_writing"] == before["brief_writing"]
    # a new Markdown file counts, a renamed one counts, other files do not
    current = changed[asset]
    (folder / "notes.txt").write_text("not read by the agent\n")
    assert stamps()[asset] == current
    (folder / "extra.md").write_text("# Extra\n\nMore text.\n")
    added = stamps()[asset]
    assert added != current and "More text." in load_skills(profile, tmp_path)[-1].text
    (folder / "extra.md").rename(folder / "more.md")
    assert stamps()[asset] != added
    # a version bump shows in the stamp
    s = folder / "SKILL.md"
    s.write_text(s.read_text().replace('version: "2026-10-10"', 'version: "1.1.0"', 1))
    assert stamps()[asset].startswith("1.1.0@")
    # line endings and a byte-order mark do not count: a Windows checkout records the same version and text
    lf = load_skills(profile, tmp_path)[-1]
    s.write_bytes(b"\xef\xbb\xbf" + s.read_bytes().replace(b"\n", b"\r\n"))
    crlf = load_skills(profile, tmp_path)[-1]
    assert (crlf.stamp, crlf.text) == (lf.stamp, lf.text)


def test_a_missing_folder_or_skill_file_is_a_clear_error(profile, tmp_path):
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    elsewhere = dict(profile, references=dict(profile["references"], skills_folder="skills/no_such_asset"))
    with pytest.raises(SkillError, match=r"no_such_asset: the asset skill folder named in the profile "
                                         r"\(references.skills_folder"):
        load_skills(elsewhere, tmp_path)
    with pytest.raises(SkillError, match=r"names no skill folder \(references.skills_folder\)"):
        load_skills(dict(profile, references={}), tmp_path)
    with pytest.raises(SkillError, match="no shared skills folder"):
        load_skills(profile, tmp_path / "empty")
    (tmp_path / "skills" / "shared" / "half_written").mkdir()
    with pytest.raises(SkillError, match="half_written: no SKILL.md"):
        load_skills(profile, tmp_path)
    (tmp_path / "skills" / "shared" / "half_written").rmdir()
    (tmp_path / profile["references"]["skills_folder"] / "SKILL.md").unlink()
    with pytest.raises(SkillError, match=f"{profile['references']['skills_folder']}: no SKILL.md"):
        load_skills(profile, tmp_path)


@pytest.mark.parametrize("front_matter, kind, problem", [
    (None, "shared", "no front matter"),
    ("name: demo\nversion: '1.0.0'", "shared", "lacks description"),
    ("name: other\ndescription: d\nversion: '1.0.0'", "shared", "does not match its folder"),
    ("name: demo\ndescription: d\nversion: 1.0", "shared", "quote it"),
    ("name: demo\ndescription: d\nversion: v2", "shared", "date .* or semver"),
    ("name: demo\ndescription: d\nversion: 2026-13-45", "shared", "front matter"),
    ("name: demo\ndescription: d\nversion: '1.0.0'\nasset: some_asset", "shared", "a shared skill names no asset"),
    ("name: demo\ndescription: d\nversion: '1.0.0'", "asset", "names its asset in front matter"),
    ("name: demo\ndescription: [unclosed", "shared", "not valid YAML"),
])
def test_front_matter_is_checked(tmp_path, front_matter, kind, problem):
    folder = tmp_path / "demo"
    folder.mkdir()
    head = "" if front_matter is None else f"---\n{front_matter}\n---\n"
    (folder / "SKILL.md").write_text(head + "# Demo\n\nText.\n")
    with pytest.raises(SkillError, match=problem):
        load_skill(folder, kind, asset_id="demo_asset")


def test_a_well_formed_skill_folder(tmp_path):
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: demo\ndescription: d\nversion: 2026-10-10\nasset: demo_asset\n"
                                     "---\n# Demo\n\nText.\n")
    (folder / "b.md").write_text("---\nnote: front matter in a second file is dropped too\n---\n# B\n")
    (folder / "a.md").write_text("# A\n")
    s = load_skill(folder, "asset", asset_id="demo_asset")
    assert (s.version, s.files, s.asset) == ("2026-10-10", ("SKILL.md", "a.md", "b.md"), "demo_asset")
    assert s.text == "# Demo\n\nText.\n\n# A\n\n# B" and s.words == 4      # '#' is not a word


# --- the text -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.parent.name)
def test_the_skills_fit_a_system_prompt(path):
    loaded = load_skills(load_profile(path), ROOT)
    for s in loaded:
        assert s.words <= MAX_WORDS_PER_SKILL, f"{s.name}: {s.words} words, over {MAX_WORDS_PER_SKILL}"
    total = sum(s.words for s in loaded)
    assert total <= MAX_WORDS_TOTAL, f"{total} words in all, over {MAX_WORDS_TOTAL}"


def test_the_loader_names_no_asset_signal():
    """CFG-03 for pdm_common.skills (tests/test_profiles.py guards all of lib/ as well)."""
    names = _signal_names()
    assert len(names) >= 15
    assert not _names_in((ROOT / "lib" / "pdm_common" / "skills.py").read_text(), names)


def test_shared_skills_are_asset_neutral(skills):
    """A new asset adds a folder without changing the shared skills, so they hold no asset facts:
    no signal names (H1, H2 there are brief_v1 hypothesis ids), no check ids, no asset ids."""
    names = _signal_names()
    assets = {yaml.safe_load(p.read_text())["asset"]["id"] for p in PROFILES}
    for s in (s for s in skills if s.kind == "shared"):
        signals = {n for n in _names_in(s.text, names) if not re.fullmatch(r"H\d+", n)}
        assert not signals, f"{s.name} names signals {sorted(signals)}"
        assert not CHECK_ID.findall(s.text), f"{s.name} names check ids"
        assert not [a for a in assets if a in s.text], f"{s.name} names an asset"


def test_no_skill_carries_a_date(skills):
    """No hindsight (AGT-04): no failure, repair or campaign dates in what the agent reads."""
    for s in skills:
        assert not re.search(r"\b(19|20)\d\d-\d\d-\d\d\b", s.text), s.name
        assert not MONTH_DAY.search(s.text), (s.name, MONTH_DAY.search(s.text))
        assert not re.search(r"\b20(20|22)\b", s.text), s.name


def test_the_asset_skill_covers_its_signals_and_checks(profile, skills):
    asset = skills[-1]
    signals = [x["name"] for kind in ("analog", "digital") for x in profile["signals"][kind]]
    assert not [n for n in signals if n not in _names_in(asset.text, set(signals))]
    suspect = [x["name"] for x in profile["signals"]["digital"] if x.get("polarity") == "suspect_inverted"]
    row = next(line for line in asset.text.splitlines() if all(n in line for n in suspect))
    assert "never evidence" in row


# --- the example brief ----------------------------------------------------------------------------------

def test_the_example_brief_validates_against_the_results_it_cites():
    example = _example_brief()
    results = json.loads(EXAMPLE_RESULTS.read_text())
    for rid, r in results.items():                         # shaped like stored tool results
        assert r["result_id"] == rid and schemas.validate("tool_result_v1", r) == [], rid
    context = next(r for r in results.values() if r["tool"] == "incident_context")
    assert schemas.validate("prognosis_v1", context["output"]["prognosis"]) == []
    for r in (r for r in results.values() if r["tool"] == "run_check"):
        for entry in r["output"]["results"]:
            CheckResult(**entry)                           # the fields of a real check result
    assert validate_brief(example, results, incident_id=example["incident_id"]) == []
    # it shows what the skill asks for
    assert example["prognosis"] == {"cites": [context["result_id"]]}
    assert example["work_order"]["status"] == "draft"
    assert any(h.get("against") for h in example["hypotheses"])
    assert len({h["category"] for h in example["hypotheses"]}) >= 2


def test_the_validator_really_reads_the_example():
    """The example's grounding is not vacuous: a changed number, a citation of a result the run did
    not produce and a prognosis cited from the wrong result all fail."""
    example, results = _example_brief(), json.loads(EXAMPLE_RESULTS.read_text())
    wrong_number = copy.deepcopy(example)
    claim = wrong_number["hypotheses"][0]["claims"][0]
    claim["text"] = claim["text"].replace("2.41", "2.6")
    assert any("2.6 appears in none of" in p for p in validate_brief(wrong_number, results))
    unknown = copy.deepcopy(example)
    unknown["hypotheses"][0]["claims"][0]["cites"] = ["R-ab12cd34-9"]
    assert any("did not produce" in p for p in validate_brief(unknown, results))
    retyped = copy.deepcopy(example)
    retyped["prognosis"]["cites"] = ["R-ab12cd34-2"]
    assert any("not a stored prognosis" in p for p in validate_brief(retyped, results))


# --- what the skills mention exists ---------------------------------------------------------------------

@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.parent.name)
def test_every_check_rule_and_tool_the_skills_mention_exists(path):
    profile = load_profile(path)
    loaded = load_skills(profile, ROOT)
    declared = {c["id"] for c in profile["consistency_checks"]}
    for s in loaded:
        unknown = set(CHECK_ID.findall(s.text)) - declared
        assert not unknown, f"{s.name} mentions checks the profile does not declare: {sorted(unknown)}"
    asset = loaded[-1]
    assert set(CHECK_ID.findall(asset.text)) == declared, "the asset skill says what every check means"
    rules = set((load_registry(profile, ROOT).triage([]) or {}).get("rules", {}))
    unknown = set(RULE_ID.findall(asset.text)) - rules
    assert not unknown, f"{asset.name} mentions triage rules that do not exist: {sorted(unknown)}"
    vocabulary = _vocabulary(profile, path, loaded)
    for s in loaded:
        unknown = set(SNAKE.findall(s.text)) - vocabulary
        assert not unknown, f"{s.name} mentions names that are no tool, field, check or feature: {sorted(unknown)}"


def test_the_triage_skill_names_every_tool(skills):
    triage = _skill(skills, "incident_triage")
    for name in TOOLS + RUNTIME_NAMES:
        assert re.search(rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", triage.text), name
    order = [triage.text.index(f"\n{i}. {t}") for i, t in enumerate(
        ("run_check", "attribute_anomaly", "get_telemetry", "get_maintenance_history", "search_knowledge",
         "device_health"), start=1)]
    assert order == sorted(order)
    brief = _skill(skills, "brief_writing")
    assert "submit_brief" in brief.text and "incident_context" in brief.text
