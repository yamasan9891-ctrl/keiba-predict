"""
週次パイプライン（GitHub Actionsから毎週呼び出す想定）

流れ:
  1. 今週末（土日）に開催されるレースのrace_idを自動取得
  2. 出馬表を取得し、DBに保存されている過去データから予想特徴量を計算
  3. 学習済みモデルで複勝確率・単勝確率を予測
  4. EV計算・穴馬抽出・WIN5候補・理由文を生成
  5. static_site/dist/ に今週分のHTMLを生成
  6. (このスクリプトの外側で) git commit & push → GitHub Pagesへ反映
"""
import argparse
import datetime as dt

from db.database import init_db, get_conn
from scraper.netkeiba_scraper import fetch_shutuba, fetch_this_week_race_ids, fetch_win5_race_ids, save_to_db
from scraper.odds_scraper import fetch_all_odds
from model.predict import predict as predict_race, precompute_current_stats
from features.feature_engineering import load_race_entries
from betting.ev_engine import normalize_strengths, build_ev_table, best_bet, positive_ev_rows, identify_value_horses, build_betting_plan
from betting.reasoning import generate_reason, summarize_race
from betting.win5 import race_win_candidates, build_win5_combinations
from static_site.generate_site import generate_index, generate_race_page


def build_race_page_data(race_id: str, race_meta: dict, stats, is_win5: bool = False) -> tuple:
    """1レース分の予想・EV・理由をまとめて計算する。戻り値は (page_data辞書, 単勝確率dict)"""
    race_meta = dict(race_meta)
    if not race_meta.get("race_number"):
        race_meta["race_number"] = str(int(race_id[-2:]))  # race_id末尾2桁がR番号
    race_meta["is_win5_race"] = is_win5

    pred_df = predict_race(race_id, stats=stats)

    strengths_raw = dict(zip(pred_df["horse_number"].astype(str), pred_df["place_probability"]))
    strengths = normalize_strengths(strengths_raw)
    names = dict(zip(pred_df["horse_number"].astype(str), pred_df.get("horse_name", pred_df["horse_number"])))

    odds = {"tan": dict(zip(pred_df["horse_number"].astype(str), pred_df.get("win_odds", [])))}
    try:
        odds.update(fetch_all_odds(race_id))
    except Exception as e:
        print(f"オッズ取得失敗（単勝以外はEV計算をスキップ）: {e}")

    ev_tables_all = build_ev_table(strengths, odds, names)
    ev_tables_positive = positive_ev_rows(ev_tables_all)
    bb = best_bet(ev_tables_all)
    betting_plan = build_betting_plan(ev_tables_all, max_picks=5)

    popularity = dict(zip(pred_df["horse_number"].astype(str), pred_df.get("popularity", [])))
    dark_horses = identify_value_horses(
        strengths, odds.get("tan", {}), popularity, names,
        popularity_threshold=5, ev_threshold=1.0,
    )

    n_nige = int((pred_df.get("prior_running_style") == "逃げ").sum()) if "prior_running_style" in pred_df.columns else None
    race_context = {
        "n_nige": n_nige,
        "track_condition": race_meta.get("track_condition"),
        "is_handicap": race_meta.get("is_handicap"),
    }
    summary_text = summarize_race(race_context)

    horses = []
    for _, r in pred_df.iterrows():
        h_features = r.to_dict()
        h_features["place_probability"] = r["place_probability"]
        h_features["running_style"] = r.get("prior_running_style")
        reason = generate_reason(h_features, race_context)
        horses.append({
            "horse_number": r.get("horse_number"),
            "name": r.get("horse_name", r.get("horse_number")),
            "place_probability": r["place_probability"],
            "reason": reason,
        })

    return {
        "race": race_meta,
        "horses": horses,
        "ev_tables": ev_tables_positive,
        "best_bet": bb,
        "race_summary_text": summary_text,
        "dark_horses": dark_horses,
        "betting_plan": betting_plan,
    }, strengths


def run_weekly(dry_run: bool = False):
    init_db()

    print("=== 今週のレースを取得・予想 ===")
    race_ids = [] if dry_run else fetch_this_week_race_ids()
    print(f"対象レース数: {len(race_ids)}")

    win5_ids = set() if dry_run else fetch_win5_race_ids()
    if win5_ids:
        print(f"WIN5対象レース: {len(win5_ids)}件")

    # 各馬・各騎手の「現時点での」実力値を1回だけ事前計算し、全レースで使い回す
    # （以前はレースごとに全過去データを再計算しており、1レース2分以上かかっていた）
    stats = None
    if race_ids:
        print("過去データを読み込み・実力値を事前計算中（1回だけ）...")
        with get_conn() as conn:
            historical = load_race_entries(conn)
        stats = precompute_current_stats(historical)
        print(f"事前計算完了: 対象馬{len(stats['horse'])}頭 / 対象騎手{len(stats['jockey'])}人")

    days_index = {}
    win5_page_data = {}       # race_id -> page_data（WIN5対象レースのみ）
    win5_strengths = {}       # race_id -> {horse: 強さ}（WIN5対象レースのみ）
    win5_race_labels = {}     # race_id -> "○○ 11R" のような表示ラベル

    for rid in race_ids:
        try:
            df = fetch_shutuba(rid)
        except Exception as e:
            print(f"  [警告] {rid} の出馬表取得に失敗: {e}")
            continue

        save_to_db(df)
        meta = df.attrs.get("meta", {"race_id": rid})
        meta["race_id"] = rid
        is_win5 = rid in win5_ids

        try:
            page_data, strengths = build_race_page_data(rid, meta, stats, is_win5=is_win5)
            generate_race_page(**page_data)
        except Exception as e:
            print(f"  [警告] {rid} の予想生成に失敗: {e}")
            continue

        if is_win5:
            win5_page_data[rid] = page_data
            win5_strengths[rid] = strengths
            win5_race_labels[rid] = f"{meta.get('course')} {page_data['race']['race_number']}R"

        # race_dateが未取得のため、race_id先頭の年+今週末の日付を仮のグルーピングキーにする
        date_key = meta.get("race_date") or rid[:4]
        days_index.setdefault(date_key, []).append({
            "race_id": rid,
            "course": meta.get("course"),
            "race_number": page_data["race"]["race_number"],
            "race_name": meta.get("race_name") or rid,
            "surface": meta.get("surface"),
            "distance": meta.get("distance"),
            "track_condition": meta.get("track_condition"),
            "n_horses": len(df),
            "is_win5_race": is_win5,
            "is_handicap": bool(meta.get("is_handicap")),
        })
        print(f"  ✓ {rid} の予想ページを生成しました")

    # WIN5対象5レース全部の予想が揃っていれば、組み合わせを計算して該当ページに反映する
    if len(win5_strengths) == len(win5_ids) and win5_ids:
        print("=== WIN5買い目を計算中 ===")
        ordered_ids = sorted(win5_strengths.keys())
        candidates = [race_win_candidates(win5_strengths[rid], top_n=3) for rid in ordered_ids]
        combos = build_win5_combinations(candidates, max_combos=10)
        win5_result = {
            "race_labels": [win5_race_labels[rid] for rid in ordered_ids],
            "combos": combos,
        }
        for rid in ordered_ids:
            page_data = dict(win5_page_data[rid])
            page_data["win5"] = win5_result
            generate_race_page(**page_data)
        print(f"WIN5買い目を{len(ordered_ids)}レースのページに反映しました")

    days = [{"date": d, "weekday": "", "races": races} for d, races in sorted(days_index.items())]
    generate_index(days)
    print("=== 完了 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="実際の取得は行わずサイト生成の骨格だけ確認")
    args = parser.parse_args()
    run_weekly(dry_run=args.dry_run)
