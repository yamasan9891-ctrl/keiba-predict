"""
血統スクレイパー
"""
import argparse
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import REQUEST_HEADERS, SCRAPE_INTERVAL_SEC
from db.database import get_conn, init_db, upsert_horse, distinct_horse_ids_without_pedigree
from scraper.netkeiba_scraper import _polite_get
from scraper.bulk_collect import _git_commit_and_push


def fetch_pedigree(horse_id: str) -> dict:
    url = f"https://db.netkeiba.com/horse/{horse_id}"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "html.parser")

    horse_name = None
    name_el = soup.find("div", class_="db_head_name")
    if name_el:
        h1 = name_el.find("h1")
        horse_name = h1.get_text(strip=True) if h1 else None

    ped_el = soup.find("dd", class_="DB_ProfHead_dd_01") or soup.find("table", class_="blood_table")
    labels = ["father", "father_father", "father_mother", "mother", "mother_father", "mother_mother"]
    values = {label: None for label in labels}

    if ped_el is not None:
        links = [a.get_text(strip=True) for a in ped_el.find_all("a")]
        for label, value in zip(labels, links):
            values[label] = value or None

    return {
        "horse_id": horse_id,
        "horse_name": horse_name,
        **values,
    }


def collect_pedigrees(limit: int = 200, commit_every: int = 50):
    init_db()
    with get_conn() as conn:
        target_ids = distinct_horse_ids_without_pedigree(conn)

    target_ids = target_ids[:limit]
    print(f"収集対象: {len(target_ids)}頭（未収集の馬から最大{limit}頭）")

    collected = 0
    for i, horse_id in enumerate(target_ids, 1):
        try:
            data = fetch_pedigree(horse_id)
            with get_conn() as conn:
                upsert_horse(conn, data)
            collected += 1
            print(f"  ✓ ({i}/{len(target_ids)}) {horse_id}: {data.get('horse_name')} "
                  f"父={data.get('father')} 母父={data.get('mother_father')}")
        except Exception as e:
            print(f"  [警告] {horse_id} 取得失敗: {e}")

        if collected > 0 and collected % commit_every == 0:
            _git_commit_and_push(f"pedigree: {collected}頭収集済み（累計）")

    if collected > 0:
        _git_commit_and_push(f"pedigree: 最終収集 {collected}頭")

    print(f"完了: {collected}頭の血統情報を新規取得しました")


def print_status():
    init_db()
    with get_conn() as conn:
        total_horses = conn.execute("SELECT COUNT(DISTINCT horse_id) FROM entries WHERE horse_id IS NOT NULL").fetchone()[0]
        with_pedigree = conn.execute("SELECT COUNT(*) FROM horses").fetchone()[0]
    print(f"entriesに登場する馬: {total_horses}頭 / うち血統取得済み: {with_pedigree}頭")


def main():
    parser = argparse.ArgumentParser(description="血統情報収集")
    parser.add_argument("--limit", type=int, default=200, help="1回の実行で収集する頭数の上限")
    parser.add_argument("--status", action="store_true", help="進捗確認のみ")
    args = parser.parse_args()

    if args.status:
        print_status()
    else:
        collect_pedigrees(limit=args.limit)


if __name__ == "__main__":
    main()