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

from db.database import init_db
from scraper.netkeiba_scraper import fetch_shutuba, fetch_this_week_race_ids, save_to_db
from scraper.odds_scraper import fetch_all_odds
from model.predict import predict as predict_race
from betting.ev_engine import normalize_strengths, build_ev_table, best_bet, positive_ev_rows, identify_value_horses
from betting.reasoning import generate_reason, summarize_race
from betting.win5 import race_win_candidates, build_win5_combinations
from static_site.generate_site import generate_index, generate_race_page


def build_race_page_data(race_id: str, race_meta: dict) -> dict:
    """1レース分の予想・EV・理由をまとめて計算する"""
    pred_df = predict_race(race_id)

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
    }


def run_weekly(dry_run: bool = False):
    init_db()

    print("=== 今週のレースを取得・予想 ===")
    race_ids = [] if dry_run else fetch_this_week_race_ids()
    print(f"対象レース数: {len(race_ids)}")

    days_index = {}
    for rid in race_ids:
        try:
            df = fetch_shutuba(rid)
        except Exception as e:
            print(f"  [警告] {rid} の出馬表取得に失敗: {e}")
            continue

        save_to_db(df)
        meta = df.attrs.get("meta", {"race_id": rid})
        meta["race_id"] = rid

        try:
            page_data = build_race_page_data(rid, meta)
            generate_race_page(**page_data)
        except Exception as e:
            print(f"  [警告] {rid} の予想生成に失敗: {e}")
            continue

        # race_dateが未取得のため、race_id先頭の年+今週末の日付を仮のグルーピングキーにする
        date_key = meta.get("race_date") or rid[:4]
        days_index.setdefault(date_key, []).append({
            "race_id": rid,
            "course": meta.get("course"),
            "race_number": rid[-2:],
            "race_name": meta.get("race_name") or rid,
            "surface": meta.get("surface"),
            "distance": meta.get("distance"),
            "track_condition": meta.get("track_condition"),
            "n_horses": len(df),
            "is_win5_race": bool(meta.get("is_win5_race")),
            "is_handicap": bool(meta.get("is_handicap")),
        })
        print(f"  ✓ {rid} の予想ページを生成しました")

    days = [{"date": d, "weekday": "", "races": races} for d, races in sorted(days_index.items())]
    generate_index(days)
    print("=== 完了 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="実際の取得は行わずサイト生成の骨格だけ確認")
    args = parser.parse_args()
    run_weekly(dry_run=args.dry_run)
