#!/usr/bin/env python3
"""清掉已入库 / 已归档包的缩略件，控住公网磁盘。"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import db  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="清理 accepted/archived 包的缩略件")
    p.add_argument("--db", default=None)
    p.add_argument("--thumbs", default=None)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    c = db.connect(a.db)
    db.migrate(c)
    root = Path(a.thumbs or (Path(a.db).parent / "thumbs" if a.db else Path(db.default_db_path()).parent / "thumbs"))
    rows = db.rows(c, "SELECT packet_id, state FROM packets WHERE state IN ('accepted','archived')")
    n = 0
    for r in rows:
        d = root / r["packet_id"]
        if not d.is_dir():
            continue
        print(f"{r['state']} {r['packet_id']}  {d}")
        if not a.dry_run:
            shutil.rmtree(d, ignore_errors=True)
            c.execute("UPDATE packets SET thumb_ready=0, updated_at=? WHERE packet_id=?",
                      (db.now(), r["packet_id"]))
        n += 1
    if not a.dry_run:
        c.commit()
    print(f"{'将清' if a.dry_run else '已清'} {n} 个目录")


if __name__ == "__main__":
    main()
