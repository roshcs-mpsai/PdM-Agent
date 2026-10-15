"""Candidate detectors behind one interface (DET-01, DET-02, DET-07).

Every detector has ``fit`` on healthy windows and ``score``, higher meaning
more anomalous. Those that can say which features drive a score also have
``errors``, the per-feature error the attribution tool reads (DET-07). Moved
unchanged from notebooks/detector_selection Section 3; the harness
(eval/harness.py) treats every detector alike.

| key | detector | family | attribution |
|---|---|---|---|
| zscore | largest absolute z across features | baseline | yes |
| pca | squared reconstruction error off the 95 %-variance subspace | baseline | yes |
| iforest | Isolation Forest | classical | no |
| robust_mahalanobis | Mahalanobis distance under a 90 %-support MCD | classical | no |
| lof | Local Outlier Factor, 50 neighbours, novelty mode | classical | no |
| dense_ae | 21-16-6-16-21 autoencoder (scikit-learn MLP) | deep, features | yes |
| dense_ae_seq | 128-32-128 autoencoder on the raw 60 x 7 sequences | deep, sequence | yes |
"""
from __future__ import annotations

import numpy as np
from sklearn.covariance import MinCovDet
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
from sklearn.neural_network import MLPRegressor

SEED = 0


class ZScore:
    key, name, family, attribution, multivariate, complexity = "zscore", "z-score", "baseline", True, False, 0

    def fit(self, X):
        return self                       # X is already standardized on the fit windows

    def errors(self, X):
        return np.abs(X)

    def score(self, X):
        return np.abs(X).max(axis=1)


class PCARecon:
    key, name, family, attribution, multivariate, complexity = "pca", "PCA", "baseline", True, True, 1

    def fit(self, X):
        self.pca = PCA(n_components=0.95, svd_solver="full", random_state=SEED).fit(X)
        return self

    def errors(self, X):
        return (X - self.pca.inverse_transform(self.pca.transform(X))) ** 2

    def score(self, X):
        return self.errors(X).sum(axis=1)


class IForest:
    key, name, family, attribution, multivariate, complexity = \
        "iforest", "Isolation Forest", "classical", False, True, 2

    def fit(self, X):
        self.model = IsolationForest(n_estimators=300, max_samples=512, random_state=SEED, n_jobs=-1).fit(X)
        return self

    def score(self, X):
        return -self.model.score_samples(X)


def subsample(X, n=20_000):
    return X[np.random.default_rng(SEED).choice(len(X), min(n, len(X)), replace=False)]


class RobustMahalanobis:
    key, name, family, attribution, multivariate, complexity = \
        "robust_mahalanobis", "Robust Mahalanobis", "classical", False, True, 2

    def fit(self, X):
        # 90 % support: with the default (about half) the estimate collapses onto the
        # compressor-off state, where several features are nearly constant
        self.cov = MinCovDet(support_fraction=0.9, random_state=SEED).fit(subsample(X))
        return self

    def score(self, X):
        return self.cov.mahalanobis(X)


class LOF:
    key, name, family, attribution, multivariate, complexity = "lof", "LOF", "classical", False, True, 2

    def fit(self, X):
        self.model = LocalOutlierFactor(n_neighbors=50, novelty=True, n_jobs=-1).fit(subsample(X))
        return self

    def score(self, X):
        return -self.model.score_samples(X)


class DenseAE:
    family, attribution, multivariate = "deep, features", True, True

    def __init__(self, hidden=(16, 6, 16), name="Dense AE (features)", key="dense_ae", complexity=3, max_iter=200):
        self.hidden, self.name, self.key, self.complexity, self.max_iter = hidden, name, key, complexity, max_iter

    def fit(self, X):
        self.model = MLPRegressor(hidden_layer_sizes=self.hidden, activation="tanh", early_stopping=True,
                                  validation_fraction=0.1, n_iter_no_change=10, max_iter=self.max_iter,
                                  random_state=SEED).fit(X, X)
        return self

    def errors(self, X):
        return (X - self.model.predict(X)) ** 2

    def score(self, X):
        return np.concatenate([self.errors(X[i:i + 50_000]).mean(axis=1) for i in range(0, len(X), 50_000)])


def tabular_detectors() -> dict:
    """The six detectors that read the window features, keyed as on the command line."""
    return {d.key: d for d in [ZScore(), PCARecon(), IForest(), RobustMahalanobis(), LOF(), DenseAE()]}


def sequence_detector() -> DenseAE:
    det = DenseAE((128, 32, 128), "Dense AE (sequences)", key="dense_ae_seq", complexity=4, max_iter=80)
    det.family = "deep, sequence"
    return det


def info(det) -> dict:
    return {"family": det.family, "attribution": det.attribution, "multivariate": det.multivariate,
            "complexity": det.complexity}
