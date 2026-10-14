"""Service generation preserves registry values and README boundaries."""

import runpy
from decimal import Decimal
from pathlib import Path

import pytest


GEN = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/gen_services.py"))


def agent(name, delivery="sync"):
    return {
        "displayName": name,
        "tagline": "Text | with\nmarkup <tag> and a long dash \u2014",
        "delivery": delivery,
        "pricing": {
            "model": "per-resolution",
            "currency": "USD",
            "tiers": [
                {"label": "4K", "usd": Decimal("0.375")},
                {"label": "1K", "usd": Decimal("0.1875")},
            ],
        },
    }


def test_all_agents_exact_bounds_and_safe_cells():
    table = GEN["render_table"]([agent("First"), agent("Second", "async")])
    assert len(table.splitlines()) == 4
    assert "$0.1875-$0.375 USD (per-resolution; 1K to 4K)" in table
    assert "| sync |" in table and "| async |" in table
    assert "Text &#124; with markup &lt;tag&gt; and a long dash -" in table
    assert all(line.count("|") == 5 for line in table.splitlines())


def test_only_generated_region_changes_and_regeneration_is_stable():
    before = "Keep this\n<!-- services:start -->\nstale\n<!-- services:end -->\nAnd this\n"
    table = GEN["render_table"]([agent("First")])
    after = GEN["update_readme"](before, table)
    assert after == "Keep this\n<!-- services:start -->\n" + table + "\n<!-- services:end -->\nAnd this\n"
    assert GEN["update_readme"](after, table) == after


@pytest.mark.parametrize("text", [
    "No markers", "<!-- services:start -->", "<!-- services:end -->\n<!-- services:start -->",
    "<!-- services:start --><!-- services:start --><!-- services:end -->",
])
def test_bad_markers_are_rejected(text):
    with pytest.raises(ValueError):
        GEN["update_readme"](text, "table")


def test_incomplete_registry_is_rejected():
    with pytest.raises(ValueError):
        GEN["render_table"]([])
    with pytest.raises(ValueError):
        GEN["render_table"]([agent("First", "unknown")])
    item = agent("First")
    item["pricing"]["tiers"] = []
    with pytest.raises(ValueError):
        GEN["render_table"]([item])
