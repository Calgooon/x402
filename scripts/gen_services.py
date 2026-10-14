#!/usr/bin/env python3
"""Regenerate the README service table from the public agent registry."""

import argparse
import json
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen


REGISTRY_URL = "https://x402agency.com/.well-known/agents"
README = Path(__file__).resolve().parents[1] / "README.md"
START = "<!-- services:start -->"
END = "<!-- services:end -->"


def cell(value):
    """Keep registry text inside one Markdown table cell with plain punctuation."""
    return " ".join(escape(str(value).replace("\u2014", "-"), quote=False).split()).replace("|", "&#124;")


def price_range(pricing):
    tiers = pricing["tiers"]
    if not tiers:
        raise ValueError("Registry pricing has no tiers")
    low = min(tiers, key=lambda tier: tier["usd"])
    high = max(tiers, key=lambda tier: tier["usd"])
    if pricing["currency"] != "USD":
        raise ValueError("Registry tier currency is not USD")
    amount = f"${low['usd']}"
    if high["usd"] != low["usd"]:
        amount += f"-${high['usd']}"
    labels = low["label"]
    if high["label"] != low["label"]:
        labels += f" to {high['label']}"
    return f"{amount} USD ({pricing['model']}; {labels})"


def render_table(agents):
    if not agents:
        raise ValueError("Registry has no agents")
    rows = [
        "| Agent | What it does | Price range | Delivery |",
        "|:------|:-------------|:------------|:---------|",
    ]
    for agent in agents:
        delivery = agent["delivery"]
        if delivery not in ("sync", "async"):
            raise ValueError(f"Unknown delivery mode: {delivery}")
        values = (agent["displayName"], agent["tagline"], price_range(agent["pricing"]), delivery)
        rows.append("| " + " | ".join(cell(value) for value in values) + " |")
    return "\n".join(rows)


def update_readme(readme, table):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README must contain exactly one pair of services markers")
    before, rest = readme.split(START)
    _, after = rest.split(END)
    return before + START + "\n" + table + "\n" + END + after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=REGISTRY_URL, help="Registry URL or local JSON file")
    parser.add_argument("--readme", type=Path, default=README, help="README to update")
    args = parser.parse_args()
    if args.registry.startswith(("https://", "http://")):
        request = Request(args.registry, headers={
            "User-Agent": "x402-services-generator/1.0",
            "Accept": "application/json",
        })
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    else:
        raw = Path(args.registry).read_bytes()
    agents = json.loads(raw, parse_float=Decimal)["agents"]
    updated = update_readme(args.readme.read_text(encoding="utf-8"), render_table(agents))
    args.readme.write_text(updated, encoding="utf-8")
    print(f"Updated service table: {len(agents)} agents")


if __name__ == "__main__":
    main()
