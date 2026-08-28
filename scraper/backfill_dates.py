"""
開催日 後埋めスクリプト

race_dateの抽出ロジックを追加する前に収集していたレースは、race_dateが空のままになっている。
これらを対象に、レース情報ページだけを再取得して開催日・競馬場名を埋める。

使い方:
  python scraper/backfill_dates.py --limit 300
"""
import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db, upsert_race
from scraper.netkeiba_scraper import fetch_race_meta
from scraper.bulk_collect import _git_commit_and_push


def races_missing_date(conn, limit: int) -> list:
    rows = conn.execute(
        "SELECT race_id FROM races WHERE race_date IS NULL ORDER BY RANDOM() LIMIT ?", (limit,)
    ).fetchall()
    return [r["race_id"] for r in rows]


def backfill_dates(limit: int = 300, commit_every: int = 100):
    init_db()
    with get_conn() as conn:
        target_ids = races_missing_date(conn, limit)

    print(f"対象: {len(target_ids)}件（開催日が未取得のレース）")

    attempted = 0
    date_found = 0
    for i, race_id in enumerate(target_ids, 1):
        attempted += 1
        try:
            meta = fetch_race_meta(race_id)
            with get_conn() as conn:
                upsert_race(conn, meta)
            if meta.get("race_date"):
                date_found += 1
            else:
                print(f"  [注意] {race_id} は開催日を抽出できませんでした")
            if i % 20 == 0:
                print(f"  ({i}/{len(target_ids)}) 処理中... 直近: {race_id} -> {meta.get('race_date')}")
        except Exception as e:
            print(f"  [警告] {race_id} 取得失敗: {e}")

        if date_found > 0 and date_found % commit_every == 0:
            _git_commit_and_push(f"backfill dates: {date_found}件の開催日を取得済み（累計、試行{attempted}件）")

    if date_found > 0:
        _git_commit_and_push(f"backfill dates: 最終 {date_found}件（試行{attempted}件）")

    print(f"完了: 試行{attempted}件中、{date_found}件の開催日を取得しました")


def main():
    parser = argparse.ArgumentParser(description="開催日 後埋め")
    parser.add_argument("--limit", type=int, default=300, help="1回の実行で処理する件数の上限")
    args = parser.parse_args()
    backfill_dates(limit=args.limit)


if __name__ == "__main__":
    main()
