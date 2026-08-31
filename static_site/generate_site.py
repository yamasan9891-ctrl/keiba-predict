"""
静的サイト生成

DB（や当日取得した出馬表・予想結果）から、
  static_site/dist/index.html          ... 今週のレース一覧
  static_site/dist/races/<race_id>.html ... 各レースの予想ページ
を生成する。GitHub Actionsから週次で呼び出す想定。

このスクリプト自体はデータ取得を行わない。事前に
  1. scraper でその週のレースをDBに取り込む
  2. model/predict.py 等で各馬の複勝確率(place_probability)を計算する
  3. betting/ev_engine.py でEVテーブルを作る
までを済ませたうえで、集約済みのデータ構造を渡して使う想定。
（実データ連携のオーケストレーションは weekly_pipeline.py で行う）
"""
import datetime as dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent / "templates"
DIST_DIR = Path(__file__).parent / "dist"

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


def generate_index(days: list[dict]) -> Path:
    """
    days: [
      {"date": "2026-08-15", "weekday": "土", "races": [
          {"race_id":..., "course":..., "race_number":..., "race_name":...,
           "surface":..., "distance":..., "track_condition":..., "n_horses":...,
           "is_win5_race": bool, "is_handicap": bool},
          ...
      ]},
      ...
    ]
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    template = _env.get_template("index.html")
    html = template.render(days=days, generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    out_path = DIST_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def generate_race_page(
    race: dict,
    horses: list[dict],
    ev_tables: dict,
    best_bet: dict | None,
    race_summary_text: str,
    win5: dict | None = None,
    dark_horses: list | None = None,
    betting_plan: list | None = None,
) -> Path:
    """
    race: {race_id, course, race_number, race_name, surface, distance, track_condition, weather, is_handicap}
    horses: [{horse_number, name, place_probability, reason}, ...]
    ev_tables: betting.ev_engine.build_ev_table() の戻り値（EV>1.0の行のみに絞ったもの推奨）
    best_bet: betting.ev_engine.best_bet() の戻り値
    win5: {"race_labels":[...], "combos":[...]} または None
    dark_horses: betting.ev_engine.identify_value_horses() の戻り値 または None
    betting_plan: betting.ev_engine.build_betting_plan() の戻り値（複数点の購入プラン） または None
    """
    races_dir = DIST_DIR / "races"
    races_dir.mkdir(parents=True, exist_ok=True)
    template = _env.get_template("race.html")
    html = template.render(
        race=race, horses=horses, ev_tables=ev_tables,
        best_bet=best_bet, race_summary_text=race_summary_text, win5=win5,
        dark_horses=dark_horses, betting_plan=betting_plan,
    )
    out_path = races_dir / f"{race['race_id']}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def generate_archive_page(races: list) -> Path:
    """
    過去に予想ページを生成した全レースの一覧（archive.html）を生成する。
    races: db.database.list_archived_races() の戻り値
    race_dateごとにグルーピングして表示する。
    """
    grouped = {}
    for r in races:
        date_key = r.get("race_date") or r["race_id"][:4]
        grouped.setdefault(date_key, []).append(r)
    days = [{"date": d, "races": races_in_day} for d, races_in_day in sorted(grouped.items(), reverse=True)]

    template = _env.get_template("archive.html")
    html = template.render(days=days, generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    out_path = DIST_DIR / "archive.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def generate_performance_page(resolved_bets: list, pending_count: int) -> Path:
    """
    収支ページ（static_site/dist/performance.html）を生成する。
    resolved_bets: db.database.all_resolved_bets() の戻り値
    """
    total_staked = sum(b["stake"] for b in resolved_bets)
    total_returned = sum(b["payout"] or 0 for b in resolved_bets)
    total_profit = total_returned - total_staked
    roi = (total_returned / total_staked * 100) if total_staked > 0 else None
    win_count = sum(1 for b in resolved_bets if b["won"])

    template = _env.get_template("performance.html")
    html = template.render(
        bets=resolved_bets,
        total_staked=total_staked,
        total_returned=total_returned,
        total_profit=total_profit,
        roi=roi,
        win_count=win_count,
        total_count=len(resolved_bets),
        pending_count=pending_count,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    out_path = DIST_DIR / "performance.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def generate_win5_page(win5: dict, races: list) -> Path:
    """
    WIN5専用の独立ページを生成する（static_site/dist/win5.html）。
    win5: {"race_labels":[...], "combos":[...]}
    races: WIN5対象5レースの簡易情報 [{race_id, course, race_number, race_name}, ...]
    """
    template = _env.get_template("win5.html")
    html = template.render(win5=win5, races=races, generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    out_path = DIST_DIR / "win5.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
