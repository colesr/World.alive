"""
FROZEN FITNESS DEFINITION.
The evolution engine must NEVER modify this file (enforced by CI guard).
These tests encode your intent: any mutation of app/core.py must pass them.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import core  # noqa: E402


# --- Structural contract: required functions must exist ---------------------

def test_required_functions_exist():
    for name in ["load_config", "fetch_feeds", "cluster_items", "summarize", "send_email", "run"]:
        assert hasattr(core, name), f"core.py must define {name}()"
        assert callable(getattr(core, name))


# --- Config sanity -----------------------------------------------------------

def test_config_has_multiple_regions():
    config = core.load_config()
    assert len(config["feeds"]) >= 4, "Must cover at least 4 world regions"
    assert sum(len(v) for v in config["feeds"].values()) >= 6, "Must use at least 6 feeds"


def test_digest_word_limit_bounded():
    config = core.load_config()
    assert 200 <= config["digest_word_limit"] <= 1500


# --- Clustering behavior ------------------------------------------------------

def _fake(title, region="Europe", source="Test"):
    return {"title": title, "summary": "", "link": "", "region": region, "source": source}


def test_duplicate_stories_are_clustered():
    items = [
        _fake("Earthquake strikes northern Japan, tsunami warning issued"),
        _fake("Tsunami warning after powerful earthquake strikes northern Japan", region="Asia"),
        _fake("Central bank raises interest rates amid inflation concerns"),
    ]
    clusters = core.cluster_items(items, threshold=0.3)
    assert len(clusters) == 2, "Near-identical stories must merge into one cluster"


def test_distinct_stories_stay_separate():
    items = [
        _fake("Elections held in Brazil"),
        _fake("New species of frog discovered in Borneo rainforest"),
    ]
    clusters = core.cluster_items(items, threshold=0.3)
    assert len(clusters) == 2


# --- Safety: summarize must respect inputs and never crash on empty ----------

def test_summarize_handles_empty_input():
    config = core.load_config()
    out = core.summarize([], config)
    assert isinstance(out, str) and len(out) > 0


def test_no_test_file_imports_in_core():
    """core.py must never import from tests/ (prevents fitness gaming)."""
    src = inspect.getsource(core)
    assert "from tests" not in src and "import tests" not in src
