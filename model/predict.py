"""
予想実行

data/raw/shutuba_<race_id>.csv （＋あれば horse_history_<race_id>.csv）を読み込み、
学習済みモデルで各馬の複勝圏内確率を計算して
data/processed/prediction_<race_id>.csv に保存する。
"""
import argparse
from pathlib import Path

import joblib
import pandas as pd

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_RAW_DIR, DATA_PROCESSED_DIR, MODEL_DIR, RACE_ID_HELP
from features.feature_engineering import build_features


def load_shutuba(race_id: str) -> pd.DataFrame:
    path = DATA_RAW_DIR / f"shutuba_{race_id}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} がありません。先に scraper/netkeiba_scraper.py --mode shutuba "
            f"--race-id {race_id} を実行してください。"
        )
    return pd.read_csv(path, encoding="utf-8-sig")


def load_history(race_id: str) -> pd.DataFrame:
    path = DATA_RAW_DIR / f"horse_history_{race_id}.csv"
    if path.exists():
        return pd.read_csv(path, encoding="utf-8-sig")
    return pd.DataFrame()


def predict(race_id: str) -> pd.DataFrame:
    model_path = MODEL_DIR / "model.pkl"
    if not model_path.exists():
        raise FileNotFoundError("model/artifacts/model.pkl がありません。先に model/train.py を実行してください。")

    bundle = joblib.load(model_path)
    model, feature_cols = bundle["model"], bundle["features"]

    shutuba = load_shutuba(race_id)
    history = load_history(race_id)
    features = build_features(shutuba, history)

    missing = [c for c in feature_cols if c not in features.columns]
    for c in missing:
        features[c] = float("nan")  # 欠けている特徴量はNaN扱い（LightGBMはNaNを扱える）

    X = features[feature_cols].apply(pd.to_numeric, errors="coerce")
    features["place_probability"] = model.predict_proba(X)[:, 1]

    name_col = "馬名" if "馬名" in features.columns else None
    sort_cols = ["place_probability"]
    display_cols = [c for c in ["horse_number", name_col, "popularity", "win_odds", "place_probability"] if c]
    features = features.sort_values("place_probability", ascending=False)

    out_path = DATA_PROCESSED_DIR / f"prediction_{race_id}.csv"
    features.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"保存しました: {out_path}")
    print(features[display_cols].to_string(index=False))
    return features


def main():
    parser = argparse.ArgumentParser(description="予想実行")
    parser.add_argument("--race-id", required=True, help=RACE_ID_HELP)
    args = parser.parse_args()
    predict(args.race_id)


if __name__ == "__main__":
    main()
