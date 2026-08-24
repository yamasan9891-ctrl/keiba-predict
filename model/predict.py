"""
予想実行（DB直結版・学習時と同一の特徴量計算ロジックを再利用）

出馬表（まだ結果が出ていないレース）を、DBの全過去データと一緒に
features.build_features() に通すことで、学習時と全く同じ計算式で
（対戦相手の強さ・騎手成績・展開シミュレーション・枠順バイアス・休み明け等を含む）
特徴量を作る。予想専用の別ロジックを作らないことで、
「学習側だけ更新して予想側が古いまま」というバグを防ぐ。
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
from features.feature_engineering import load_race_entries, build_features, CATEGORICAL_COLS
from scraper.netkeiba_scraper import fetch_shutuba


def _shutuba_to_entry_rows(shutuba_df: pd.DataFrame, race_meta: dict, conn) -> pd.DataFrame:
    """出馬表を、load_race_entries()と同じ列構成のDataFrameに変換する（未確定情報はNaN）"""
    rename = {
        "馬 番": "horse_number", "枠": "post_position", "斤量": "weight_carried",
        "単勝 オッズ": "win_odds", "人 気": "popularity", "馬名": "horse_name",
    }
    df = shutuba_df.rename(columns={k: v for k, v in rename.items() if k in shutuba_df.columns})

    race_id = race_meta.get("race_id")
    rows = []
    for _, r in df.iterrows():
        horse_id = r.get("horse_id")
        ped = {"father": None, "mother_father": None}
        if horse_id:
            prow = conn.execute(
                "SELECT father, mother_father FROM horses WHERE horse_id = ?", (horse_id,)
            ).fetchone()
            if prow:
                ped = {"father": prow["father"], "mother_father": prow["mother_father"]}

        rows.append({
            "race_id": race_id,
            "horse_number": pd.to_numeric(r.get("horse_number"), errors="coerce"),
            "post_position": pd.to_numeric(r.get("post_position"), errors="coerce"),
            "horse_id": horse_id,
            "horse_name": r.get("horse_name"),
            "jockey_id": r.get("jockey_id"),
            "trainer_name": None,  # 出馬表段階では通常取得済みだが、未対応時はNaNで問題ない
            "weight_carried": pd.to_numeric(r.get("weight_carried"), errors="coerce"),
            "horse_weight": np.nan,       # 出馬表段階では馬体重は未発表のことが多い
            "horse_weight_diff": np.nan,
            "running_style": None,        # まだ走っていないので当該レースの脚質は存在しない（正しくNaN）
            "last_3f": None,               # 同上
            "finish_pos": None,            # 未確定（このレースの予想対象であることを示す）
            "win_odds": pd.to_numeric(r.get("win_odds"), errors="coerce"),
            "popularity": pd.to_numeric(r.get("popularity"), errors="coerce"),
            "is_placed": None,
            "race_date": race_meta.get("race_date"),
            "distance": race_meta.get("distance"),
            "surface": race_meta.get("surface"),
            "track_condition": race_meta.get("track_condition"),
            "weather": race_meta.get("weather"),
            "is_handicap": race_meta.get("is_handicap"),
            "grade": race_meta.get("grade"),
            "father": ped["father"],
            "mother_father": ped["mother_father"],
            "speed_figure": np.nan,  # 当該レースの指数はまだ存在しない（過去平均は履歴から計算される）
        })
    return pd.DataFrame(rows)


def predict(race_id: str) -> pd.DataFrame:
    model_path = MODEL_DIR / "model.pkl"
    if not model_path.exists():
        raise FileNotFoundError("model/artifacts/model.pkl がありません。先に model/train.py を実行してください。")

    bundle = joblib.load(model_path)
    model, feature_cols = bundle["model"], bundle["features"]

    init_db()
    shutuba = fetch_shutuba(race_id)
    race_meta = shutuba.attrs.get("meta", {"race_id": race_id})
    race_meta["race_id"] = race_id

    with get_conn() as conn:
        historical = load_race_entries(conn)
        shutuba_rows = _shutuba_to_entry_rows(shutuba, race_meta, conn)

    # 過去データ＋今回の出馬表を結合して、学習時と全く同じ計算式で特徴量を作る
    combined = pd.concat([historical, shutuba_rows], ignore_index=True, sort=False)
    features_all = build_features(combined)

    features = features_all[features_all["race_id"] == race_id].copy()
    if features.empty:
        raise RuntimeError(f"出馬表の行が特徴量テーブルに見つかりませんでした: {race_id}")

    missing = [c for c in feature_cols if c not in features.columns]
    for c in missing:
        features[c] = np.nan

    for col in CATEGORICAL_COLS:
        if col in features.columns and features[col].dtype.name != "category":
            features[col] = features[col].astype("category")

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
