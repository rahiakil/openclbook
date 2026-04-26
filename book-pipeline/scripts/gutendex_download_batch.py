#!/usr/bin/env python3
"""
Download ~N public-domain plain-text books via Gutendex (https://gutendex.com/), strip PG boilerplate.

Usage (from repo root):
  python scripts/gutendex_download_batch.py --target 40 --out gutenberg_library

Re-runs are incremental: existing ``manifest.json`` plus files under ``raw/`` and ``clean/`` are
reused; only missing titles are selected and downloaded until ``--target`` total clean books exist.
Use ``--force`` to re-download and overwrite.

Respects remote hosts: sleeps between Gutendex and Gutenberg requests.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

# Repo root = parent of scripts/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from book_pipeline.gutenberg import strip_project_gutenberg_boilerplate  # noqa: E402

# Trailing slash avoids 301 from /books → /books/ (see https://gutendex.com/ docs).
GUTENDEX_BASE = "https://gutendex.com/"
DEFAULT_LANGS = [
    "en",
    "fr",
    "de",
    "es",
    "pt",
    "it",
    "fi",
    "nl",
    "ru",
    "pl",
    "hu",
    "el",
    "la",
    "eo",
    "zh",
]


def slugify(s: str, max_len: int = 48) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:max_len] or "book").strip("-") or "book"


def pick_plaintext_url(formats: dict) -> str | None:
    if not formats:
        return None
    best: tuple[int, str] | None = None
    for mime, url in formats.items():
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        m = mime.lower()
        if not m.startswith("text/plain"):
            continue
        # Prefer UTF-8 / ascii charset hints
        prio = 0
        if "utf-8" in m or "utf8" in m:
            prio = 3
        elif "ascii" in m:
            prio = 2
        else:
            prio = 1
        if best is None or prio > best[0]:
            best = (prio, url)
    return best[1] if best else None


def fetch_json(client: httpx.Client, url: str) -> dict:
    r = client.get(url, timeout=60.0)
    r.raise_for_status()
    return r.json()


def download_text(client: httpx.Client, url: str) -> str:
    r = client.get(url, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    return r.text


def load_complete_books(out: Path) -> dict[int, dict]:
    """
    Books that already have non-empty raw + clean files on disk (paths from manifest or default layout).
    """
    mp = out / "manifest.json"
    if not mp.is_file():
        return {}
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    prior: dict[int, dict] = {}
    for b in data.get("books") or []:
        if not isinstance(b, dict):
            continue
        bid = int(b.get("id") or 0)
        if not bid:
            continue
        paths = b.get("paths") or {}
        raw_rel = paths.get("raw")
        clean_rel = paths.get("clean")
        raw_p = (out / raw_rel).resolve() if raw_rel else (out / "raw" / f"{bid}.txt").resolve()
        clean_p = (out / clean_rel).resolve() if clean_rel else None
        if clean_p is None or not clean_p.is_file():
            matches = sorted((out / "clean").glob(f"{bid}__*.txt"))
            clean_p = matches[0].resolve() if matches else None
        if not raw_p.is_file() or clean_p is None or not clean_p.is_file():
            continue
        if clean_p.stat().st_size == 0:
            continue
        b = dict(b)
        b["paths"] = {
            "raw": str(raw_p.relative_to(out)),
            "clean": str(clean_p.relative_to(out)),
        }
        if "clean_chars" not in b:
            try:
                b["clean_chars"] = len(clean_p.read_text(encoding="utf-8"))
            except OSError:
                b["clean_chars"] = 0
        prior[bid] = b
    return prior


def main() -> int:
    ap = argparse.ArgumentParser(description="Download public-domain texts via Gutendex + strip PG boilerplate")
    ap.add_argument("--target", type=int, default=40, help="How many distinct books to collect")
    ap.add_argument("--out", type=Path, default=ROOT / "gutenberg_library", help="Output directory")
    ap.add_argument("--min-downloads", type=int, default=800, help="Minimum Gutendex download_count (popularity proxy)")
    ap.add_argument("--sleep-gutendex", type=float, default=0.8, help="Seconds between Gutendex list requests")
    ap.add_argument("--sleep-gutenberg", type=float, default=2.0, help="Seconds between Gutenberg text downloads")
    ap.add_argument(
        "--force",
        action="store_true",
        help="For titles selected in this run, re-download and overwrite raw/clean even if files exist",
    )
    ap.add_argument(
        "--languages",
        type=str,
        default=",".join(DEFAULT_LANGS),
        help="Comma-separated two-letter Gutendex language codes to rotate through",
    )
    args = ap.parse_args()
    out: Path = args.out.expanduser().resolve()
    raw_dir = out / "raw"
    clean_dir = out / "clean"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clean_dir.mkdir(parents=True, exist_ok=True)

    langs = [x.strip().lower() for x in args.languages.split(",") if x.strip()]
    if not langs:
        langs = DEFAULT_LANGS

    complete = load_complete_books(out)
    manifest_by_id: dict[int, dict] = dict(complete)
    need_more = max(0, args.target - len(manifest_by_id))
    if need_more == 0:
        manifest_books = sorted(manifest_by_id.values(), key=lambda x: int(x["id"]))
        manifest = {
            "gutendex_base": GUTENDEX_BASE.rstrip("/"),
            "count": len(manifest_books),
            "books": manifest_books,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(
            f"already have {len(manifest_books)} book(s) (target {args.target}); wrote {out / 'manifest.json'}",
            flush=True,
        )
        return 0

    headers = {"User-Agent": "book-pipeline-gutendex-batch/1.0 (educational; respects robots)"}
    collected: dict[int, dict] = {}

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for lang in langs:
            if len(collected) >= need_more:
                break
            url = (
                f"{GUTENDEX_BASE}books?"
                f"copyright=false&mime_type=text&languages={lang}&sort=popular"
            )
            while url and len(collected) < need_more:
                print(f"gutendex: GET {url}", flush=True)
                data = fetch_json(client, url)
                time.sleep(max(0.0, args.sleep_gutendex))
                for book in data.get("results") or []:
                    if len(collected) >= need_more:
                        break
                    bid = int(book.get("id") or 0)
                    if not bid or bid in collected or bid in manifest_by_id:
                        continue
                    dc = int(book.get("download_count") or 0)
                    if dc < args.min_downloads:
                        continue
                    src = pick_plaintext_url(book.get("formats") or {})
                    if not src:
                        continue
                    title = (book.get("title") or "").strip() or f"Book {bid}"
                    authors = book.get("authors") or []
                    anames = ", ".join(
                        (a.get("name") or "").strip() for a in authors if isinstance(a, dict)
                    )
                    collected[bid] = {
                        "id": bid,
                        "title": title,
                        "authors": anames,
                        "languages": book.get("languages") or [],
                        "download_count": dc,
                        "copyright": book.get("copyright"),
                        "source_url": src,
                        "gutendex_url": urljoin(GUTENDEX_BASE, f"books/{bid}/"),
                        "language_query": lang,
                    }
                url = data.get("next")
                if url and not url.startswith("http"):
                    url = urljoin(GUTENDEX_BASE, url)

        print(f"selected {len(collected)} new book(s) to download (have {len(manifest_by_id)}, target {args.target})…", flush=True)
        for bid in sorted(collected.keys()):
            meta = collected[bid]
            src = meta["source_url"]
            slug = slugify(meta["title"])
            raw_path = raw_dir / f"{bid}.txt"
            clean_path = clean_dir / f"{bid}__{slug}.txt"
            if (
                not args.force
                and raw_path.is_file()
                and clean_path.is_file()
                and clean_path.stat().st_size > 0
            ):
                print(f"gutenberg: id={bid} {meta['title'][:60]!r} (skip, already on disk)", flush=True)
                clean_body = clean_path.read_text(encoding="utf-8", errors="replace")
                meta["paths"] = {
                    "raw": str(raw_path.relative_to(out)),
                    "clean": str(clean_path.relative_to(out)),
                }
                meta["clean_chars"] = len(clean_body)
                manifest_by_id[bid] = meta
                continue

            print(f"gutenberg: id={bid} {meta['title'][:60]!r}", flush=True)
            try:
                raw_text = download_text(client, src)
            except Exception as e:  # noqa: BLE001
                print(f"  skip download error: {e}", flush=True)
                continue
            time.sleep(max(0.0, args.sleep_gutenberg))
            raw_path.write_text(raw_text, encoding="utf-8", errors="replace")
            clean_body = strip_project_gutenberg_boilerplate(raw_text)
            clean_path.write_text(
                clean_body + ("\n" if clean_body and not clean_body.endswith("\n") else "\n"),
                encoding="utf-8",
            )
            meta["paths"] = {
                "raw": str(raw_path.relative_to(out)),
                "clean": str(clean_path.relative_to(out)),
            }
            meta["clean_chars"] = len(clean_body)
            manifest_by_id[bid] = meta

    manifest_books = sorted(manifest_by_id.values(), key=lambda x: int(x["id"]))
    manifest = {
        "gutendex_base": GUTENDEX_BASE.rstrip("/"),
        "count": len(manifest_books),
        "books": manifest_books,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {out / 'manifest.json'} with {len(manifest_books)} clean text(s)", flush=True)
    if len(manifest_books) < args.target:
        print(
            f"warning: only {len(manifest_books)}/{args.target} books after this run "
            f"(Gutendex filters or network may limit results; try lowering --min-downloads or more --languages)",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
