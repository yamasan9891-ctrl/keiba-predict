"""
特徴量エンジニアリング（DB直結版）

data/keiba.db から races / entries / horses を結合して読み込み、
モデル学習・推論に使える特徴量テーブルを作る。

【重要】以前はdata/raw/のCSVファイルを読む設計だったが、実際の収集データは
すべてSQLite DB（data/keiba.db）に保存されるようになったため、こちらに一本化した。

特徴量:
  - レース属性: 距離, 芝/ダート, 馬場状態, ハンデ戦か
  - 出走馬属性: 斤量, 馬体重(増減), 枠番, 馬番, 脚質, 上がり3F
  - 市場評価: 単勝人気, 単勝オッズ（市場がどう評価しているかも重要な特徴量）
  - 血統: 父, 母父（BMS）※カテゴリ変数としてLightGBMにそのまま渡す
  - 過去成績（対象レースより前の情報のみを使用し、リーク防止）:
      直近平均着順, 複勝率, 出走数, 直近3走平均着順
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_PROCESSED_DIR, TARGET_COL
from db.database import get_conn, init_db


def load_race_entries(conn) -> pd.DataFrame:
    """races + entries + horses を結合した生データを読み込む"""
    query = """
        SELECT
            e.race_id, e.horse_number, e.post_position, e.horse_id, e.horse_name,
            e.jockey_id, e.weight_carried, e.horse_weight, e.horse_weight_diff,
            e.running_style, e.last_3f, e.finish_pos, e.win_odds, e.popularity, e.is_placed,
            r.distance, r.surface, r.track_condition, r.weather, r.is_handicap, r.grade,
            h.father, h.mother_father
        FROM entries e
        LEFT JOIN races r ON e.race_id = r.race_id
        LEFT JOIN horses h ON e.horse_id = h.horse_id
        WHERE e.finish_pos IS NOT NULL
    """
    return pd.read_sql_query(query, conn)


def _add_horse_history_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    馬ごとの過去成績を、リーク防止のため「そのレースより前の情報だけ」で計算する。
    """
    df = df.sort_values(["horse_id", "race_id"]).reset_index(drop=True)

    def _expanding_stats(group: pd.DataFrame) -> pd.DataFrame:
        finish = group["finish_pos"].astype(float)
        prior = finish.shift(1)
        group["avg_finish_pos"] = prior.expanding().mean()
        group["n_races"] = prior.expanding().count()
        group["best_finish_pos"] = prior.expanding().min()
        group["place_rate"] = prior.expanding().apply(lambda s: (s <= 3).mean() if len(s) else np.nan)
        group["recent3_avg_finish_pos"] = prior.rolling(3, min_periods=1).mean()
        # 上がり3Fは「そのレース自体の結果」なので予想時点では存在しない（リークになる）。
        # 代わりに「過去レースの上がり3F平均」を使う（これなら予想時点でも計算可能）。
        prior_3f = group["last_3f"].shift(1)
        group["avg_last_3f"] = prior_3f.expanding().mean()
        # running_styleも「そのレースの実際の通過順位」から算出されたものなのでリークになる。
        # 代わりに「直前のレースでの脚質」を、その馬の傾向の目安として使う。
        group["prior_running_style"] = group["running_style"].shift(1)
        return group

    df = df.groupby("horse_id", group_keys=False).apply(_expanding_stats)
    return df


CATEGORICAL_COLS = ["prior_running_style", "surface", "track_condition", "father", "mother_father"]

FEATURE_COLS = [
    "post_position", "horse_number", "weight_carried", "win_odds", "popularity",
    "horse_weight", "horse_weight_diff",
    "distance", "is_handicap",
    "avg_finish_pos", "n_races", "best_finish_pos", "place_rate", "recent3_avg_finish_pos",
    "avg_last_3f",
] + CATEGORICAL_COLS


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df[TARGET_COL] = (df["finish_pos"] <= 3).astype(int)
    df["is_handicap"] = df["is_handicap"].fillna(0).astype(int)

    df = _add_horse_history_features(df)

    for col in CATEGORICAL_COLS:
        if col in df.columns:
            df[col] = df[col].astype("category")

    return df


def main():
    init_db()
    with get_conn() as conn:
        raw = load_race_entries(conn)

    if raw.empty:
        print("DBに確定済みレースデータがありません。先にscraperでデータ収集してください。")
        return

    features = build_features(raw)

    out_path = DATA_PROCESSED_DIR / "features.csv"
    features.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"保存しました: {out_path} ({len(features)}行, {len(features.columns)}列)")

    present = [c for c in FEATURE_COLS if c in features.columns]
    print(f"利用可能な特徴量({len(present)}個): {present}")


if __name__ == "__main__":
    main()
