"""
スピード指数（コース補正タイム）

同じ「1600m」でも競馬場・馬場状態によってタイムの出やすさが全く違うため、
単純なタイム比較は意味がない。
そこで「同じコース・距離・馬場状態のレース群の中で、平均よりどれだけ
速かったか（標準偏差換算）」を指数化する。これにより、
「東京の1600m稍重」と「中山の1600m良」のようなタイムも公平に比較できる。

計算式（一般的なスピード指数の考え方を採用）:
  speed_figure = 50 + (グループ平均タイム - 走破タイム) / グループ標準偏差 × 10
  → 平均的なら50、速いほど高く、遅いほど低くなる
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db


def compute_course_group_stats(conn) -> pd.DataFrame:
    """競馬場×距離×芝ダート×馬場状態ごとの、走破タイムの平均・標準偏差を計算する"""
    query = """
        SELECT
            r.race_id, substr(r.race_id,5,2) as course_code, r.distance, r.surface, r.track_condition,
            e.finish_time
        FROM entries e
        JOIN races r ON e.race_id = r.race_id
        WHERE e.finish_time IS NOT NULL AND r.distance IS NOT NULL AND r.surface IS NOT NULL
    """
    df = pd.read_sql_query(query, conn)
    if df.empty:
        return pd.DataFrame()

    df["track_condition"] = df["track_condition"].fillna("不明")
    stats = df.groupby(["course_code", "distance", "surface", "track_condition"])["finish_time"].agg(
        ["mean", "std", "count"]
    ).reset_index()
    stats = stats[stats["count"] >= 5]  # サンプル数が少なすぎるグループは信頼できないため除外
    return stats


def compute_speed_figures(conn) -> pd.DataFrame:
    """
    全entriesに対してスピード指数を計算して返す。
    戻り値: race_id, horse_id, speed_figure の3列
    """
    group_stats = compute_course_group_stats(conn)
    if group_stats.empty:
        return pd.DataFrame(columns=["race_id", "horse_id", "speed_figure"])

    query = """
        SELECT
            e.race_id, e.horse_id, substr(r.race_id,5,2) as course_code,
            r.distance, r.surface, r.track_condition, e.finish_time
        FROM entries e
        JOIN races r ON e.race_id = r.race_id
        WHERE e.finish_time IS NOT NULL AND e.horse_id IS NOT NULL
    """
    df = pd.read_sql_query(query, conn)
    df["track_condition"] = df["track_condition"].fillna("不明")

    merged = df.merge(group_stats, on=["course_code", "distance", "surface", "track_condition"], how="inner")
    merged = merged[merged["std"] > 0]
    merged["speed_figure"] = 50 + (merged["mean"] - merged["finish_time"]) / merged["std"] * 10

    return merged[["race_id", "horse_id", "speed_figure"]]


def main():
    init_db()
    with get_conn() as conn:
        figures = compute_speed_figures(conn)
    if figures.empty:
        print("スピード指数を計算できるデータがまだありません（finish_timeが空、またはグループのサンプル数不足）。")
        return
    print(f"{len(figures)}件のスピード指数を計算しました")
    print(figures.describe())


if __name__ == "__main__":
    main()
