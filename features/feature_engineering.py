"""
特徴量エンジニアリング

data/raw/ にある result_*.csv / horse_history_*.csv を読み込み、
モデル学習・推論に使える特徴量テーブルを作って data/processed/ に保存する。

特徴量の例:
  - 斤量、馬体重（増減）、枠番、馬番
  - 直近成績（平均着順、複勝率、直近3走の着順推移）
  - 騎手の勝率（簡易集計）
  - 単勝人気（市場の評価をそのまま特徴量として利用）
"""
import glob
import re
from pathlib import Path

import numpy as np
import pandas as pd

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_RAW_DIR, DATA_PROCESSED_DIR, TARGET_COL


def _to_number(series: pd.Series) -> pd.Series:
    """全角数字や単位混じりの文字列を数値に変換"""
    return pd.to_numeric(
        series.astype(str).str.replace(r"[^0-9\.\-]", "", regex=True),
        errors="coerce",
    )


def load_all_results() -> pd.DataFrame:
    files = glob.glob(str(DATA_RAW_DIR / "result_*.csv"))
    if not files:
        raise FileNotFoundError(
            "data/raw/ に result_*.csv がありません。先に "
            "scraper/netkeiba_scraper.py --mode result を実行してください。"
        )
    dfs = [pd.read_csv(f, encoding="utf-8-sig") for f in files]
    return pd.concat(dfs, ignore_index=True)


def load_all_histories() -> pd.DataFrame:
    files = glob.glob(str(DATA_RAW_DIR / "horse_history_*.csv"))
    if not files:
        return pd.DataFrame()
    dfs = [pd.read_csv(f, encoding="utf-8-sig") for f in files]
    return pd.concat(dfs, ignore_index=True)


def build_features(results: pd.DataFrame, histories: pd.DataFrame) -> pd.DataFrame:
    df = results.copy()

    # 列名はnetkeibaのHTML構造次第で変わり得るため、存在チェックしながら処理
    rename_map = {
        "着順": "finish_pos",
        "枠番": "post_position",
        "馬番": "horse_number",
        "斤量": "weight_carried",
        "単勝": "win_odds",
        "人気": "popularity",
        "馬体重": "horse_weight_raw",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "finish_pos" in df.columns:
        df["finish_pos_num"] = _to_number(df["finish_pos"])
        df[TARGET_COL] = (df["finish_pos_num"] <= 3).astype(int)

    for col in ["weight_carried", "win_odds", "popularity", "post_position", "horse_number"]:
        if col in df.columns:
            df[col] = _to_number(df[col])

    # 馬体重は "480(+2)" のような形式 → 体重と増減を分離
    if "horse_weight_raw" in df.columns:
        weight = df["horse_weight_raw"].astype(str)
        df["horse_weight"] = weight.str.extract(r"(\d+)").astype(float)
        df["horse_weight_diff"] = weight.str.extract(r"\(([+\-]?\d+)\)").astype(float)

    # 過去成績集計（馬ごと）
    if not histories.empty and "horse_id" in histories.columns:
        hist_features = _aggregate_horse_history(histories)
        df = df.merge(hist_features, on="horse_id", how="left")

    return df


def _aggregate_horse_history(histories: pd.DataFrame) -> pd.DataFrame:
    """過去走から馬ごとの集計特徴量を作る"""
    h = histories.copy()
    finish_col = "着順" if "着順" in h.columns else None
    if finish_col is None:
        return pd.DataFrame(columns=["horse_id"])

    h["finish_num"] = _to_number(h[finish_col])

    agg = h.groupby("horse_id")["finish_num"].agg(
        avg_finish_pos="mean",
        n_races="count",
        best_finish_pos="min",
    ).reset_index()
    agg["place_rate"] = h.groupby("horse_id")["finish_num"].apply(
        lambda s: (s <= 3).mean()
    ).values

    # 直近3走の平均（現在の並びが新しい順という前提。異なる場合は要調整）
    recent3 = (
        h.sort_values(["horse_id"])
        .groupby("horse_id")
        .head(3)
        .groupby("horse_id")["finish_num"]
        .mean()
        .rename("recent3_avg_finish_pos")
        .reset_index()
    )
    agg = agg.merge(recent3, on="horse_id", how="left")
    return agg


FEATURE_COLS = [
    "post_position",
    "horse_number",
    "weight_carried",
    "win_odds",
    "popularity",
    "horse_weight",
    "horse_weight_diff",
    "avg_finish_pos",
    "n_races",
    "best_finish_pos",
    "place_rate",
    "recent3_avg_finish_pos",
]


def main():
    results = load_all_results()
    histories = load_all_histories()
    features = build_features(results, histories)

    out_path = DATA_PROCESSED_DIR / "features.csv"
    features.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"保存しました: {out_path} ({len(features)}行, {len(features.columns)}列)")
    present = [c for c in FEATURE_COLS if c in features.columns]
    missing = [c for c in FEATURE_COLS if c not in features.columns]
    print(f"利用可能な特徴量: {present}")
    if missing:
        print(f"欠けている特徴量（要データ拡充）: {missing}")


if __name__ == "__main__":
    main()
