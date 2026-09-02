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


def generate_index(days: list[dict], next_week: dict = None, preview_races: list = None, top_featured: list = None) -> Path:
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
    next_week: scraper.netkeiba_scraper.fetch_next_week_preview() の戻り値
    preview_races: 枠順未発表で出走予定プレビューだけ作ったレースのリスト
    top_featured: 週の注目レースTOP3（見出しEVが高い順）
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    template = _env.get_template("index.html")
    html = template.render(
        days=days, next_week=next_week, preview_races=preview_races or [], top_featured=top_featured or [],
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    out_path = DIST_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def generate_preview_page(race_meta: dict, horses: list) -> Path:
    """
    枠順（正式な馬番）未発表レース向けの軽量プレビューページを生成する。
    フルの予想ページ(race.html)とは別の、シンプルな出走予定表のみのページ。
    """
    races_dir = DIST_DIR / "races"
    races_dir.mkdir(parents=True, exist_ok=True)
    template = _env.get_template("preview.html")
    html = template.render(race=race_meta, horses=horses, generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    out_path = races_dir / f"{race_meta['race_id']}.html"
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
    pace_diagram_svg: str | None = None,
    win_ev_ranking: list | None = None,
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
        dark_horses=dark_horses, betting_plan=betting_plan, pace_diagram_svg=pace_diagram_svg,
        win_ev_ranking=win_ev_ranking,
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


def _summarize_bets(bets: list) -> dict:
    total_staked = sum(b["stake"] for b in bets)
    total_returned = sum(b["payout"] or 0 for b in bets)
    total_profit = total_returned - total_staked
    roi = (total_returned / total_staked * 100) if total_staked > 0 else None
    win_count = sum(1 for b in bets if b["won"])
    return {
        "total_staked": total_staked, "total_returned": total_returned, "total_profit": total_profit,
        "roi": roi, "win_count": win_count, "total_count": len(bets),
    }


def generate_performance_page(
    bets_for_summary: list, bets_for_display: list, pending_count: int,
    favorite_summary_bets: list = None, favorite_display_bets: list = None, favorite_pending_count: int = 0,
) -> Path:
    """
    収支ページ（static_site/dist/performance.html）を生成する。
    bets_for_summary: 期待値重視戦略の集計用全件（例: 今年1年分）
    bets_for_display: 期待値重視戦略の一覧表示用（直近分のみ）
    favorite_*: 「堅い本命」戦略側の同様のデータ（比較用に併記する）
    """
    value_stats = _summarize_bets(bets_for_summary)
    favorite_stats = _summarize_bets(favorite_summary_bets or [])

    template = _env.get_template("performance.html")
    html = template.render(
        bets=bets_for_display,
        pending_count=pending_count,
        favorite_bets=favorite_display_bets or [],
        favorite_pending_count=favorite_pending_count,
        favorite=favorite_stats,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        **value_stats,
    )
    out_path = DIST_DIR / "performance.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def update_race_result_section(race_id: str, comparison: list) -> bool:
    """
    既に生成済みのレースページ(races/<race_id>.html)を直接読み込み、
    「結果」タブの中身だけをHTML文字列操作で書き換える
    （モデルの再読み込みなど重い処理をせずに済ませるため）。

    comparison: db.database.get_race_result_with_predictions() の戻り値
    戻り値: 更新できればTrue、対象ファイルが無ければFalse
    """
    out_path = DIST_DIR / "races" / f"{race_id}.html"
    if not out_path.exists():
        return False

    # finish_posが全頭分揃っていなければ「まだ結果未確定」として何もしない
    if not comparison or any(c.get("finish_pos") is None for c in comparison):
        return False

    rows_html = []
    for c in sorted(comparison, key=lambda x: (x["finish_pos"] is None, x["finish_pos"])):
        finish = c.get("finish_pos")
        is_top3 = finish is not None and finish <= 3
        hit_class = " hit-row" if (is_top3 and c.get("predicted_rank", 99) <= 3) else ""
        badge_class = "top3" if is_top3 else "other"
        finish_display = f"{finish}着" if finish is not None else "-"
        prob = c.get("predicted_probability")
        prob_display = f"{prob*100:.0f}%" if prob is not None else "-"
        rows_html.append(
            f'<tr class="{hit_class.strip()}">'
            f'<td class="mono">{c.get("predicted_rank", "-")}</td>'
            f'<td class="name-cell">{c.get("horse_number")} {c.get("horse_name") or ""}</td>'
            f'<td class="mono">{prob_display}</td>'
            f'<td><span class="finish-badge {badge_class} mono">{finish_display}</span></td>'
            f'</tr>'
        )

    hit_count = sum(1 for c in comparison if c.get("predicted_rank", 99) <= 3 and (c.get("finish_pos") or 99) <= 3)
    summary = f"AIが上位3位に予想した馬のうち、{hit_count}頭が実際に複勝圏内（3着以内）でした。"

    new_content = (
        '<!-- RESULT_CONTENT_START -->\n'
        f'<div class="card" style="font-size:12.5px; color:var(--text-dim); margin-bottom:10px;">{summary}</div>\n'
        '<div class="card" style="padding:0; overflow:hidden;">\n'
        '<table class="result-table">\n'
        '<thead><tr><th>AI予想順位</th><th>馬名</th><th>予想確率</th><th>実際の着順</th></tr></thead>\n'
        f'<tbody>{"".join(rows_html)}</tbody>\n'
        '</table>\n'
        '</div>\n'
        '<!-- RESULT_CONTENT_END -->'
    )

    html = out_path.read_text(encoding="utf-8")
    start_marker = "<!-- RESULT_CONTENT_START -->"
    end_marker = "<!-- RESULT_CONTENT_END -->"
    start_idx = html.find(start_marker)
    end_idx = html.find(end_marker)
    if start_idx == -1 or end_idx == -1:
        return False
    html = html[:start_idx] + new_content + html[end_idx + len(end_marker):]
    out_path.write_text(html, encoding="utf-8")
    return True


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
