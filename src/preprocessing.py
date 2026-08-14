from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.feature_selection import SelectKBest, mutual_info_classif
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


class QuantileClipper(BaseEstimator, TransformerMixin):
    """Clip numeric columns using quantiles estimated on the training split only."""

    def __init__(self, lower: float = 0.01, upper: float = 0.99):
        self.lower = lower
        self.upper = upper
        self.lower_bounds_: Optional[np.ndarray] = None
        self.upper_bounds_: Optional[np.ndarray] = None

    def fit(self, X, y=None):
        arr = np.asarray(X, dtype=float)
        self.lower_bounds_ = np.nanquantile(arr, self.lower, axis=0)
        self.upper_bounds_ = np.nanquantile(arr, self.upper, axis=0)
        return self

    def transform(self, X):
        arr = np.asarray(X, dtype=float)
        return np.clip(arr, self.lower_bounds_, self.upper_bounds_)


def build_preprocessor(
    numeric_features: Iterable[str],
    clip_quantiles: tuple[float, float] = (0.01, 0.99),
    scale: bool = True,
) -> ColumnTransformer:
    steps = [
        ("imputer", SimpleImputer(strategy="median")),
        ("clipper", QuantileClipper(lower=clip_quantiles[0], upper=clip_quantiles[1])),
    ]
    if scale:
        steps.append(("scaler", StandardScaler()))

    numeric_pipeline = Pipeline(steps=steps)
    return ColumnTransformer(
        transformers=[("num", numeric_pipeline, list(numeric_features))],
        remainder="drop",
        verbose_feature_names_out=False,
    )


def build_model_pipeline(
    estimator,
    numeric_features: Iterable[str],
    clip_quantiles: tuple[float, float] = (0.01, 0.99),
    scale: bool = True,
    use_feature_selection: bool = False,
    k: int | str = "all",
) -> Pipeline:
    steps = [
        (
            "preprocess",
            build_preprocessor(
                numeric_features=numeric_features,
                clip_quantiles=clip_quantiles,
                scale=scale,
            ),
        )
    ]
    if use_feature_selection:
        steps.append(("select", SelectKBest(score_func=mutual_info_classif, k=k)))
    steps.append(("model", estimator))
    return Pipeline(steps)


def clean_feature_frame(df: pd.DataFrame, label_col: str) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """Prepare feature matrix for scikit-learn.

    - Drops identifiers and raw timestamp-like columns.
    - Keeps numeric columns only.
    - Removes rows without a label.
    """
    df = df.copy()
    df = df.dropna(subset=[label_col])
    y = df[label_col].astype(int)

    drop_cols = {label_col, "hadm_id", "subject_id"}
    numeric_cols = [
        c for c in df.columns
        if c not in drop_cols and pd.api.types.is_numeric_dtype(df[c])
    ]
    X = df[numeric_cols]
    return X, y, numeric_cols
