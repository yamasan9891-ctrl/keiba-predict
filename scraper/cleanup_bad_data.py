"""
ゴミデータ掃除スクリプト
"""
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db


def find_bad_race_ids(conn) -> list:
    rows = conn.execute("""
        SELECT race_id
        FROM entries
        GROUP BY race_id
        HAVING SUM(CASE WHEN horse_number IS NOT NULL THEN 1 ELSE 0 END) = 0
    """).fetchall()
    return [r["race_id"] for r in rows]


def cleanup(dry_run: bool = True):
    init_db()
    with get_conn() as conn:
        bad_ids = find_bad_race_ids(conn)
        print(f"ゴミデータと判定されたrace_id: {len(bad_ids)}件")
        if bad_ids[:10]:
            print("例:", bad_ids[:10])

        if dry_run:
            print("(--dry-run のため実際の削除はしていません)")
            return

        for rid in bad_ids:
            conn.execute("DELETE FROM entries WHERE race_id = ?", (rid,))
            conn.execute("DELETE FROM races WHERE race_id = ?", (rid,))
        print(f"削除完了: {len(bad_ids)}件のレースをDBから除去しました。次回収集時に再取得されます。")


def main():
    parser = argparse.ArgumentParser(description="ゴミデータ掃除")
    parser.add_argument("--dry-run", action="store_true", help="削除せず対象件数のみ確認")
    args = parser.parse_args()
    cleanup(dry_run=args.dry_run)


if __name__ == "__main__":
    main()