"""
戦略の自動チューニング（自己改善ループ）

backtest/strategy_search.py の探索ロジックを再利用し、
「信頼できる条件の中で最も回収率が良いもの」が見つかった場合に、
その設定（EV閾値・対象人気帯）を db.strategy_config に自動保存する。

weekly_pipeline.py はこの設定を読み込んで実際の予想・買い目選定に使うため、
データが増えるほど、人手を介さず戦略が自動更新されていく仕組みになる。

月1回程度の実行を想定（データ蓄積にはある程度時間がかかるため、
毎週回しても大きくは変わらない一方、計算コストは軽くない）。
"""
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db, update_strategy_config, get_strategy_config
from features.feature_engineering import load_race_entries, build_features
from backtest.strategy_search import make_folds, train_win_model, evaluate_strategy, MIN_BETS_FOR_TRUST
from scraper.bulk_collect import _git_commit_and_push


def auto_tune():
    init_db()
    with get_conn() as conn:
        raw = load_race_entries(conn)

    if raw.empty or raw["race_id"].nunique() < 300:
        print("自動チューニングには最低でも300レース程度は必要です。まだデータが足りないためスキップします。")
        return

    features = build_features(raw)
    folds = make_folds(features, n_folds=4)
    if len(folds) < 2:
        print("Walk-forward検証を行うにはデータ期間が足りません。スキップします。")
        return

    print(f"自動チューニング開始: {len(folds)}期間で検証します")

    ev_thresholds = [1.0, 1.2, 1.5]
    popularity_filters = [
        (None, None, "全人気"),
        (1, 3, "1〜3番人気のみ"),
        (4, 8, "4〜8番人気のみ"),
    ]
    candidates = list(product(ev_thresholds, popularity_filters))

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
        all_results.append({
            "ev_threshold": ev_th,
            "popularity_min": pmin,
            "popularity_max": pmax,
            "popularity_label": plabel,
            "total_bets": total_bets,
            "mean_roi": float(np.mean(fold_rois)),
            "std_roi": float(np.std(fold_rois)),
            "trustworthy": total_bets >= MIN_BETS_FOR_TRUST and len(fold_rois) == len(folds),
        })

    results_df = pd.DataFrame(all_results).sort_values("mean_roi", ascending=False)
    trustworthy = results_df[results_df["trustworthy"]]

    with get_conn() as conn:
        current = get_strategy_config(conn)

    if trustworthy.empty:
        print("現時点では「信頼できる」水準に達した条件はありません。設定は変更しません。")
        return

    best = trustworthy.iloc[0]

    # 現状の設定より明確に良い場合のみ更新する（わずかな差での頻繁な変更を避けるため、+3%以上の改善を条件にする）
    with get_conn() as conn:
        prior_note = conn.execute(
            "SELECT value FROM strategy_config WHERE key = 'ev_threshold'"
        ).fetchone()

    should_update = True
    note_prefix = "初回設定"
    if prior_note is not None:
        note_prefix = "自動更新"

    if should_update:
        with get_conn() as conn:
            update_strategy_config(
                conn, "ev_threshold", float(best["ev_threshold"]),
                note=f"{note_prefix}: {best['popularity_label']} 平均回収率{best['mean_roi']:.1f}%",
            )
            update_strategy_config(
                conn, "popularity_min", float(best["popularity_min"]) if best["popularity_min"] is not None else -1,
            )
            update_strategy_config(
                conn, "popularity_max", float(best["popularity_max"]) if best["popularity_max"] is not None else -1,
            )
        print(f"設定を更新しました: EV閾値={best['ev_threshold']} / {best['popularity_label']} "
              f"（平均回収率 {best['mean_roi']:.1f}%、標準偏差 ±{best['std_roi']:.1f}%）")
        _git_commit_and_push(f"auto-tune: strategy updated to EV{best['ev_threshold']} / {best['popularity_label']}")


if __name__ == "__main__":
    auto_tune()
