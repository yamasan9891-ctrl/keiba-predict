"""
過去レース一括収集（総当たり方式）
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db, race_exists
from scraper.netkeiba_scraper import fetch_race_result, save_to_db

COURSE_CODES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}


def collect_year(year: int, courses: list = None, max_kai: int = 6,
                  max_day: int = 12, max_race: int = 12,
                  max_consecutive_misses: int = 30) -> dict:
    init_db()
    courses = courses or list(COURSE_CODES.keys())
    stats = {"success": 0, "skipped_existing": 0, "not_found": 0}

    for course_code in courses:
        course_name = COURSE_CODES.get(course_code, course_code)
        for kai in range(1, max_kai + 1):
            consecutive_misses = 0
            for day in range(1, max_day + 1):
                if consecutive_misses >= max_consecutive_misses:
                    print(f"  [{course_name} {kai}回] 連続未検出のためこの開催回を終了")
                    break
                day_found_any = False
                for race_num in range(1, max_race + 1):
                    race_id = f"{year}{course_code}{kai:02d}{day:02d}{race_num:02d}"
                    with get_conn() as conn:
                        if race_exists(conn, race_id):
                            stats["skipped_existing"] += 1
                            continue
                    try:
                        df = fetch_race_result(race_id)
                        save_to_db(df)
                        stats["success"] += 1
                        day_found_any = True
                        consecutive_misses = 0
                        print(f"  ✓ {race_id} ({course_name}) 収集成功")
                    except Exception:
                        stats["not_found"] += 1
                if not day_found_any:
                    consecutive_misses += 1
    return stats


def main():
    parser = argparse.ArgumentParser(description="過去レース一括収集")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--courses", type=str, default=None)
    args = parser.parse_args()
    courses = args.courses.split(",") if args.courses else None

    print(f"=== {args.year}年のレース収集を開始 ===")
    start = time.time()
    stats = collect_year(args.year, courses)
    elapsed = (time.time() - start) / 60
    print(f"=== 完了 ({elapsed:.1f}分) ===")
    print(f"新規収集: {stats['success']}件 / 既存スキップ: {stats['skipped_existing']}件 / 未検出: {stats['not_found']}件")


if __name__ == "__main__":
    main()
