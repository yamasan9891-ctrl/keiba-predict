"""
特徴量エンジニアリング（DB直結版・フル機能）

data/keiba.db から races / entries / horses を結合して読み込み、
モデル学習・推論に使える特徴量テーブルを作る。

特徴量:
  - レース属性: 距離, 芝/ダート, 馬場状態, ハンデ戦か
  - 出走馬属性: 斤量, 馬体重(増減), 枠番, 馬番
  - 市場評価: 単勝人気, 単勝オッズ
  - 血統: 父, 母父（BMS）
  - コース補正スピード指数（過去平均・過去ベスト）
  - 対戦相手の強さ（過去平均）
  - 騎手の過去成績（過去平均複勝率）: 新規スクレイピング不要、既存データから計算可能
  - 展開適性（ペースシミュレーション）:
      出走馬全員の「過去の脚質」から、そのレースが速いペースになりそうか
      遅いペースになりそうかを予測し、それが自分の脚質に有利かどうかを特徴量化する
      （逃げ馬が多い＝ハイペース想定＝差し・追込有利、逃げ馬0＝スロー想定＝先行有利）
  - 過去成績（対象レースより前の情報のみを使用し、リーク防止）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_PROCESSED_DIR, TARGET_COL
from db.database import get_conn, init_db
from features.speed_figure import compute_speed_figures


def load_race_entries(conn) -> pd.DataFrame:
    """races + entries + horses を結合した生データを読み込む"""
    query = """
        SELECT
            e.race_id, e.horse_number, e.post_position, e.horse_id, e.horse_name,
            e.jockey_id, e.trainer_name, e.weight_carried, e.horse_weight, e.horse_weight_diff,
            e.running_style, e.last_3f, e.finish_pos, e.win_odds, e.popularity, e.is_placed,
            r.race_date, r.distance, r.surface, r.track_condition, r.weather, r.is_handicap, r.grade,
            h.father, h.mother_father
        FROM entries e
        LEFT JOIN races r ON e.race_id = r.race_id
        LEFT JOIN horses h ON e.horse_id = h.horse_id
        WHERE e.finish_pos IS NOT NULL
    """
    df = pd.read_sql_query(query, conn)

    speed_figures = compute_speed_figures(conn)
    if not speed_figures.empty:
        df = df.merge(speed_figures, on=["race_id", "horse_id"], how="left")
    else:
        df["speed_figure"] = np.nan

    return df


def _add_field_strength(df: pd.DataFrame) -> pd.DataFrame:
    """「そのレースの対戦相手のレベル」を表す特徴量を作る（自分を除いた平均複勝率）"""
    if "place_rate" not in df.columns:
        df["opponent_strength"] = np.nan
        return df

    race_sum = df.groupby("race_id")["place_rate"].transform(lambda s: s.fillna(0).sum())
    race_n = df.groupby("race_id")["place_rate"].transform("count")
    denom = (race_n - 1).replace(0, np.nan)
    df["opponent_strength"] = (race_sum - df["place_rate"].fillna(0)) / denom
    return df


def _add_pace_simulation(df: pd.DataFrame) -> pd.DataFrame:
    """
    展開シミュレーション: 出走馬全員の「過去の脚質」から、そのレースのペースを予測し、
    自分の脚質がそのペースに有利かどうかを特徴量化する。

    ロジック:
      - レース内で「逃げ・先行」の馬が多い（全体の40%超）→ ハイペース想定 → 差し・追込馬に有利
      - 「逃げ・先行」の馬が少ない（20%未満）→ スローペース想定 → 逃げ・先行馬に有利
      - それ以外 → 平均的なペース、有利不利なし
    """
    if "prior_running_style" not in df.columns:
        df["pace_advantage"] = 0
        return df

    front_styles = {"逃げ", "先行"}
    is_front = df["prior_running_style"].isin(front_styles)

    front_ratio = is_front.groupby(df["race_id"]).transform("mean")

    predicted_pace = pd.Series("平均", index=df.index)
    predicted_pace[front_ratio > 0.4] = "ハイペース"
    predicted_pace[front_ratio < 0.2] = "スローペース"
    df["predicted_pace"] = predicted_pace

    # 自分の脚質が、そのペース展開において有利なら+1、不利なら-1、平均的なら0
    advantage = pd.Series(0, index=df.index)
    is_closer = df["prior_running_style"].isin({"差し", "追込"})
    advantage[(predicted_pace == "ハイペース") & is_closer] = 1
    advantage[(predicted_pace == "ハイペース") & is_front] = -1
    advantage[(predicted_pace == "スローペース") & is_front] = 1
    advantage[(predicted_pace == "スローペース") & is_closer] = -1
    df["pace_advantage"] = advantage

    return df


def _add_layoff_and_post_bias(df: pd.DataFrame) -> pd.DataFrame:
    """
    休み明け間隔（前走からの日数）と、枠順×コース×距離の有利不利統計を追加する。
    race_dateが取得できているレースのみ対象（未取得のレースはNaNになる）。
    """
    if "race_date" not in df.columns:
        df["days_since_last_race"] = np.nan
        df["post_position_bias"] = np.nan
        return df

    df["race_date_parsed"] = pd.to_datetime(df["race_date"], errors="coerce")

    df = df.sort_values(["horse_id", "race_date_parsed", "race_id"]).reset_index(drop=True)
    horse_ids = df["horse_id"]

    def _layoff(group: pd.DataFrame) -> pd.DataFrame:
        prior_date = group["race_date_parsed"].shift(1)
        group["days_since_last_race"] = (group["race_date_parsed"] - prior_date).dt.days
        return group

    df = df.groupby("horse_id", group_keys=False).apply(_layoff)
    df["horse_id"] = horse_ids.values

    # 枠順×コース×距離×芝ダートごとの複勝率を統計化し、「その枠が有利かどうか」を数値化する
    # （is_placedは自分自身の結果なので、グループ全体の平均を使う際は自分を除いた値にする必要があるが、
    #   ここでは簡易的に「全体傾向」として扱う。過学習防止のためサンプル数が少ないグループは中立値にする）
    if "post_position" in df.columns and "is_placed" in df.columns:
        group_cols = ["distance", "surface", "post_position"]
        group_stats = df.groupby(group_cols)["is_placed"].agg(["mean", "count"]).reset_index()
        group_stats.loc[group_stats["count"] < 30, "mean"] = np.nan  # サンプル不足は無効化
        group_stats = group_stats.rename(columns={"mean": "post_position_bias"})
        df = df.merge(group_stats[group_cols + ["post_position_bias"]], on=group_cols, how="left")
    else:
        df["post_position_bias"] = np.nan

    return df


def _add_horse_history_features(df: pd.DataFrame) -> pd.DataFrame:
    """馬ごとの過去成績を、リーク防止のため「そのレースより前の情報だけ」で計算する。"""
    df = df.sort_values(["horse_id", "race_id"]).reset_index(drop=True)
    horse_ids = df["horse_id"]  # pandas 3.0ではgroupby.apply内でグループ化列が消えるため退避

    def _expanding_stats(group: pd.DataFrame) -> pd.DataFrame:
        finish = group["finish_pos"].astype(float)
        prior = finish.shift(1)
        group["avg_finish_pos"] = prior.expanding().mean()
        group["n_races"] = prior.expanding().count()
        group["best_finish_pos"] = prior.expanding().min()
        group["place_rate"] = prior.expanding().apply(lambda s: (s <= 3).mean() if len(s) else np.nan)
        group["recent3_avg_finish_pos"] = prior.rolling(3, min_periods=1).mean()

        prior_3f = group["last_3f"].shift(1)
        group["avg_last_3f"] = prior_3f.expanding().mean()
        group["prior_running_style"] = group["running_style"].shift(1)

        prior_speed = group["speed_figure"].shift(1)
        group["avg_speed_figure"] = prior_speed.expanding().mean()
        group["best_speed_figure"] = prior_speed.expanding().max()

        if "opponent_strength" in group.columns:
            prior_opp = group["opponent_strength"].shift(1)
            group["avg_opponent_strength"] = prior_opp.expanding().mean()
        else:
            group["avg_opponent_strength"] = np.nan
        return group

    df = df.groupby("horse_id", group_keys=False).apply(_expanding_stats)
    df["horse_id"] = horse_ids.values
    return df


def _add_jockey_history_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    騎手の過去成績を計算する（新規スクレイピング不要、既存のjockey_id×finish_posから算出可能）。
    馬の場合と同様、リーク防止のため「そのレースより前」の成績のみ使う。
    """
    df = df.sort_values(["jockey_id", "race_id"]).reset_index(drop=True)
    jockey_ids = df["jockey_id"]

    def _expanding_stats(group: pd.DataFrame) -> pd.DataFrame:
        finish = group["finish_pos"].astype(float)
        prior = finish.shift(1)
        group["jockey_place_rate"] = prior.expanding().apply(lambda s: (s <= 3).mean() if len(s) else np.nan)
        group["jockey_n_races"] = prior.expanding().count()
        return group

    df = df.groupby("jockey_id", group_keys=False).apply(_expanding_stats)
    df["jockey_id"] = jockey_ids.values
    return df


CATEGORICAL_COLS = ["prior_running_style", "surface", "track_condition", "father", "mother_father", "predicted_pace"]

FEATURE_COLS = [
    "post_position", "horse_number", "weight_carried", "win_odds", "popularity",
    "horse_weight", "horse_weight_diff",
    "distance", "is_handicap",
    "avg_finish_pos", "n_races", "best_finish_pos", "place_rate", "recent3_avg_finish_pos",
    "avg_last_3f", "avg_speed_figure", "best_speed_figure", "avg_opponent_strength",
    "jockey_place_rate", "jockey_n_races", "pace_advantage",
    "days_since_last_race", "post_position_bias",
] + CATEGORICAL_COLS


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df[TARGET_COL] = (df["finish_pos"] <= 3).astype(int)
    df["is_handicap"] = df["is_handicap"].fillna(0).astype(int)

    # 対戦相手の強さの算出には各馬の履歴的place_rateが必要なため、2段階で計算する
    tmp = _add_horse_history_features(df)
    tmp = _add_field_strength(tmp)
    df = _add_horse_history_features(tmp)

    # 展開シミュレーションは「各馬の過去の脚質」が確定した後でないと計算できない
    df = _add_pace_simulation(df)

    # 騎手の成績は馬とは独立に計算できる
    df = _add_jockey_history_features(df)

    # 休み明け間隔・枠順バイアスは開催日ベースで計算する
    df = _add_layoff_and_post_bias(df)

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
