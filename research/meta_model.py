"""Meta-labeling ("weight system") study.

Train on one year of setups, test on the other (both directions). Reports whether the model's
probability ranks setups by real outcome, and what the expectancy of the selected setups is.

    python -m research.meta_model
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from research.download import OUT

SPLIT = pd.Timestamp("2025-09-30", tz="UTC")
DROP = {"id", "symbol", "side", "created_at", "poi", "swept", "r_net", "r_gross", "fee_r", "exit", "held_bars",
        "mfe_r", "tp_r_setup", "y"}


def prepare_xy(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    X = df.drop(columns=[c for c in df.columns if c in DROP]).copy()
    X["is_long"] = (df["side"] == "long").astype(int)
    for p in ("FVG", "FVG+OB", "OB"):
        X[f"poi_{p}"] = (df["poi"] == p).astype(int)
    for c in X.columns:
        if X[c].dtype == bool:
            X[c] = X[c].astype(int)
    X = X.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return X, (df["r_net"] > 0).astype(int)


def models():
    return {
        "logistic": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=0.05, max_iter=2000)),
        "gbm": HistGradientBoostingClassifier(max_depth=3, min_samples_leaf=100, learning_rate=0.04, max_iter=250,
                                              l2_regularization=1.0, early_stopping=True, validation_fraction=0.2,
                                              random_state=0),
    }


def evaluate(p: np.ndarray, test: pd.DataFrame) -> dict:
    t = test.assign(p=p)
    out = {"auc": round(roc_auc_score(t.r_net > 0, p), 3), "all_setups_R": round(t.r_net.mean(), 3), "n": len(t)}
    for q in (0.5, 0.3, 0.2, 0.1):
        cut = t.p.quantile(1 - q)
        sel = t[t.p >= cut]
        out[f"top{int(q * 100)}%_R"] = round(sel.r_net.mean(), 3)
    t["decile"] = pd.qcut(t.p.rank(method="first"), 5, labels=False)
    out["quintiles_R"] = t.groupby("decile").r_net.mean().round(3).tolist()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(OUT / "setups.pkl"))
    a = ap.parse_args()
    df = pd.read_pickle(a.data).sort_values("created_at").reset_index(drop=True)
    y1, y2 = df[df.created_at < SPLIT], df[df.created_at >= SPLIT]
    print(f"Setups: {len(df)} (Y1 {len(y1)}, Y2 {len(y2)}) | win rate {(df.r_net > 0).mean():.1%} | "
          f"mean net R {df.r_net.mean():+.3f} (Y1 {y1.r_net.mean():+.3f}, Y2 {y2.r_net.mean():+.3f})")

    # 1) single-feature consistency: Spearman correlation with net R in each year
    X, _ = prepare_xy(df)
    rows = []
    for c in X.columns:
        r1 = spearmanr(X.loc[y1.index, c], y1.r_net, nan_policy="omit").statistic
        r2 = spearmanr(X.loc[y2.index, c], y2.r_net, nan_policy="omit").statistic
        rows.append((c, r1, r2))
    fc = pd.DataFrame(rows, columns=["feature", "corr_Y1", "corr_Y2"]).dropna()
    fc["consistent"] = np.sign(fc.corr_Y1) == np.sign(fc.corr_Y2)
    fc["min_abs"] = np.where(fc.consistent, np.minimum(fc.corr_Y1.abs(), fc.corr_Y2.abs()), 0)
    print("\n== Özellik tutarlılığı (net R ile Spearman korelasyonu, iki yılda aynı işaret) ==")
    print(fc.sort_values("min_abs", ascending=False).head(15).round(3).to_string(index=False))

    # 2) walk-forward both ways
    results = {}
    for name, (train, test) in {"Y1->Y2": (y1, y2), "Y2->Y1": (y2, y1)}.items():
        Xtr, ytr = prepare_xy(train)
        Xte, _ = prepare_xy(test)
        Xte = Xte[Xtr.columns]
        for mname, model in models().items():
            model.fit(Xtr, ytr)
            p = model.predict_proba(Xte)[:, 1]
            results[f"{name} {mname}"] = evaluate(p, test)
    print("\n== Walk-forward (eğitim yılı -> test yılı) ==")
    print(json.dumps(results, indent=1))

    # 3) reference: the current hand-made hard filters
    ref = df[(df.htf_trend == 1) & (df.killzone.astype(bool)) & (df.sweep_weight >= 10)]
    print("\nMevcut sert filtre (4H+killzone+seviye>=10): "
          f"Y1 n={int((ref.created_at < SPLIT).sum())} R={ref[ref.created_at < SPLIT].r_net.mean():+.3f} | "
          f"Y2 n={int((ref.created_at >= SPLIT).sum())} R={ref[ref.created_at >= SPLIT].r_net.mean():+.3f}")


if __name__ == "__main__":
    main()
