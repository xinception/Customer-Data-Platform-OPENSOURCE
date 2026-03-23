"""Customer segmentation engine using RFM analysis and clustering algorithms.

Provides automated customer segmentation via KMeans and DBSCAN clustering
on Recency, Frequency, and Monetary features derived from customer event data.
"""

from __future__ import annotations

import logging
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN, KMeans
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import settings

logger = logging.getLogger(__name__)

SEGMENT_LABELS: dict[int, str] = {
    0: "Champions",
    1: "Loyal",
    2: "Potential",
    3: "At Risk",
    4: "Lost",
}


class CustomerSegmentationEngine:
    """Cluster customers into behavioural segments using RFM features.

    Supports KMeans (default) and DBSCAN algorithms.  Models can be
    persisted to disk and reloaded for inference without retraining.

    Usage::

        engine = CustomerSegmentationEngine()
        engine.train_segments(customers_df)
        label = engine.predict_segment(single_customer_features)
    """

    def __init__(
        self,
        algorithm: str = "kmeans",
        model_dir: Optional[str] = None,
    ) -> None:
        self.algorithm = algorithm
        self.model_dir = Path(model_dir or settings.AI_MODEL_PATH) / "segmentation"
        self.model_dir.mkdir(parents=True, exist_ok=True)

        self.scaler: StandardScaler = StandardScaler()
        self.model: KMeans | DBSCAN | None = None
        self.cluster_centers_: np.ndarray | None = None
        self.is_fitted: bool = False
        self._segment_labels: dict[int, str] = dict(SEGMENT_LABELS)

    # ------------------------------------------------------------------
    # Feature engineering
    # ------------------------------------------------------------------

    @staticmethod
    def compute_rfm_features(
        customer_events: pd.DataFrame,
        reference_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Compute Recency, Frequency, and Monetary features per customer.

        Parameters
        ----------
        customer_events:
            DataFrame with at least ``customer_id``, ``event_timestamp``,
            and ``revenue`` (or ``amount``) columns.
        reference_date:
            Anchor date for recency calculation.  Defaults to *now* (UTC).

        Returns
        -------
        pd.DataFrame
            Indexed by ``customer_id`` with columns ``recency``,
            ``frequency``, and ``monetary``.
        """
        if reference_date is None:
            reference_date = datetime.now(timezone.utc)

        df = customer_events.copy()

        # Normalise column names
        if "event_timestamp" in df.columns:
            df["event_timestamp"] = pd.to_datetime(df["event_timestamp"], utc=True)
        elif "timestamp" in df.columns:
            df.rename(columns={"timestamp": "event_timestamp"}, inplace=True)
            df["event_timestamp"] = pd.to_datetime(df["event_timestamp"], utc=True)

        revenue_col = "revenue" if "revenue" in df.columns else "amount"
        if revenue_col not in df.columns:
            df[revenue_col] = 0.0

        rfm = df.groupby("customer_id").agg(
            recency=("event_timestamp", lambda x: (reference_date - x.max()).days),
            frequency=("event_timestamp", "count"),
            monetary=(revenue_col, "sum"),
        )

        return rfm

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train_segments(
        self,
        customers_data: pd.DataFrame,
        n_clusters: int = 5,
    ) -> np.ndarray:
        """Train the segmentation model on pre-computed RFM features.

        Parameters
        ----------
        customers_data:
            DataFrame with columns ``recency``, ``frequency``, ``monetary``.
        n_clusters:
            Number of clusters (only used for KMeans).

        Returns
        -------
        np.ndarray
            Cluster labels for each row in *customers_data*.
        """
        feature_cols = ["recency", "frequency", "monetary"]
        missing = [c for c in feature_cols if c not in customers_data.columns]
        if missing:
            raise ValueError(f"Missing required feature columns: {missing}")

        X = customers_data[feature_cols].fillna(0).values
        X_scaled = self.scaler.fit_transform(X)

        if self.algorithm == "dbscan":
            self.model = DBSCAN(eps=0.5, min_samples=5)
        else:
            self.model = KMeans(
                n_clusters=n_clusters,
                n_init=10,
                random_state=42,
            )

        labels = self.model.fit_predict(X_scaled)

        if hasattr(self.model, "cluster_centers_"):
            self.cluster_centers_ = self.model.cluster_centers_

        self.is_fitted = True
        self._assign_segment_labels(X, labels)

        logger.info(
            "Segmentation model trained (%s) on %d customers -> %d segments",
            self.algorithm,
            len(X),
            len(set(labels)),
        )
        return labels

    def auto_segment(
        self,
        customers_data: pd.DataFrame,
        n_clusters: int = 5,
    ) -> pd.DataFrame:
        """Train and return the original data augmented with segment labels.

        Parameters
        ----------
        customers_data:
            DataFrame with ``recency``, ``frequency``, ``monetary`` columns.
        n_clusters:
            Desired number of segments.

        Returns
        -------
        pd.DataFrame
            Copy of *customers_data* with added ``segment_id`` and
            ``segment_label`` columns.
        """
        labels = self.train_segments(customers_data, n_clusters=n_clusters)
        result = customers_data.copy()
        result["segment_id"] = labels
        result["segment_label"] = result["segment_id"].map(
            lambda sid: self._segment_labels.get(sid, f"Segment {sid}")
        )
        return result

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_segment(self, customer: dict[str, Any] | pd.Series) -> dict[str, Any]:
        """Predict the segment for a single customer.

        Parameters
        ----------
        customer:
            Must contain ``recency``, ``frequency``, ``monetary`` keys.

        Returns
        -------
        dict
            ``{"segment_id": int, "segment_label": str}``
        """
        if not self.is_fitted or self.model is None:
            raise RuntimeError("Model has not been trained. Call train_segments() first.")

        features = np.array([[
            customer.get("recency", customer.get("Recency", 0)),
            customer.get("frequency", customer.get("Frequency", 0)),
            customer.get("monetary", customer.get("Monetary", 0)),
        ]])
        features_scaled = self.scaler.transform(features)

        if hasattr(self.model, "predict"):
            segment_id = int(self.model.predict(features_scaled)[0])
        else:
            # DBSCAN does not natively support predict; use nearest centre
            segment_id = self._nearest_cluster(features_scaled[0])

        return {
            "segment_id": segment_id,
            "segment_label": self._segment_labels.get(segment_id, f"Segment {segment_id}"),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_model(self, filename: Optional[str] = None) -> Path:
        """Serialize the trained model, scaler, and labels to disk.

        Returns the path to the saved file.
        """
        if not self.is_fitted:
            raise RuntimeError("Cannot save an untrained model.")

        filename = filename or f"segmentation_{self.algorithm}.pkl"
        path = self.model_dir / filename

        payload = {
            "model": self.model,
            "scaler": self.scaler,
            "segment_labels": self._segment_labels,
            "cluster_centers": self.cluster_centers_,
            "algorithm": self.algorithm,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

        logger.info("Segmentation model saved to %s", path)
        return path

    def load_model(self, filename: Optional[str] = None) -> None:
        """Load a previously saved model from disk."""
        filename = filename or f"segmentation_{self.algorithm}.pkl"
        path = self.model_dir / filename

        if not path.exists():
            raise FileNotFoundError(f"No saved model found at {path}")

        with open(path, "rb") as fh:
            payload = pickle.load(fh)  # noqa: S301

        self.model = payload["model"]
        self.scaler = payload["scaler"]
        self._segment_labels = payload.get("segment_labels", dict(SEGMENT_LABELS))
        self.cluster_centers_ = payload.get("cluster_centers")
        self.algorithm = payload.get("algorithm", self.algorithm)
        self.is_fitted = True

        logger.info("Segmentation model loaded from %s", path)

    # ------------------------------------------------------------------
    # Async database integration
    # ------------------------------------------------------------------

    async def run_segmentation(
        self,
        db_session: AsyncSession,
        n_clusters: int = 5,
    ) -> pd.DataFrame:
        """Pull customer event data, train segments, and update customers.

        This is the main entry-point for scheduled / background jobs.

        Parameters
        ----------
        db_session:
            An active async SQLAlchemy session.
        n_clusters:
            Number of segments to create.

        Returns
        -------
        pd.DataFrame
            RFM table augmented with segment assignments.
        """
        logger.info("Starting segmentation run (n_clusters=%d)", n_clusters)

        # Pull raw event data -------------------------------------------
        query = select(
            func.column("customer_id"),
            func.column("event_timestamp"),
            func.column("revenue"),
        ).select_from(func.table("events"))

        result = await db_session.execute(query)
        rows = result.fetchall()

        if not rows:
            logger.warning("No event data found; segmentation aborted.")
            return pd.DataFrame()

        events_df = pd.DataFrame(rows, columns=["customer_id", "event_timestamp", "revenue"])

        # Compute RFM & segment -----------------------------------------
        rfm = self.compute_rfm_features(events_df)
        segmented = self.auto_segment(rfm, n_clusters=n_clusters)

        # Persist segment assignments back to DB -------------------------
        for customer_id, row in segmented.iterrows():
            stmt = (
                func.table("customers")
                .update()
                .where(func.column("id") == customer_id)
                .values(
                    segment_id=int(row["segment_id"]),
                    segment_label=row["segment_label"],
                )
            )
            try:
                await db_session.execute(stmt)
            except Exception:
                logger.debug(
                    "Skipping DB update for customer %s (table may not exist yet)",
                    customer_id,
                )

        await db_session.commit()
        self.save_model()

        logger.info(
            "Segmentation run complete: %d customers segmented into %d groups",
            len(segmented),
            segmented["segment_id"].nunique(),
        )
        return segmented

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assign_segment_labels(
        self, raw_features: np.ndarray, labels: np.ndarray
    ) -> None:
        """Heuristically map cluster IDs to human-readable labels.

        Ordering is based on the mean monetary value of each cluster
        (descending), so the highest-value cluster gets the *Champions*
        label.
        """
        unique_labels = sorted(set(labels))
        if -1 in unique_labels:
            unique_labels.remove(-1)

        # Compute mean monetary per cluster (column index 2)
        cluster_monetary: dict[int, float] = {}
        for cid in unique_labels:
            mask = labels == cid
            cluster_monetary[cid] = float(np.mean(raw_features[mask, 2])) if mask.any() else 0.0

        ranked = sorted(cluster_monetary, key=cluster_monetary.get, reverse=True)  # type: ignore[arg-type]

        default_names = ["Champions", "Loyal", "Potential", "At Risk", "Lost"]
        self._segment_labels = {}
        for idx, cid in enumerate(ranked):
            if idx < len(default_names):
                self._segment_labels[cid] = default_names[idx]
            else:
                self._segment_labels[cid] = f"Segment {cid}"

        if -1 in set(labels):
            self._segment_labels[-1] = "Outlier"

    def _nearest_cluster(self, point: np.ndarray) -> int:
        """Return the label of the nearest known cluster centre."""
        if self.cluster_centers_ is None:
            return -1
        distances = np.linalg.norm(self.cluster_centers_ - point, axis=1)
        return int(np.argmin(distances))
