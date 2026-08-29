"""
血統スクレイパー（検証済み版）

血統情報は db.netkeiba.com/horse/ped/<horse_id>/ の「5代血統表」
（class="blood_table"のtable、rowspanでセル結合された木構造）に格納されている。
rowspanを正しく解釈してグリッド化し、決まった位置（父=行0列0、母=行16列0、
父父=行0列1、父母=行8列1、母父=行16列1、母母=行24列1）から抜き出す。

※このロジックは実際のnetkeibaページ（イクイノックス:2019105219）で
  実データと一致することを確認済み。
"""
import argparse
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db, upsert_horse, distinct_horse_ids_without_pedigree
from scraper.netkeiba_scraper import _polite_get
from scraper.bulk_collect import _git_commit_and_push

# 5代血統表グリッド上の(行, 列)座標
CELL_POSITIONS = {
    "father": (0, 0),
    "mother": (16, 0),
    "father_father": (0, 1),
    "father_mother": (8, 1),
    "mother_father": (16, 1),
    "mother_mother": (24, 1),
}


_INVALID_NAMES = {"血統", "産駒", ""}


def _cell_name(td) -> str:
    """td内のリンクから馬名テキストだけを取り出す（改行や英語表記部分は除く）"""
    if td is None:
        return None
    a = td.find("a")
    if a is None:
        return None
    first_text = a.find(string=True)
    name = first_text.strip() if first_text else None
    # 「血統」「産駒」タブへのリンク文字を誤って拾ってしまうケースへのガード
    if name in _INVALID_NAMES:
        return None
    return name


def _parse_blood_table(table) -> dict:
    """rowspanを解釈してtableをグリッド化し、決まった座標から血統情報を抜き出す"""
    grid = {}
    rowspans = {}  # col_idx -> (残り行数, td)
    row_idx = 0

    for tr in table.find_all("tr"):
        col_idx = 0
        tds = iter(tr.find_all("td"))
        while True:
            while col_idx in rowspans and rowspans[col_idx][0] > 0:
                grid[(row_idx, col_idx)] = rowspans[col_idx][1]
                remaining, td_ref = rowspans[col_idx]
                rowspans[col_idx] = (remaining - 1, td_ref)
                if rowspans[col_idx][0] == 0:
                    del rowspans[col_idx]
                col_idx += 1
            td = next(tds, None)
            if td is None:
                break
            span = int(td.get("rowspan", 1))
            grid[(row_idx, col_idx)] = td
            if span > 1:
                rowspans[col_idx] = (span - 1, td)
            col_idx += 1
        row_idx += 1

    return {label: _cell_name(grid.get(pos)) for label, pos in CELL_POSITIONS.items()}


def fetch_pedigree(horse_id: str) -> dict:
    """馬の血統情報を専用ページ(/horse/ped/<id>/)から取得する"""
    url = f"https://db.netkeiba.com/horse/ped/{horse_id}/"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "html.parser")

    horse_name = None
    name_el = soup.find("div", class_="db_head_name")
    if name_el:
        h1 = name_el.find("h1")
        horse_name = h1.get_text(strip=True) if h1 else None

    values = {label: None for label in CELL_POSITIONS}
    table = soup.find("table", class_="blood_table")
    if table is not None:
        try:
            values = _parse_blood_table(table)
        except Exception:
            pass  # 構造が想定と異なる場合は空のまま（次回再試行対象になる）

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
    print(f"収集対象: {len(target_ids)}頭（未収集/取得失敗の馬から最大{limit}頭）")

    collected = 0
    for i, horse_id in enumerate(target_ids, 1):
        try:
            data = fetch_pedigree(horse_id)
            with get_conn() as conn:
                upsert_horse(conn, data)
            if data.get("father"):
                collected += 1
            status = "✓" if data.get("father") else "△(空)"
            print(f"  {status} ({i}/{len(target_ids)}) {horse_id}: {data.get('horse_name')} "
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
        with_pedigree = conn.execute("SELECT COUNT(*) FROM horses WHERE father IS NOT NULL").fetchone()[0]
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
