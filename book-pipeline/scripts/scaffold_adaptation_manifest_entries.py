#!/usr/bin/env python3
"""
Emit ``samples`` manifest rows for **faithful** and **twist** adaptation leaves.

Use when you already have N public-domain (or local) sample *base* rows with stable ids
like ``gut-11-Alice-...`` and want many catalog entries that **reuse** the same preview paths.

Example (from book-pipeline repo root)::

  python scripts/scaffold_adaptation_manifest_entries.py \\
    --bases gut-11-Alice-s-Adventures-in-Wonderland,gut-43-The-strange-case-of-Dr.-Jekyll-and-Mr.-Hyde \\
    --manifest ../sceneweaver/public/samples/manifest.json \\
    --apply

Without ``--apply``, prints a JSON array of new sample objects only (merge manually).

Faithful grid: seasons in 1,2,3 × episodes_per_season in 6,8,10.
Twist grid: same season/episode grid × one twist_axis per base (rotates from a fixed list).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

TWIST_AXES = ["time_period", "character", "mood", "length", "extra_season", "prelude"]
SEASONS = [1, 2, 3]
EPS = [6, 8, 10]


def _novel_key(sample_id: str) -> str:
    m = re.match(r"^(?:gut-)?(\d+)", sample_id, re.I)
    if m:
        return f"gut-{m.group(1)}"
    m2 = re.match(r"^gut-(\d+)", sample_id, re.I)
    if m2:
        return f"gut-{m2.group(1)}"
    return sample_id.split("-")[0][:32]


def _find_base(manifest: dict[str, Any], base_id: str) -> dict[str, Any] | None:
    for s in manifest.get("samples") or []:
        if isinstance(s, dict) and s.get("id") == base_id:
            return s
    return None


def build_rows(base: dict[str, Any], idx: int) -> list[dict[str, Any]]:
    bid = str(base["id"])
    paths = base.get("paths") or {}
    prev = paths.get("preview")
    uask_path = paths.get("userAsk")
    if not prev or not uask_path:
        raise SystemExit(f"base {bid}: missing paths.preview / paths.userAsk")
    title = str(base.get("title") or bid)
    nk = _novel_key(bid)
    rows: list[dict[str, Any]] = []

    for s in SEASONS:
        for e in EPS:
            sid = f"faithful-{nk}-s{s}-e{e}"
            rows.append(
                {
                    "id": sid,
                    "title": f"{title} — faithful · {s} season(s) × {e} eps",
                    "userAsk": (
                        f"Faithful screenplay conversion for {title}: preserve plot and character arcs. "
                        f"Target {s} season(s), {e} episodes per season. No deliberate high-concept twist."
                    ),
                    "paths": {"preview": prev, "userAsk": uask_path},
                    "adaptation": {
                        "novelKey": nk,
                        "pipeline": "faithful",
                        "seasons": s,
                        "episodesPerSeason": e,
                        "assetSampleId": bid,
                    },
                }
            )

    axis = TWIST_AXES[idx % len(TWIST_AXES)]
    for s in (1, 2):
        for e in EPS:
            sid = f"twist-{nk}-{axis}-s{s}-e{e}"
            rows.append(
                {
                    "id": sid,
                    "title": f"{title} — twist ({axis}) · {s} season(s) × {e} eps",
                    "userAsk": (
                        f"Classical adaptation with twist axis `{axis}` for {title}. "
                        f"Target {s} season(s), {e} episodes per season. Keep story DNA recognizable."
                    ),
                    "paths": {"preview": prev, "userAsk": uask_path},
                    "adaptation": {
                        "novelKey": nk,
                        "pipeline": "twist",
                        "seasons": s,
                        "episodesPerSeason": e,
                        "twistAxis": axis,
                        "assetSampleId": bid,
                    },
                }
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", required=True, help="Comma-separated manifest sample ids")
    ap.add_argument("--manifest", type=Path, required=True, help="Path to manifest.json")
    ap.add_argument("--apply", action="store_true", help="Merge into manifest (dedupe by id)")
    args = ap.parse_args()

    bases = [b.strip() for b in args.bases.split(",") if b.strip()]
    raw = args.manifest.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        raise SystemExit("manifest.samples must be a list")

    new_chunks: list[dict[str, Any]] = []
    for i, bid in enumerate(bases):
        base = _find_base(manifest, bid)
        if not base:
            print(f"skip: base id not found: {bid}", file=sys.stderr)
            continue
        new_chunks.extend(build_rows(base, i))

    if not args.apply:
        print(json.dumps(new_chunks, indent=2, ensure_ascii=False))
        return 0

    seen = {str(s.get("id")) for s in samples if isinstance(s, dict) and s.get("id")}
    merged = list(samples)
    for row in new_chunks:
        rid = str(row["id"])
        if rid in seen:
            continue
        merged.append(row)
        seen.add(rid)
    manifest["samples"] = merged
    args.manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.manifest} (+{len(merged) - len(samples)} samples)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
