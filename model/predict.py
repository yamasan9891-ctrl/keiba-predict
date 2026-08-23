"""
予想実行（DB直結版）

出馬表（まだ結果が出ていないレース）を取得し、各馬について
DBに蓄積された「過去の（このレースより前の）成績」から特徴量を計算して、
学習済みモデルで複勝圏内確率を予測する。
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


def _horse_history_features(conn, horse_id: str) -> dict:
    """
    ある馬の「現時点までの全過去成績」から特徴量を計算する
    （まだ行われていないレースの予想なので、DBにある全履歴が「過去」になる）。
    """
    rows = conn.execute(
        "SELECT finish_pos, last_3f, running_style FROM entries "
        "WHERE horse_id = ? AND finish_pos IS NOT NULL ORDER BY race_id",
        (horse_id,),
    ).fetchall()

    if not rows:
        return {
            "avg_finish_pos": np.nan, "n_races": 0, "best_finish_pos": np.nan,
            "place_rate": np.nan, "recent3_avg_finish_pos": np.nan,
            "avg_last_3f": np.nan, "prior_running_style": None,
        }

    finishes = [r["finish_pos"] for r in rows]
    last3fs = [r["last_3f"] for r in rows if r["last_3f"] is not None]

    return {
        "avg_finish_pos": float(np.mean(finishes)),
        "n_races": len(finishes),
        "best_finish_pos": float(np.min(finishes)),
        "place_rate": float(np.mean([f <= 3 for f in finishes])),
        "recent3_avg_finish_pos": float(np.mean(finishes[-3:])),
        "avg_last_3f": float(np.mean(last3fs)) if last3fs else np.nan,
        "prior_running_style": rows[-1]["running_style"],
    }


def _pedigree_features(conn, horse_id: str) -> dict:
    row = conn.execute(
        "SELECT father, mother_father FROM horses WHERE horse_id = ?", (horse_id,)
    ).fetchone()
    if row is None:
        return {"father": None, "mother_father": None}
    return {"father": row["father"], "mother_father": row["mother_father"]}


def build_prediction_features(shutuba_df: pd.DataFrame, race_meta: dict) -> pd.DataFrame:
    init_db()
    rename = {
        "馬 番": "horse_number", "枠": "post_position", "斤量": "weight_carried",
        "単勝 オッズ": "win_odds", "人 気": "popularity", "馬名": "horse_name",
    }
    df = shutuba_df.rename(columns={k: v for k, v in rename.items() if k in shutuba_df.columns})

    rows = []
    with get_conn() as conn:
        for _, r in df.iterrows():
            horse_id = r.get("horse_id")
            hist = _horse_history_features(conn, horse_id) if horse_id else {}
            ped = _pedigree_features(conn, horse_id) if horse_id else {}
            row = {
                "horse_number": r.get("horse_number"),
                "post_position": r.get("post_position"),
                "horse_id": horse_id,
                "horse_name": r.get("horse_name"),
                "weight_carried": pd.to_numeric(r.get("weight_carried"), errors="coerce"),
                "win_odds": pd.to_numeric(r.get("win_odds"), errors="coerce"),
                "popularity": pd.to_numeric(r.get("popularity"), errors="coerce"),
                "horse_weight": np.nan,       # 出馬表段階では未発表のことが多い
                "horse_weight_diff": np.nan,
                "distance": race_meta.get("distance"),
                "surface": race_meta.get("surface"),
                "track_condition": race_meta.get("track_condition"),  # 直前まで確定しないことが多い
                "is_handicap": 1 if race_meta.get("is_handicap") else 0,
                **hist,
                **ped,
            }
            rows.append(row)

    features = pd.DataFrame(rows)
    for col in CATEGORICAL_COLS:
        if col in features.columns:
            features[col] = features[col].astype("category")
    return features


def predict(race_id: str) -> pd.DataFrame:
    model_path = MODEL_DIR / "model.pkl"
    if not model_path.exists():
        raise FileNotFoundError("model/artifacts/model.pkl がありません。先に model/train.py を実行してください。")

    bundle = joblib.load(model_path)
    model, feature_cols = bundle["model"], bundle["features"]

    shutuba = fetch_shutuba(race_id)
    race_meta = shutuba.attrs.get("meta", {})
    features = build_prediction_features(shutuba, race_meta)

    missing = [c for c in feature_cols if c not in features.columns]
    for c in missing:
        features[c] = np.nan

    X = features[feature_cols]
    features["place_probability"] = model.predict_proba(X)[:, 1]
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
