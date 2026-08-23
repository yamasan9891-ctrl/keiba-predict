"""
モデル学習

data/processed/features.csv を使って「複勝圏内（3着以内）に入る確率」を
予測するLightGBM二値分類モデルを学習し、model/artifacts/ に保存する。

十分な量の過去レースデータ（できれば数百〜数千レース分）を
scraper で収集してから実行してください。データが少なすぎると
過学習し、実戦では役に立ちません。
"""
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, log_loss
from sklearn.model_selection import GroupKFold

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_PROCESSED_DIR, MODEL_DIR, TARGET_COL
from features.feature_engineering import FEATURE_COLS, CATEGORICAL_COLS


def load_features() -> pd.DataFrame:
    path = DATA_PROCESSED_DIR / "features.csv"
    if not path.exists():
        raise FileNotFoundError(
            "features.csv がありません。先に features/feature_engineering.py を実行してください。"
        )
    df = pd.read_csv(path, encoding="utf-8-sig")
    # CSV保存でcategory型は失われる（文字列に戻る）ため、学習前に再度category型へ戻す
    # （LightGBMはcategory型の列をそのままカテゴリ特徴量として扱える）
    for col in CATEGORICAL_COLS:
        if col in df.columns:
            df[col] = df[col].astype("category")
    return df


def train():
    df = load_features()
    if TARGET_COL not in df.columns:
        raise ValueError(
            f"{TARGET_COL} 列がありません。学習には確定済みレース結果（--mode result）"
            "から作った特徴量が必要です。"
        )

    cols = [c for c in FEATURE_COLS if c in df.columns]
    if len(cols) < 3:
        raise ValueError(
            f"利用可能な特徴量が少なすぎます({cols})。データ収集を増やしてください。"
        )

    df = df.dropna(subset=[TARGET_COL])
    X = df[cols]
    y = df[TARGET_COL]
    groups = df["race_id"] if "race_id" in df.columns else np.arange(len(df))
    cat_features = [c for c in CATEGORICAL_COLS if c in cols]

    # レース単位でCVを分割する（同じレースの馬が train/valid に分かれて
    # リークするのを防ぐため）
    gkf = GroupKFold(n_splits=min(5, df["race_id"].nunique()) if "race_id" in df.columns else 5)

    aucs, loglosses = [], []
    models = []
    for fold, (tr_idx, va_idx) in enumerate(gkf.split(X, y, groups)):
        X_tr, X_va = X.iloc[tr_idx], X.iloc[va_idx]
        y_tr, y_va = y.iloc[tr_idx], y.iloc[va_idx]

        model = lgb.LGBMClassifier(
            n_estimators=500,
            learning_rate=0.03,
            num_leaves=15,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
        )
        model.fit(
            X_tr, y_tr,
            eval_set=[(X_va, y_va)],
            categorical_feature=cat_features,
            callbacks=[lgb.early_stopping(30, verbose=False)],
        )
        pred = model.predict_proba(X_va)[:, 1]
        auc = roc_auc_score(y_va, pred)
        ll = log_loss(y_va, pred)
        aucs.append(auc)
        loglosses.append(ll)
        models.append(model)
        print(f"fold {fold}: AUC={auc:.3f} logloss={ll:.3f}")

    print(f"平均 AUC={np.mean(aucs):.3f} / logloss={np.mean(loglosses):.3f}")

    # 最終モデルは全データで再学習
    final_model = lgb.LGBMClassifier(
        n_estimators=int(np.mean([m.best_iteration_ or 500 for m in models])),
        learning_rate=0.03,
        num_leaves=15,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
    )
    final_model.fit(X, y, categorical_feature=cat_features)

    joblib.dump({"model": final_model, "features": cols}, MODEL_DIR / "model.pkl")
    print(f"モデルを保存しました: {MODEL_DIR / 'model.pkl'}")

    importance = pd.Series(
        final_model.feature_importances_, index=cols
    ).sort_values(ascending=False)
    print("特徴量重要度:")
    print(importance)


if __name__ == "__main__":
    train()
