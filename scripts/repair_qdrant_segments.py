"""Script to quarantine qdrant segments that are newer than the last applied op."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
import sys


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object in %s" % path)
    return data


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect_segments(segments_dir: Path, applied_seq: int) -> list[tuple[Path, int]]:
    if not segments_dir.exists():
        return []
    candidates: list[tuple[Path, int]] = []
    for child in sorted(segments_dir.iterdir()):
        if not child.is_dir():
            continue
        seg_json = child / "segment.json"
        if not seg_json.exists():
            continue
        metadata = _load_json(seg_json)
        version = metadata.get("version")
        if version is None:
            continue
        try:
            version = int(version)
        except (TypeError, ValueError):
            continue
        if version > applied_seq:
            candidates.append((child, version))
    return candidates


def move_segments(
    segments: list[tuple[Path, int]],
    target_root: Path,
    apply: bool,
) -> None:
    if not segments:
        return
    target_root.mkdir(parents=True, exist_ok=True)
    for segment_path, version in segments:
        dest = target_root / segment_path.name
        print(f"  - moving {segment_path} (version {version}) → {dest}")
        if apply:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(segment_path), str(dest))


def update_first_index(wal_dir: Path, applied_seq: int, apply: bool) -> None:
    first_index_path = wal_dir / "first-index"
    if not first_index_path.exists():
        print("first-index file is missing; skipping wal ack fix")
        return
    data = _load_json(first_index_path)
    prev = data.get("ack_index")
    if prev is None or prev == applied_seq:
        print(f"first-index already at {prev}; no change needed")
        return
    print(f"updating first-index ack_index: {prev} → {applied_seq}")
    if apply:
        data["ack_index"] = applied_seq
        _write_json(first_index_path, data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--collection",
        type=Path,
        default=Path("qdrant_data") / "collections" / "agent_memories",
        help="Path to the collection directory",
    )
    parser.add_argument(
        "--dest-root",
        type=Path,
        default=None,
        help="Where to move quarantined segments (defaults to collection/corrupted-segments/{timestamp})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files; default is dry run",
    )

    args = parser.parse_args()

    collection = args.collection
    if not collection.exists():
        print(f"Collection path {collection} does not exist", file=sys.stderr)
        return 1

    seq_path = collection / "0" / "applied_seq.json"
    if not seq_path.exists():
        print(f"No applied_seq.json at {seq_path}; aborting", file=sys.stderr)
        return 1

    applied_data = _load_json(seq_path)
    applied_seq = int(applied_data.get("op_num", 0))
    print(f"last applied op_num = {applied_seq}")

    segments_dir = collection / "0" / "segments"
    corrupted = collect_segments(segments_dir, applied_seq)
    if not corrupted:
        print("no segments newer than the applied sequence were found")
        return 0

    print(f"segments newer than {applied_seq}:")
    for path, version in corrupted:
        print(f"  {path.name} (version {version})")

    dest_root = args.dest_root
    if dest_root is None:
        now = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        dest_root = collection / "corrupted_segments" / f"after-{applied_seq}-{now}"

    if not args.apply:
        print("\nDry run: set --apply to move the above directories and rewrite first-index.")
        return 0

    move_segments(corrupted, dest_root, apply=True)

    wal_dir = collection / "0" / "wal"
    if wal_dir.exists():
        update_first_index(wal_dir, applied_seq, apply=True)

    print("\nQuarantined segments moved to:", dest_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
