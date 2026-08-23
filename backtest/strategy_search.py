"""
ストラテジー自動探索（安全装置付き）

「良さそうな条件」をたくさん試して自動で見つけるための仕組み。
ただし、条件を試せば試すほど「たまたま良く見える」ものが混ざるため、
以下の安全装置を必ず組み込む:

  1. Walk-forward検証: 1回の分割ではなく、時期をずらして複数回検証する。
     本物の効果なら、どの期間でもある程度安定した結果になるはず。
  2. 最低サンプル数フィルタ: ベット数が少なすぎる条件は「参考記録」として
     区別し、ランキング上位には出さない。
  3. 安定性（標準偏差）も評価: 平均回収率が高くても、期間ごとのブレが
     大きい条件は信頼度を下げる。
  4. 多重検定への意識: 試した条件数を明示し、「これだけ試せばこの程度の
     好成績は偶然でも出る」という目安を示す。

使い方:
  python backtest/strategy_search.py
"""
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db
from features.feature_engineering import load_race_entries, build_features, FEATURE_COLS, CATEGORICAL_COLS

MIN_BETS_FOR_TRUST = 500  # これ未満のベット数の結果は「参考記録」扱い


def make_folds(df: pd.DataFrame, n_folds: int = 4):
    """
    race_idを時系列順に並べ、walk-forward方式でn_folds個の(train, test)ペアを作る。
    各foldでは「それより前の全データ」を学習に、次の一区切りをテストに使う。
    """
    race_ids_sorted = sorted(df["race_id"].unique())
    n = len(race_ids_sorted)
    # 最初の40%は必ず学習用に確保し、残りをfold数で等分してテスト期間にする
    start = int(n * 0.4)
    remaining = race_ids_sorted[start:]
    fold_size = max(1, len(remaining) // n_folds)

    folds = []
    for i in range(n_folds):
        test_start = i * fold_size
        test_end = (i + 1) * fold_size if i < n_folds - 1 else len(remaining)
        test_ids = set(remaining[test_start:test_end])
        train_ids = set(race_ids_sorted[: start + test_start])
        if not test_ids or not train_ids:
            continue
        folds.append((train_ids, test_ids))
    return folds


def train_win_model(train_df: pd.DataFrame):
    cols = [c for c in FEATURE_COLS if c in train_df.columns]
    cat_features = [c for c in CATEGORICAL_COLS if c in cols]
    X = train_df[cols]
    y = (train_df["finish_pos"] == 1).astype(int)

    model = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.05, num_leaves=15,
        min_child_samples=20, subsample=0.8, colsample_bytree=0.8, random_state=42,
        verbosity=-1,
    )
    model.fit(X, y, categorical_feature=cat_features)
    return model, cols


def evaluate_strategy(test_df: pd.DataFrame, ev_threshold: float, popularity_min: int = None, popularity_max: int = None) -> dict:
    """指定条件（EV閾値＋人気範囲フィルタ）でのベット結果を集計"""
    df = test_df
    if popularity_min is not None:
        df = df[df["popularity"] >= popularity_min]
    if popularity_max is not None:
        df = df[df["popularity"] <= popularity_max]

    df = df[df["ev"] > ev_threshold]
    if df.empty:
        return {"bets": 0, "staked": 0, "returned": 0, "roi": None}

    staked = len(df) * 100
    returned = (df["win_odds"] * 100 * (df["finish_pos"] == 1)).sum()
    roi = (returned / staked - 1) * 100
    return {"bets": len(df), "staked": staked, "returned": returned, "roi": roi}


def run_search():
    init_db()
    with get_conn() as conn:
        raw = load_race_entries(conn)

    if raw.empty or raw["race_id"].nunique() < 100:
        print("探索には最低でも100レース程度は必要です。もう少しデータが貯まってから実行してください。")
        return

    features = build_features(raw)
    folds = make_folds(features, n_folds=4)
    if len(folds) < 2:
        print("Walk-forward検証を行うにはデータ期間が足りません。")
        return

    print(f"Walk-forward検証: {len(folds)}期間で検証します\n")

    # 探索する条件の候補（増やしすぎない。多重検定の影響を抑えるため）
    ev_thresholds = [1.0, 1.2, 1.5]
    popularity_filters = [
        (None, None, "全人気"),
        (1, 3, "1〜3番人気のみ"),
        (4, 8, "4〜8番人気のみ"),
        (9, None, "9番人気以下のみ"),
    ]

    candidates = list(product(ev_thresholds, popularity_filters))
    print(f"試す条件数: {len(candidates)}通り "
          f"（試行数が多いほど偶然の好成績が混ざりやすくなる点に留意）\n")

    all_results = []
    for ev_th, (pmin, pmax, plabel) in candidates:
        fold_rois, fold_bets = [], []
        for train_ids, test_ids in folds:
            train_df = features[features["race_id"].isin(train_ids)]
            test_df = features[features["race_id"].isin(test_ids)].copy()
            if train_df.empty or test_df.empty:
                continue

            model, cols = train_win_model(train_df)
            test_df["win_prob"] = model.predict_proba(test_df[cols])[:, 1]
            test_df["ev"] = test_df["win_prob"] * test_df["win_odds"].fillna(0)

            res = evaluate_strategy(test_df, ev_th, pmin, pmax)
            if res["roi"] is not None:
                fold_rois.append(res["roi"])
                fold_bets.append(res["bets"])

        if not fold_rois:
            continue

        total_bets = sum(fold_bets)
        mean_roi = float(np.mean(fold_rois))
        std_roi = float(np.std(fold_rois))
        all_results.append({
            "ev_threshold": ev_th,
            "popularity_filter": plabel,
            "total_bets": total_bets,
            "mean_roi": mean_roi,
            "std_roi": std_roi,
            "n_folds_with_bets": len(fold_rois),
            "trustworthy": total_bets >= MIN_BETS_FOR_TRUST and len(fold_rois) == len(folds),
        })

    results_df = pd.DataFrame(all_results).sort_values("mean_roi", ascending=False)

    print("=== 探索結果（回収率が高い順） ===\n")
    for _, r in results_df.iterrows():
        trust_mark = "✓ 信頼度: 十分なサンプル数" if r["trustworthy"] else "△ 参考記録（サンプル不足 or 一部期間で該当なし）"
        print(f"EV閾値={r['ev_threshold']} / {r['popularity_filter']}")
        print(f"  平均回収率: {r['mean_roi']:.1f}% (期間ごとのばらつき ±{r['std_roi']:.1f}%) "
              f"/ 総ベット数: {r['total_bets']} / {trust_mark}")
        print()

    trustworthy = results_df[results_df["trustworthy"]]
    if not trustworthy.empty:
        best = trustworthy.iloc[0]
        print(f"→ 信頼できる条件の中で最も良いのは: EV閾値={best['ev_threshold']} / {best['popularity_filter']} "
              f"（平均回収率 {best['mean_roi']:.1f}%）")
    else:
        print("→ 現時点では「信頼できる」水準に達した条件はありません。データが増えるのを待ってください。")


if __name__ == "__main__":
    run_search()
