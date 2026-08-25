from __future__ import annotations

import argparse
import random
from pathlib import Path

import pandas as pd


def main() -> None:
    p = argparse.ArgumentParser(
        description="Randomization schedule for a same-session, equal-capacity "
        "comparison of local6 vs standalone2 (both interleaved randomly within each "
        "block so any time-varying host load affects both arms equally, rather than "
        "running one scenario fully before the other)."
    )
    p.add_argument("--output", required=True)
    p.add_argument("--repeats", type=int, default=12)
    p.add_argument("--seed", type=int, default=20260817)
    args = p.parse_args()

    cells = [(w, s) for w in ("compact", "timeseries") for s in ("local6", "standalone2")]
    rng = random.Random(args.seed)
    rows = []
    global_order = 0
    for phase, blocks in (("warmup", [0]), ("measured", range(1, args.repeats + 1))):
        for block in blocks:
            block_cells = cells.copy()
            rng.shuffle(block_cells)
            for within, (workload, scenario) in enumerate(block_cells, 1):
                global_order += 1
                rows.append({"global_order": global_order, "phase": phase, "block": block,
                             "within_block_order": within, "workload": workload,
                             "scenario": scenario, "seed": args.seed})
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)


if __name__ == "__main__":
    main()
