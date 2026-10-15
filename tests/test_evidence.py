"""The generated evidence pages (T37): G1/G2 from the frozen evaluation, and the contributions page."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tools"))

import contributions  # noqa: E402
import evidence  # noqa: E402

FREEZE = ROOT / "eval" / "out" / "freeze"


@pytest.mark.skipif(not (FREEZE / "pca_plus_spot" / "result.json").exists(),
                    reason="needs the frozen evaluation (python eval/evaluate.py --frozen)")
def test_the_g1_g2_page_is_what_the_checked_in_page_says():
    page = evidence.build(FREEZE)
    assert "## G1:" in page and "## G2:" in page and "G2 is not met" in page
    checked_in = (ROOT / "docs" / "evidence" / "G1_G2.md").read_text()
    assert page == checked_in, "docs/evidence/G1_G2.md is stale: run python eval/evidence.py"


def test_the_contributions_page_lists_every_team_member():
    try:
        since = contributions.default_since()
        rows = contributions.commits(since)
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout")
    page = contributions.page(since, rows)
    for person in contributions.OWNERS:
        assert f"| {person} |" in page
    assert all(r["author"] for r in rows)
