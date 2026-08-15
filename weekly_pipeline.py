"""
週次パイプライン（GitHub Actionsから毎週呼び出す想定）

流れ:
  1. 先週分の確定レース結果をDBに追加収集（過去10年分を少しずつ埋めていく用途にも使う）
  2. 蓄積したDB全体で特徴量再作成・モデル再学習（相性データも含めて毎週賢くなる）
  3. 今週開催されるレースの出馬表を取得
  4. 各馬の複勝確率・EV・WIN5候補・理由文を計算
  5. static_site/dist/ に今週分のHTMLを生成
  6. (このスクリプトの外側で) git commit & push → GitHub Pagesへ反映

このファイルは「まず動く骨格」を提供するもので、対象レースID一覧の自動収集
（JRAの開催日程からrace_idを機械的に列挙する部分）はサイト構造に依存するため
TODOとして明示しています。運用開始時に your_race_id_source() を実装してください。
"""
import argparse
import datetime as dt

from db.database import init_db, get_conn
from scraper.netkeiba_scraper import fetch_race_result, fetch_shutuba, save_to_db, infer_running_style
from scraper.odds_scraper import fetch_all_odds
from features.feature_engineering import load_all_results, load_all_histories, build_features, FEATURE_COLS
from model.train import train as train_model
from model.predict import predict as predict_race
from betting.ev_engine import normalize_strengths, build_ev_table, best_bet, positive_ev_rows, identify_value_horses
from betting.reasoning import generate_reason, summarize_race
from betting.win5 import race_win_candidates, build_win5_combinations
from static_site.generate_site import generate_index, generate_race_page


def this_week_race_ids() -> list[str]:
    """
    TODO: 今週開催されるJRAレースのrace_id一覧を返す。
    netkeibaのカレンダーページ（例: race.netkeiba.com/top/calendar.html）や
    開催日程ページから、今週の開催場・R番号を機械的に列挙して
    race_idを組み立てる実装をここに追加してください。
    （race_idの構造: 年4桁+競馬場コード2桁+開催回2桁+日目2桁+R番号2桁 が基本）
    """
    raise NotImplementedError("今週のレースID取得ロジックを実装してください")


def last_week_result_race_ids() -> list[str]:
    """TODO: 先週確定した結果レースのrace_id一覧を返す（学習データ蓄積用）"""
    raise NotImplementedError("先週のレースID取得ロジックを実装してください")


def collect_results(race_ids: list[str]):
    for rid in race_ids:
        try:
            df = fetch_race_result(rid)
            save_to_db(df)
            print(f"収集OK: {rid}")
        except Exception as e:
            print(f"収集失敗: {rid} ({e})")


def retrain():
    results = load_all_results()
    histories = load_all_histories()
    features = build_features(results, histories)
    features.to_csv("data/processed/features.csv", index=False, encoding="utf-8-sig")
    train_model()


def build_race_page_data(race_id: str, race_meta: dict, n_races_win5: int = 0) -> dict:
    """1レース分の予想・EV・理由をまとめて計算する"""
    pred_df = predict_race(race_id)  # place_probability列を含むDataFrame

    strengths_raw = dict(zip(pred_df["horse_number"].astype(str), pred_df["place_probability"]))
    strengths = normalize_strengths(strengths_raw)
    names = dict(zip(pred_df["horse_number"].astype(str), pred_df.get("馬名", pred_df["horse_number"])))

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

    n_nige = int((pred_df.get("running_style") == "逃げ").sum()) if "running_style" in pred_df.columns else None
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
        reason = generate_reason(h_features, race_context)
        horses.append({
            "horse_number": r.get("horse_number"),
            "name": r.get("馬名", r.get("horse_number")),
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

    print("=== 1. 先週分の結果を収集 ===")
    if not dry_run:
        collect_results(last_week_result_race_ids())

    print("=== 2. 再学習 ===")
    if not dry_run:
        retrain()

    print("=== 3. 今週のレースを取得・予想 ===")
    race_ids = [] if dry_run else this_week_race_ids()
    days_index = {}
    for rid in race_ids:
        df = fetch_shutuba(rid)
        save_to_db(df)
        meta = df.attrs.get("meta", {"race_id": rid})
        page_data = build_race_page_data(rid, meta)
        generate_race_page(**page_data)

        date_key = meta.get("race_date") or "unknown"
        days_index.setdefault(date_key, []).append({
            "race_id": rid,
            "course": meta.get("course"),
            "race_number": meta.get("race_number"),
            "race_name": meta.get("race_name"),
            "surface": meta.get("surface"),
            "distance": meta.get("distance"),
            "track_condition": meta.get("track_condition"),
            "n_horses": len(df),
            "is_win5_race": bool(meta.get("is_win5_race")),
            "is_handicap": bool(meta.get("is_handicap")),
        })

    days = [{"date": d, "weekday": "", "races": races} for d, races in sorted(days_index.items())]
    generate_index(days)
    print("=== 完了 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="実際の収集は行わずサイト生成の骨格だけ確認")
    args = parser.parse_args()
    run_weekly(dry_run=args.dry_run)
