"""
全自動バックフィル
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import BASE_DIR
from scraper.bulk_collect import COURSE_CODES, collect_year, _git_commit_and_push

STATE_FILE = BASE_DIR / "data" / "backfill_state.json"
BACKFILL_YEARS = 10


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"completed": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def all_targets() -> list:
    current_year = datetime.datetime.now().year
    years = list(range(current_year, current_year - BACKFILL_YEARS - 1, -1))
    targets = []
    for year in years:
        for course_code in COURSE_CODES.keys():
            targets.append((year, course_code))
    return targets


def next_target(state: dict):
    completed = set(state.get("completed", []))
    for year, course_code in all_targets():
        key = f"{year}-{course_code}"
        if key not in completed:
            return year, course_code
    return None


def print_status(state: dict):
    completed = set(state.get("completed", []))
    targets = all_targets()
    done = len(completed)
    total = len(targets)
    print(f"進捗: {done} / {total} (年×競馬場の組み合わせ)")
    nxt = next_target(state)
    if nxt:
        year, code = nxt
        print(f"次に収集するのは: {year}年 {COURSE_CODES[code]}")
    else:
        print("全ての対象年・競馬場の収集が完了しています！")


def run_next():
    state = load_state()
    nxt = next_target(state)
    if nxt is None:
        print("収集対象がすべて完了しています。何もしません。")
        return

    year, course_code = nxt
    course_name = COURSE_CODES[course_code]
    print(f"=== {year}年 {course_name} の収集を開始 ===")

    collect_year(year, courses=[course_code], commit_per_course=True)

    key = f"{year}-{course_code}"
    state.setdefault("completed", []).append(key)
    save_state(state)
    _git_commit_and_push(f"auto-backfill: {year}年 {course_name} 完了 ({len(state['completed'])}/{len(all_targets())})")

    print(f"=== {year}年 {course_name} 完了 ===")


def main():
    parser = argparse.ArgumentParser(description="全自動バックフィル")
    parser.add_argument("--status", action="store_true", help="進捗状況の確認のみ")
    args = parser.parse_args()

    state = load_state()
    if args.status:
        print_status(state)
    else:
        run_next()


if __name__ == "__main__":
    main()