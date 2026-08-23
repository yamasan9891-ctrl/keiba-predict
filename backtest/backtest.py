"""
バックテスト

「もし過去にEVルール（期待値100%超のみ購入）で単勝を買い続けていたら、
実際に儲かっていたか」を検証する。

やり方:
  1. レースを時系列で「学習期間」と「検証期間」に分割する
     （学習期間のデータだけでモデルを学習し、検証期間には一切使わない。
      未来の情報を学習に混ぜない＝リークを防ぐのが最重要）
  2. 学習済みモデルで、検証期間の各レース・各馬の勝率を予測する
     （現在のモデルは「複勝圏内(3着以内)確率」を学習しているため、
      単勝の勝率としては近似値として使う。将来的には勝率専用モデルの
      追加学習も検討の余地がある）
  3. 予測勝率 × 実際の単勝オッズ = EV を計算し、EVが閾値を超える馬にだけ
     「1点100円」を賭けたと仮定してシミュレーションする
  4. 実際の着順と比較して回収率（ROI）を計算する

比較対象（ベースライン）:
  - 全馬に満遍なく賭けた場合のROI（理論上、控除率の分だけマイナスになるはず。
    これが正しく再現されればシミュレーション自体の妥当性の裏付けになる）
  - 1番人気だけに賭け続けた場合のROI（よくある「素朴な戦略」との比較）

使い方:
  python backtest/backtest.py --ev-threshold 1.0 --test-ratio 0.3
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import TARGET_COL
from db.database import get_conn, init_db
from features.feature_engineering import load_race_entries, build_features, FEATURE_COLS, CATEGORICAL_COLS


def chronological_split(df: pd.DataFrame, test_ratio: float = 0.3):
    """
    race_idで時系列順に並べ、直近test_ratio分をテスト期間として切り出す。
    ※race_idは年が先頭にあるため年単位ではおおむね時系列だが、
      競馬場をまたぐ厳密な日付順ではない点に注意（今後race_dateの正確な
      取得ができ次第、そちらに切り替えるのが望ましい）。
    """
    race_ids_sorted = sorted(df["race_id"].unique())
    cutoff_idx = int(len(race_ids_sorted) * (1 - test_ratio))
    train_race_ids = set(race_ids_sorted[:cutoff_idx])
    test_race_ids = set(race_ids_sorted[cutoff_idx:])
    train_df = df[df["race_id"].isin(train_race_ids)].copy()
    test_df = df[df["race_id"].isin(test_race_ids)].copy()
    return train_df, test_df


def train_backtest_model(train_df: pd.DataFrame, target: str = TARGET_COL):
    cols = [c for c in FEATURE_COLS if c in train_df.columns]
    cat_features = [c for c in CATEGORICAL_COLS if c in cols]
    X = train_df[cols]
    y = train_df[target]

    model = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.05, num_leaves=15,
        min_child_samples=20, subsample=0.8, colsample_bytree=0.8, random_state=42,
        verbosity=-1,
    )
    model.fit(X, y, categorical_feature=cat_features)
    return model, cols


def simulate_tansho_strategy(test_df: pd.DataFrame, model, cols: list, ev_threshold: float = 1.0, use_win_prob_directly: bool = False) -> dict:
    """
    検証期間の各レースについて、予測確率×実オッズ(win_odds)でEVを計算し、
    EV>ev_thresholdの馬にのみ単勝100円を賭けたと仮定して集計する。

    use_win_prob_directly=True の場合、モデルの出力をレース内正規化せず
    そのまま「1着になる確率」として使う（is_win専用モデルの場合はこちら）。
    """
    X = test_df[cols]
    test_df = test_df.copy()
    test_df["pred_prob"] = model.predict_proba(X)[:, 1]

    results = {"ev_strategy": {"bets": 0, "staked": 0, "returned": 0},
               "bet_all": {"bets": 0, "staked": 0, "returned": 0},
               "favorite_only": {"bets": 0, "staked": 0, "returned": 0}}

    for race_id, race_df in test_df.groupby("race_id"):
        if use_win_prob_directly:
            win_prob_approx = race_df["pred_prob"]
        else:
            total_strength = race_df["pred_prob"].sum()
            if total_strength <= 0:
                continue
            win_prob_approx = race_df["pred_prob"] / total_strength

        for (_, row), win_p in zip(race_df.iterrows(), win_prob_approx):
            odds = row.get("win_odds")
            if pd.isna(odds) or odds <= 0:
                continue
            won = row["finish_pos"] == 1
            payout = odds * 100 if won else 0

            ev = win_p * odds
            if ev > ev_threshold:
                results["ev_strategy"]["bets"] += 1
                results["ev_strategy"]["staked"] += 100
                results["ev_strategy"]["returned"] += payout

            results["bet_all"]["bets"] += 1
            results["bet_all"]["staked"] += 100
            results["bet_all"]["returned"] += payout

            if row.get("popularity") == 1:
                results["favorite_only"]["bets"] += 1
                results["favorite_only"]["staked"] += 100
                results["favorite_only"]["returned"] += payout

    for strategy, r in results.items():
        r["roi"] = (r["returned"] / r["staked"] - 1) * 100 if r["staked"] > 0 else None

    return results


def print_results(results: dict):
    labels = {
        "ev_strategy": "① EVルール戦略（期待値100%超のみ購入）",
        "bet_all": "② 全馬にベタ買い（理論上マイナスになるはずの妥当性チェック用）",
        "favorite_only": "③ 1番人気だけ単勝（素朴な戦略との比較）",
    }
    print("\n=== バックテスト結果 ===")
    for key, label in labels.items():
        r = results[key]
        roi_str = f"{r['roi']:.1f}%" if r["roi"] is not None else "N/A（賭けなし）"
        print(f"{label}")
        print(f"  賭けた回数: {r['bets']}回 / 投資額: {r['staked']}円 / 払戻: {r['returned']:.0f}円 / 回収率: {roi_str}")
        print()


def run_backtest(ev_threshold: float = 1.0, test_ratio: float = 0.3, target: str = "is_win"):
    init_db()
    with get_conn() as conn:
        raw = load_race_entries(conn)

    if raw.empty or raw["race_id"].nunique() < 20:
        print("バックテストには十分なレース数がありません（最低でも数十レース必要）。もう少しデータが貯まってから実行してください。")
        return

    features = build_features(raw)
    features["is_win"] = (features["finish_pos"] == 1).astype(int)
    train_df, test_df = chronological_split(features, test_ratio=test_ratio)

    print(f"学習期間: {train_df['race_id'].nunique()}レース / 検証期間: {test_df['race_id'].nunique()}レース")
    print(f"予測対象: {'1着になる確率(is_win)専用モデル' if target == 'is_win' else '複勝圏内確率(top3)の代用'}")

    if train_df.empty or test_df.empty:
        print("学習期間または検証期間のデータが不足しています。")
        return

    model, cols = train_backtest_model(train_df, target=target)
    results = simulate_tansho_strategy(test_df, model, cols, ev_threshold=ev_threshold,
                                        use_win_prob_directly=(target == "is_win"))
    print_results(results)


def main():
    parser = argparse.ArgumentParser(description="バックテスト")
    parser.add_argument("--ev-threshold", type=float, default=1.0, help="EV閾値（1.0=100%）")
    parser.add_argument("--test-ratio", type=float, default=0.3, help="検証期間の割合")
    parser.add_argument("--target", choices=["is_win", "is_placed"], default="is_win",
                         help="is_win: 単勝専用(1着確率) / is_placed: 複勝圏内確率を代用")
    args = parser.parse_args()
    run_backtest(ev_threshold=args.ev_threshold, test_ratio=args.test_ratio, target=args.target)


if __name__ == "__main__":
    main()
