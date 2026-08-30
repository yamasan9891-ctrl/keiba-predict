"""
予想実行（高速版）

以前のバージョンは、予想のたびに過去データ全件（数万行）を
学習時と同じ複雑な計算（groupby×複数回）に通していたため、
1レースあたり2分以上かかり、週末72レース分の処理が現実的な時間で
終わらないという重大な性能問題があった。

このバージョンでは、「各馬・各騎手の"現時点での"実力値」を
週次パイプライン開始時に1回だけ計算し（precompute_current_stats）、
個々のレース予想はその結果を単純に参照するだけにすることで、
1レースあたりの処理を数百倍高速化する。

なお、学習（model/train.py）側は引き続き「そのレースより前の情報だけ」を
使うリーク防止つきの厳密な計算（features/feature_engineering.py）を使う。
今回の高速版は「今まさに行われる未来のレース」の予想専用であり、
そこでは全ての過去データが正当に「既知の情報」であるため、
リーク防止のための複雑なshift処理が本来不要という点を利用している。
"""
import argparse
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import MODEL_DIR, RACE_ID_HELP
from db.database import get_conn, init_db
from features.feature_engineering import CATEGORICAL_COLS
from scraper.netkeiba_scraper import fetch_shutuba

# JRAの1レースの出走可能頭数は最大18頭。人気9999や負のオッズは
# 出走取消・データ異常のサイン。
MAX_VALID_HORSE_NUMBER = 30
MIN_VALID_ODDS = 0.1


def precompute_current_stats(historical: pd.DataFrame) -> dict:
    """
    馬ごと・騎手ごとの「現時点での」累積実力値をまとめて1回だけ計算する。
    戻り値: {"horse": DataFrame, "jockey": DataFrame, "post_bias": DataFrame}
    """
    df = historical.copy()

    # --- 馬ごとの累積成績 ---
    h = df.sort_values(["horse_id", "race_id"])
    grp = h.groupby("horse_id")
    horse_stats = grp.agg(
        avg_finish_pos=("finish_pos", "mean"),
        n_races=("finish_pos", "count"),
        best_finish_pos=("finish_pos", "min"),
        avg_last_3f=("last_3f", "mean"),
    ).reset_index()
    horse_stats["place_rate"] = grp["finish_pos"].apply(lambda s: (s <= 3).mean()).values
    horse_stats["recent3_avg_finish_pos"] = grp["finish_pos"].apply(lambda s: s.tail(3).mean()).values
    horse_stats["prior_running_style"] = grp["running_style"].last().values
    if "speed_figure" in df.columns:
        horse_stats["avg_speed_figure"] = grp["speed_figure"].mean().values
        horse_stats["best_speed_figure"] = grp["speed_figure"].max().values
    else:
        horse_stats["avg_speed_figure"] = np.nan
        horse_stats["best_speed_figure"] = np.nan

    # --- 騎手ごとの累積成績 ---
    j = df.sort_values(["jockey_id", "race_id"])
    jgrp = j.groupby("jockey_id")
    jockey_stats = jgrp.agg(jockey_n_races=("finish_pos", "count")).reset_index()
    jockey_stats["jockey_place_rate"] = jgrp["finish_pos"].apply(lambda s: (s <= 3).mean()).values

    # --- 調教師ごとの累積成績 ---
    if "trainer_name" in df.columns:
        t = df.sort_values(["trainer_name", "race_id"])
        tgrp = t.groupby("trainer_name")
        trainer_stats = tgrp.agg(trainer_n_races=("finish_pos", "count")).reset_index()
        trainer_stats["trainer_place_rate"] = tgrp["finish_pos"].apply(lambda s: (s <= 3).mean()).values
    else:
        trainer_stats = pd.DataFrame(columns=["trainer_name", "trainer_n_races", "trainer_place_rate"])

    # --- 枠順バイアス（距離×芝ダート×枠番） ---
    if "post_position" in df.columns:
        pos_stats = df.groupby(["distance", "surface", "post_position"])["is_placed"].agg(["mean", "count"]).reset_index()
        pos_stats.loc[pos_stats["count"] < 30, "mean"] = np.nan
        pos_stats = pos_stats.rename(columns={"mean": "post_position_bias"})
    else:
        pos_stats = pd.DataFrame(columns=["distance", "surface", "post_position", "post_position_bias"])

    # --- 馬の「強さ」の目安（対戦相手の強さ計算に使う）: 現時点の複勝率をそのまま使う ---
    horse_strength = horse_stats.set_index("horse_id")["place_rate"].to_dict()

    return {
        "horse": horse_stats.set_index("horse_id"),
        "jockey": jockey_stats.set_index("jockey_id"),
        "trainer": trainer_stats.set_index("trainer_name") if not trainer_stats.empty else trainer_stats,
        "post_bias": pos_stats,
        "horse_strength": horse_strength,
    }


def _tan_lookup(tan_data: dict, horse_number, field: str, fallback):
    """単勝オッズAPIの値を優先し、無ければ出馬表側の値にフォールバックする"""
    try:
        key = str(int(pd.to_numeric(horse_number, errors="coerce")))
    except (ValueError, TypeError):
        key = None
    if key and key in tan_data and tan_data[key].get(field) is not None:
        return tan_data[key][field]
    return pd.to_numeric(fallback, errors="coerce")


def _match_col(columns, *keywords):
    for col in columns:
        col_norm = str(col).replace("\n", "").replace(" ", "")
        if all(kw in col_norm for kw in keywords):
            return col
    return None


def _pace_advantage(running_style, predicted_pace, front_styles, closer_styles) -> int:
    """展開とその馬の脚質から、有利(+1)/不利(-1)/中立(0)を判定する"""
    if running_style is None:
        return 0
    if predicted_pace == "ハイペース":
        if running_style in closer_styles:
            return 1
        if running_style in front_styles:
            return -1
    elif predicted_pace == "スローペース":
        if running_style in front_styles:
            return 1
        if running_style in closer_styles:
            return -1
    return 0


def build_prediction_row(shutuba_df: pd.DataFrame, race_meta: dict, conn, stats: dict, tan_data: dict) -> pd.DataFrame:
    """出馬表1レース分から、precompute_current_statsの結果を参照して特徴量を組み立てる（高速）"""
    cols = shutuba_df.columns
    rename = {}
    for target, keywords in [
        ("horse_number", ("馬番",)), ("post_position", ("枠",)),
        ("weight_carried", ("斤量",)), ("win_odds", ("オッズ",)),
        ("popularity", ("人気",)), ("horse_name", ("馬名",)),
        ("jockey_name_disp", ("騎手",)), ("sex_age", ("性齢",)),
        ("horse_weight_disp", ("馬体重",)), ("trainer_name_disp", ("厩舎",)),
    ]:
        found = _match_col(cols, *keywords)
        if found is not None:
            rename[found] = target
    df = shutuba_df.rename(columns=rename)

    race_id = race_meta.get("race_id")
    horse_df = stats["horse"]
    jockey_df = stats["jockey"]
    trainer_df = stats.get("trainer")
    post_bias = stats["post_bias"]
    horse_strength = stats["horse_strength"]

    # このレースの全出走馬の複勝率（対戦相手の強さの算出に使う）
    horse_ids_today = df.get("horse_id", pd.Series(dtype=str)).tolist()
    strengths_today = [horse_strength.get(hid, np.nan) for hid in horse_ids_today]

    # 展開シミュレーション: 出走馬全員の「直近の脚質」からこのレースのペースを予測する
    front_styles = {"逃げ", "先行"}
    closer_styles = {"差し", "追込"}
    styles_today = [
        (horse_df.loc[hid, "prior_running_style"] if hid in horse_df.index else None)
        for hid in horse_ids_today
    ]
    valid_styles = [s for s in styles_today if s is not None]
    front_ratio = (sum(1 for s in valid_styles if s in front_styles) / len(valid_styles)) if valid_styles else 0.3
    if front_ratio > 0.4:
        predicted_pace = "ハイペース"
    elif front_ratio < 0.2:
        predicted_pace = "スローペース"
    else:
        predicted_pace = "平均"

    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        horse_id = r.get("horse_id")
        h = horse_df.loc[horse_id] if horse_id in horse_df.index else None
        jockey_id = r.get("jockey_id")
        j = jockey_df.loc[jockey_id] if jockey_id in jockey_df.index else None
        trainer_name = r.get("trainer_name")
        t = (trainer_df.loc[trainer_name] if trainer_df is not None and not trainer_df.empty and trainer_name in trainer_df.index else None)

        ped = {"father": None, "mother_father": None}
        if horse_id:
            prow = conn.execute(
                "SELECT father, mother_father FROM horses WHERE horse_id = ?", (horse_id,)
            ).fetchone()
            if prow:
                ped = {"father": prow["father"], "mother_father": prow["mother_father"]}

        # 対戦相手の強さ = 自分以外の今日の出走馬の複勝率平均
        others = [s for k, s in enumerate(strengths_today) if k != i and not pd.isna(s)]
        opp_strength = float(np.mean(others)) if others else np.nan

        distance = race_meta.get("distance")
        surface = race_meta.get("surface")
        post_position = pd.to_numeric(r.get("post_position"), errors="coerce")
        bias_row = post_bias[
            (post_bias["distance"] == distance) & (post_bias["surface"] == surface) &
            (post_bias["post_position"] == post_position)
        ]
        pos_bias_val = bias_row["post_position_bias"].iloc[0] if not bias_row.empty else np.nan

        win_odds = _tan_lookup(tan_data, r.get("horse_number"), "odds", r.get("win_odds"))
        popularity = _tan_lookup(tan_data, r.get("horse_number"), "popularity", r.get("popularity"))
        # 出走取消・異常値ガード（オッズが極端に小さい/負、人気が異常に大きい）
        if pd.notna(win_odds) and win_odds < MIN_VALID_ODDS:
            win_odds, popularity = np.nan, np.nan
        if pd.notna(popularity) and popularity > MAX_VALID_HORSE_NUMBER:
            win_odds, popularity = np.nan, np.nan

        rows.append({
            "race_id": race_id,
            "horse_number": pd.to_numeric(r.get("horse_number"), errors="coerce"),
            "post_position": post_position,
            "horse_id": horse_id,
            "horse_name": r.get("horse_name"),
            "win_odds": win_odds,
            "popularity": popularity,
            "jockey_name_disp": r.get("jockey_name_disp"),
            "sex_age": r.get("sex_age"),
            "horse_weight_disp": r.get("horse_weight_disp"),
            "trainer_name_disp": r.get("trainer_name_disp"),
            "weight_carried": pd.to_numeric(r.get("weight_carried"), errors="coerce"),
            "horse_weight": np.nan,
            "horse_weight_diff": np.nan,
            "distance": distance,
            "surface": surface,
            "track_condition": race_meta.get("track_condition"),
            "is_handicap": 1 if race_meta.get("is_handicap") else 0,
            "avg_finish_pos": h["avg_finish_pos"] if h is not None else np.nan,
            "n_races": h["n_races"] if h is not None else 0,
            "best_finish_pos": h["best_finish_pos"] if h is not None else np.nan,
            "place_rate": h["place_rate"] if h is not None else np.nan,
            "recent3_avg_finish_pos": h["recent3_avg_finish_pos"] if h is not None else np.nan,
            "avg_last_3f": h["avg_last_3f"] if h is not None else np.nan,
            "avg_speed_figure": h["avg_speed_figure"] if h is not None else np.nan,
            "best_speed_figure": h["best_speed_figure"] if h is not None else np.nan,
            "avg_opponent_strength": opp_strength,
            "jockey_place_rate": j["jockey_place_rate"] if j is not None else np.nan,
            "jockey_n_races": j["jockey_n_races"] if j is not None else 0,
            "trainer_place_rate": t["trainer_place_rate"] if t is not None else np.nan,
            "trainer_n_races": t["trainer_n_races"] if t is not None else 0,
            "prior_running_style": h["prior_running_style"] if h is not None else None,
            "predicted_pace": predicted_pace,
            "pace_advantage": _pace_advantage(h["prior_running_style"] if h is not None else None, predicted_pace, front_styles, closer_styles),
            "post_position_bias": pos_bias_val,
            "father": ped["father"],
            "mother_father": ped["mother_father"],
        })

    return pd.DataFrame(rows)


def predict(race_id: str, stats: dict = None, historical: pd.DataFrame = None) -> pd.DataFrame:
    """
    stats（precompute_current_statsの結果）を渡すと高速に予想できる。
    省略時はこの呼び出し内でDBから読み込んで計算する（単発CLI実行用、やや遅い）。
    """
    model_path = MODEL_DIR / "model.pkl"
    if not model_path.exists():
        raise FileNotFoundError("model/artifacts/model.pkl がありません。先に model/train.py を実行してください。")

    bundle = joblib.load(model_path)
    model, feature_cols = bundle["model"], bundle["features"]
    calibrator = bundle.get("calibrator")  # 古いモデルファイルには無い場合があるので安全に取得

    init_db()

    if stats is None:
        from features.feature_engineering import load_race_entries
        with get_conn() as conn:
            if historical is None:
                historical = load_race_entries(conn)
        stats = precompute_current_stats(historical)

    shutuba = fetch_shutuba(race_id)
    race_meta = shutuba.attrs.get("meta", {"race_id": race_id})
    race_meta["race_id"] = race_id

    try:
        from scraper.odds_scraper import fetch_tan_odds_and_popularity
        tan_data = fetch_tan_odds_and_popularity(race_id)
    except Exception as e:
        print(f"[警告] 単勝オッズAPI取得失敗（出馬表の値のまま進めます）: {e}")
        tan_data = {}

    with get_conn() as conn:
        features = build_prediction_row(shutuba, race_meta, conn, stats, tan_data)

    missing = [c for c in feature_cols if c not in features.columns]
    for c in missing:
        features[c] = np.nan  # object型のNoneではなく数値NaNで埋める（LightGBMがdtypeエラーになるため）

    for col in CATEGORICAL_COLS:
        if col in features.columns:
            features[col] = features[col].astype("category")

    X = features[feature_cols]
    raw_proba = model.predict_proba(X)[:, 1]
    features["place_probability"] = calibrator.predict(raw_proba) if calibrator is not None else raw_proba
    features = features.sort_values("place_probability", ascending=False)

    display_cols = [c for c in ["horse_number", "horse_name", "popularity", "win_odds", "place_probability"] if c in features.columns]
    print(features[display_cols].to_string(index=False))
    return features


def main():
    parser = argparse.ArgumentParser(description="予想実行")
    parser.add_argument("--race-id", required=True, help=RACE_ID_HELP)
    args = parser.parse_args()
    predict(args.race_id)


if __name__ == "__main__":
    main()
