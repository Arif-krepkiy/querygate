"""Offline retrieval hit@k over the golden question set.

Loads the catalog, replays every 'retrieval' case against the index, and prints
hit@1 / hit@3. No LLM, no warehouse, no auth.

    python evals/eval_retrieval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.retrieval.index import CatalogIndex

QUESTIONS = Path(__file__).resolve().parent / "questions.json"


def main() -> int:
    cases = [c for c in json.loads(QUESTIONS.read_text())["cases"] if c["type"] == "retrieval"]
    catalog = bundle.load_bundle(config.CATALOG_LOCAL_PATH)
    index = CatalogIndex.build(catalog)

    hits1 = hits3 = 0
    for case in cases:
        ranked = [m.name for m in index.search(case["q"], 3)]
        expect = set(case["expect_models"])
        h1 = bool(ranked[:1]) and ranked[0] in expect
        h3 = bool(expect & set(ranked))
        hits1 += h1
        hits3 += h3
        mark = "OK " if h3 else "MISS"
        print(f"[{mark}] {case['q']!r:45} -> {ranked}  (expect any of {sorted(expect)})")

    n = len(cases)
    print(f"\nhit@1 = {hits1}/{n} ({hits1 / n:.0%})   hit@3 = {hits3}/{n} ({hits3 / n:.0%})")
    return 0 if hits3 == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
