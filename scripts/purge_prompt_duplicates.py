"""One-shot cleanup for the prompt template storage (#231).

Before the fix, every server startup wrote new UUID-keyed JSON copies of
the 16 YAML seed templates into ``collegue/prompts/templates/templates/``.
The audit found 2112 files on disk — 132 copies of each of the 16 unique
template names, all with identical content and all with ``performance_score
= 0.0`` / ``usage_count = 0``.

This script consolidates the storage :

1. Group on-disk JSONs by ``name``.
2. For each name, keep **one canonical template** — preferring the entry
   with a non-zero ``usage_count`` (if any), otherwise the one most recently
   updated.
3. Delete the duplicates.
4. Rebuild ``collegue/prompts/versions/versions.json`` so it only references
   template IDs that still exist on disk, and dedupe identical-content
   versions within each template.

Usage::

    python scripts/purge_prompt_duplicates.py --dry-run   # show plan only
    python scripts/purge_prompt_duplicates.py             # apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "collegue" / "prompts" / "templates" / "templates"
VERSIONS_FILE = REPO_ROOT / "collegue" / "prompts" / "versions" / "versions.json"


def _rank_entry(entry: Tuple[Path, Dict]) -> Tuple[int, str]:
    """Higher-rank entry wins the keeper slot.

    Priority : non-zero ``usage_count`` > most recent ``updated_at`` >
    most recent ``created_at``.
    """
    _, data = entry
    usage = data.get("usage_count") or 0
    stamp = data.get("updated_at") or data.get("created_at") or ""
    return (usage, stamp)


def _plan_template_purge(templates_dir: Path) -> Tuple[Dict[str, str], List[Path]]:
    """Group templates by name, decide which UUID survives per name.

    Returns (keep_by_name, to_delete_paths).
    """
    by_name: Dict[str, List[Tuple[Path, Dict]]] = defaultdict(list)
    for json_file in templates_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] cannot parse {json_file.name}: {exc}", file=sys.stderr)
            continue
        by_name[data.get("name", "<unknown>")].append((json_file, data))

    keep_by_name: Dict[str, str] = {}
    to_delete: List[Path] = []
    for name, entries in by_name.items():
        entries.sort(key=_rank_entry, reverse=True)
        winner_path, winner_data = entries[0]
        keep_by_name[name] = winner_data["id"]
        for loser_path, _ in entries[1:]:
            to_delete.append(loser_path)

    return keep_by_name, to_delete


def _rebuild_versions(kept_ids: set, versions_file: Path) -> Tuple[int, int]:
    """Drop orphaned template_ids from versions.json, dedupe by content.

    Returns (count_before, count_after).
    """
    if not versions_file.exists():
        return (0, 0)

    versions = json.loads(versions_file.read_text(encoding="utf-8"))
    total_before = sum(len(v) for v in versions.values())

    cleaned: Dict[str, List[Dict]] = {}
    for tid, vs in versions.items():
        if tid not in kept_ids:
            continue
        seen_contents: set = set()
        unique_vs: List[Dict] = []
        for v in vs:
            content = v.get("content", "")
            if content in seen_contents:
                continue
            seen_contents.add(content)
            unique_vs.append(v)
        cleaned[tid] = unique_vs

    versions_file.write_text(
        json.dumps(cleaned, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    total_after = sum(len(v) for v in cleaned.values())
    return total_before, total_after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without touching the filesystem",
    )
    args = ap.parse_args()

    if not TEMPLATES_DIR.is_dir():
        print(f"[ERROR] {TEMPLATES_DIR} does not exist (run from repo root)", file=sys.stderr)
        return 2

    total_before = len(list(TEMPLATES_DIR.glob("*.json")))
    keep_by_name, to_delete = _plan_template_purge(TEMPLATES_DIR)

    print(f"Templates on disk: {total_before}")
    print(f"Unique names:      {len(keep_by_name)}")
    print(f"Duplicates to drop: {len(to_delete)}")

    if args.dry_run:
        print("\n[DRY RUN] no files deleted.")
        print("Top 5 names by duplicate count would be trimmed to 1 each.")
        return 0

    for path in to_delete:
        path.unlink()
    total_after = len(list(TEMPLATES_DIR.glob("*.json")))
    print(f"\nDeleted {len(to_delete)} duplicate JSONs.")
    print(f"Templates on disk: {total_before} → {total_after}")

    kept_ids = set(keep_by_name.values())
    v_before, v_after = _rebuild_versions(kept_ids, VERSIONS_FILE)
    print(f"versions.json: {v_before} → {v_after} entries (orphans + content dupes removed)")

    print("\nDone. Restart the MCP server — the refactored `_load_tool_templates` will now")
    print("recognise the canonical templates and skip creating duplicates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
