from __future__ import annotations

from typing import Dict

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    matthews_corrcoef,
    precision_score,
    recall_score,
    roc_auc_score,
)


def classification_metrics(y_true, y_prob, threshold: float = 0.5) -> Dict[str, float]:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    y_pred = (y_prob >= threshold).astype(int)

    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    specificity = tn / (tn + fp) if (tn + fp) else np.nan

    metrics = {
        "accuracy": accuracy_score(y_true, y_pred),
        "balanced_accuracy": balanced_accuracy_score(y_true, y_pred),
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall_sensitivity": recall_score(y_true, y_pred, zero_division=0),
        "specificity": specificity,
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "mcc": matthews_corrcoef(y_true, y_pred),
        "auroc": roc_auc_score(y_true, y_prob) if len(np.unique(y_true)) > 1 else np.nan,
        "auprc": average_precision_score(y_true, y_prob) if len(np.unique(y_true)) > 1 else np.nan,
        "brier_score": brier_score_loss(y_true, y_prob),
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "tp": tp,
    }
    return {k: round(float(v), 6) if isinstance(v, (float, np.floating)) else int(v) for k, v in metrics.items()}


def calibration_intercept_slope(y_true, y_prob, eps: float = 1e-6) -> Dict[str, float]:
    """Calibration intercept/slope via logistic regression of y_true on logit(y_prob).

    Standard weak-calibration diagnostic (Van Calster et al., 2019): a perfectly
    calibrated model has intercept ~0 and slope ~1. y_prob is clipped away from 0/1
    to avoid infinities in the logit transform.
    """
    y_true = np.asarray(y_true).astype(int)
    if len(np.unique(y_true)) < 2:
        return {"calibration_intercept": float("nan"), "calibration_slope": float("nan")}
    p = np.clip(np.asarray(y_prob, dtype=float), eps, 1 - eps)
    logit_p = np.log(p / (1 - p)).reshape(-1, 1)
    lr = LogisticRegression(solver="lbfgs")
    lr.fit(logit_p, y_true)
    return {
        "calibration_intercept": round(float(lr.intercept_[0]), 6),
        "calibration_slope": round(float(lr.coef_[0][0]), 6),
    }


def calibration_curve_points(y_true, y_prob, n_bins: int = 10,
                              strategy: str = "quantile") -> Dict[str, np.ndarray]:
    """Bin-level reliability-diagram points. May return fewer than n_bins points if the
    sample is small or has many tied probabilities (quantile edges can merge)."""
    frac_pos, mean_pred = calibration_curve(y_true, y_prob, n_bins=n_bins, strategy=strategy)
    return {"mean_predicted": mean_pred, "frac_positive": frac_pos}
