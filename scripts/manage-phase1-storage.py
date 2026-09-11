#!/usr/bin/env python3
"""Delete an explicit derived-file plan, or inventory protected capture. Never auto-prune."""

import argparse
import json
import os
from pathlib import Path
from phase1_storage import GIB, identity, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory-raw", help="hash protected capture files; no mutations")
    inventory.add_argument("--root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    delete = commands.add_parser("delete-derived", help="delete only the exact verified derived-file plan")
    delete.add_argument("--plan", type=Path, required=True)
    delete.add_argument("--journal", type=Path, required=True)
    delete.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "delete-derived":
        plan = json.loads(args.plan.read_text())
        if plan.get("version") != "phase1-derived-deletion-v1":
            raise ValueError("unsupported deletion plan")
        root = Path(plan["root"]).resolve(strict=True)
        paths = set()
        # Validate the whole exact list before deleting its first file.
        for row in plan["files"]:
            path = Path(row["path"])
            if (path in paths or path.resolve(strict=True) != path or not path.is_relative_to(root)
                    or not path.is_file() or path.name not in {"examples.jsonl", "packed-examples.jsonl", "examples.jsonl.gz", "packed-examples.jsonl.gz"}
                    or (path.parent / "raw.jsonl").exists() or (path.parent / "session.json").exists()
                    or [str(v) for v in identity(path)] != row["identity"]):
                raise ValueError(f"unsafe or changed deletion target: {path}")
            paths.add(path)
        if not args.apply:
            print(f"Validated {len(paths)} explicit derived files, {sum(int(r['identity'][2]) for r in plan['files']) / GIB:.3f} GiB")
            return
        with args.journal.open("x") as journal:
            for row in plan["files"]:
                path = Path(row["path"])
                digest = sha256(path)
                if [str(v) for v in identity(path)] != row["identity"]:
                    raise ValueError(f"target changed while hashing: {path}")
                record = {**row, "sha256": digest, "status": "verified_before_deletion"}
                print(json.dumps(record, sort_keys=True), file=journal, flush=True)
                os.fsync(journal.fileno())
                path.unlink()
                print(json.dumps({"path": str(path), "status": "deleted"}), file=journal, flush=True)
                os.fsync(journal.fileno())
                print(f"Deleted derived file ({int(row['identity'][2]) / GIB:.3f} GiB): {path}", flush=True)
        return
    if args.command == "inventory-raw":
        count = 0
        total = 0
        with args.output.open("x") as handle:
            for path in sorted(args.root.resolve().rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                if path.name not in {"raw.jsonl", "session.json"} and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".heic", ".wav", ".mp4"}:
                    continue
                size = path.stat().st_size
                print(json.dumps({"path": str(path), "bytes": size, "sha256": sha256(path)}, sort_keys=True), file=handle)
                count += 1
                total += size
        print(f"Hashed {count} protected capture files ({total / GIB:.3f} GiB)")
        return


if __name__ == "__main__":
    main()
