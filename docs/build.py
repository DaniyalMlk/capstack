#!/usr/bin/env python3
"""Write the site out.

    python docs/build.py

Kept apart from ``docs/site.py`` so that the generator itself touches no
filesystem and the tests can call it directly. No dependency beyond the
standard library and the engine, because a documentation build that needs a
toolchain is a documentation build that stops working.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# `docs/site.py` would shadow the standard library's `site`, which is already
# imported before this script runs, so the generator is `pages`.
from pages import ROOT, build_site, measure

OUT = ROOT / "site"


def main() -> int:
    # Cleared rather than written over: a page the generator no longer produces
    # should not survive in the published site because it was there last time.
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    measured = measure()
    files = build_site(measured)
    for name, content in files.items():
        (OUT / name).write_text(content, encoding="utf-8")

    cells = sum(len(row) for row in measured.grid.cells)
    print(
        f"site: {', '.join(sorted(files))}\n"
        f"memo: {measured.report.name}, {len(measured.report)} sections; "
        f"grid: {cells} cells, {len(measured.grid.populated)} reachable, "
        f"{len(measured.grid.breaches)} breaching"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
