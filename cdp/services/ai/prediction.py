"""Prediction engine for churn, lifetime value, and next-purchase forecasting.

Uses scikit-learn ensemble models with automatic feature extraction from
customer profiles and their behavioural event streams.
"""

from __future__ import annotations

import logging
import pickle
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import settings
from cdp.models.ai_model import PredictionResult, PredictionType
from cdp.models.customer import Customer, CustomerEvent

logger = logging.getLogger(__name__)


class PredictionEngine:
    """Produces churn, LTV, and next-purchase predictions for customers.

    Each prediction type has its own scikit-learn model that can be trained
    independently.  Models are versioned with timestamps and persisted to
    disk so they can be reloaded without retraining.

    Usage::

        engine = PredictionEngine()
        engine.train_churn_model(training_df)
        result = engine.predict_churn(customer_features)
    """

    def __init__(self, model_dir: Optional[str] = None) -> None:
        self.model_dir = Path(model_dir or settings.AI_MODEL_PATH) / "prediction"
        self.model_dir.mkdir(parents=True, exist_ok=True)

        # Models
        self._churn_model: RandomForestClassifier | None = None
        self._ltv_model: GradientBoostingRegressor | None = None
        self._next_purchase_model: GradientBoostingRegressor | None = None

        # Scalers (one per model keeps feature spaces independent)
        self._churn_scaler: StandardScaler = StandardScaler()
        self._ltv_scaler: StandardScaler = StandardScaler()
        self._next_purchase_scaler: StandardScaler = StandardScaler()

        # Metadata
        self._model_versions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_features(
        customer: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Derive a flat feature dictionary from a customer profile and events.

        Parameters
        ----------
        customer:
            Customer record (or dict-like) with fields such as
            ``created_at``, ``lifetime_value``, ``is_active``.
        events:
            List of event dicts with ``timestamp``, ``event_type``,
            ``properties``.

        Returns
        -------
        dict[str, float]
            Feature names mapped to numeric values suitable for model input.
        """
        now = datetime.now(timezone.utc)

        # --- Basic profile features ---
        created_at = customer.get("created_at")
        if isinstance(created_at, str):
            created_at = pd.Timestamp(created_at)
        elif not isinstance(created_at, datetime):
            created_at = now

        account_age_days = max((now - pd.Timestamp(created_at, unit="ns").to_pydatetime().replace(
            tzinfo=timezone.utc
        )).days, 0) if created_at else 0

        ltv = float(customer.get("lifetime_value", 0) or 0)

        # --- Event-derived features ---
        if events:
            timestamps = []
            for e in events:
                ts = e.get("timestamp")
                if isinstance(ts, (int, float)):
                    timestamps.append(datetime.fromtimestamp(ts, tz=timezone.utc))
                elif isinstance(ts, str):
                    timestamps.append(pd.Timestamp(ts).to_pydatetime().replace(tzinfo=timezone.utc))
                elif isinstance(ts, datetime):
                    timestamps.append(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))

            if timestamps:
                last_event = max(timestamps)
                first_event = min(timestamps)
                recency_days = (now - last_event).days
                active_span_days = max((last_event - first_event).days, 1)
            else:
                recency_days = 999
                active_span_days = 0
        else:
            recency_days = 999
            active_span_days = 0
            timestamps = []

        total_events = len(events) if events else 0
        event_types = set(e.get("event_type", "") for e in events) if events else set()

        # Revenue from event properties
        total_revenue = sum(
            float(e.get("properties", {}).get("revenue", 0) or 0)
            for e in (events or [])
        )

        purchase_events = [
            e for e in (events or [])
            if e.get("event_type") in ("purchase", "order", "transaction")
        ]
        purchase_count = len(purchase_events)
        avg_order_value = total_revenue / max(purchase_count, 1)

        # Session / engagement proxies
        unique_sessions = len(set(e.get("session_id", "") for e in (events or []) if e.get("session_id")))
        avg_events_per_session = total_events / max(unique_sessions, 1)

        # Days between purchases (inter-purchase interval)
        if purchase_count >= 2:
            purchase_times = sorted(
                pd.Timestamp(e.get("timestamp")).to_pydatetime()
                for e in purchase_events
                if e.get("timestamp") is not None
            )
            intervals = [
                (purchase_times[i + 1] - purchase_times[i]).days
                for i in range(len(purchase_times) - 1)
            ]
            avg_inter_purchase_days = float(np.mean(intervals)) if intervals else 0.0
        else:
            avg_inter_purchase_days = 0.0

        return {
            "account_age_days": float(account_age_days),
            "lifetime_value": ltv,
            "recency_days": float(recency_days),
            "active_span_days": float(active_span_days),
            "total_events": float(total_events),
            "unique_event_types": float(len(event_types)),
            "total_revenue": total_revenue,
            "purchase_count": float(purchase_count),
            "avg_order_value": avg_order_value,
            "unique_sessions": float(unique_sessions),
            "avg_events_per_session": avg_events_per_session,
            "avg_inter_purchase_days": avg_inter_purchase_days,
        }

    # ------------------------------------------------------------------
    # Churn prediction
    # ------------------------------------------------------------------

    def train_churn_model(
        self,
        training_data: pd.DataFrame,
        target_col: str = "churned",
        test_size: float = 0.2,
    ) -> dict[str, float]:
        """Train the churn classification model.

        Parameters
        ----------
        training_data:
            DataFrame where every row is a customer feature vector.  Must
            include a boolean/int ``churned`` target column.
        target_col:
            Name of the binary target column.
        test_size:
            Fraction of data held out for evaluation.

        Returns
        -------
        dict
            Training and test accuracy metrics.
        """
        feature_cols = [c for c in training_data.columns if c != target_col]
        X = training_data[feature_cols].fillna(0).values
        y = training_data[target_col].astype(int).values

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=42, stratify=y,
        )

        X_train_scaled = self._churn_scaler.fit_transform(X_train)
        X_test_scaled = self._churn_scaler.transform(X_test)

        self._churn_model = RandomForestClassifier(
            n_estimators=200,
            max_depth=10,
            min_samples_split=5,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
        self._churn_model.fit(X_train_scaled, y_train)

        train_acc = float(self._churn_model.score(X_train_scaled, y_train))
        test_acc = float(self._churn_model.score(X_test_scaled, y_test))

        version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._model_versions["churn"] = version

        logger.info(
            "Churn model trained (v%s) – train_acc=%.4f test_acc=%.4f",
            version, train_acc, test_acc,
        )
        return {
            "train_accuracy": train_acc,
            "test_accuracy": test_acc,
            "model_version": version,
            "n_samples": len(X),
            "feature_cols": feature_cols,
        }

    def predict_churn(
        self,
        customer_features: dict[str, float],
    ) -> dict[str, Any]:
        """Predict churn probability for a single customer.

        Parameters
        ----------
        customer_features:
            Feature dict (keys matching the training columns).

        Returns
        -------
        dict
            ``churn_probability``, ``will_churn`` (bool), ``confidence``,
            ``model_version``.
        """
        if self._churn_model is None:
            raise RuntimeError("Churn model is not trained. Call train_churn_model() first.")

        X = np.array([[v for v in customer_features.values()]])
        X_scaled = self._churn_scaler.transform(X)

        proba = self._churn_model.predict_proba(X_scaled)[0]
        churn_prob = float(proba[1]) if len(proba) > 1 else float(proba[0])

        return {
            "churn_probability": round(churn_prob, 4),
            "will_churn": churn_prob >= 0.5,
            "confidence": round(float(max(proba)), 4),
            "model_version": self._model_versions.get("churn", "unknown"),
        }

    # ------------------------------------------------------------------
    # LTV prediction
    # ------------------------------------------------------------------

    def train_ltv_model(
        self,
        training_data: pd.DataFrame,
        target_col: str = "ltv",
        test_size: float = 0.2,
    ) -> dict[str, float]:
        """Train the lifetime-value regression model.

        Parameters
        ----------
        training_data:
            DataFrame with customer feature columns and a numeric ``ltv``
            target.
        target_col:
            Name of the continuous target column.
        test_size:
            Fraction of data held out for evaluation.

        Returns
        -------
        dict
            Training and test R-squared metrics.
        """
        feature_cols = [c for c in training_data.columns if c != target_col]
        X = training_data[feature_cols].fillna(0).values
        y = training_data[target_col].fillna(0).values.astype(float)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=42,
        )

        X_train_scaled = self._ltv_scaler.fit_transform(X_train)
        X_test_scaled = self._ltv_scaler.transform(X_test)

        self._ltv_model = GradientBoostingRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.1,
            min_samples_split=5,
            min_samples_leaf=3,
            subsample=0.8,
            random_state=42,
        )
        self._ltv_model.fit(X_train_scaled, y_train)

        train_r2 = float(self._ltv_model.score(X_train_scaled, y_train))
        test_r2 = float(self._ltv_model.score(X_test_scaled, y_test))

        version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._model_versions["ltv"] = version

        logger.info(
            "LTV model trained (v%s) – train_r2=%.4f test_r2=%.4f",
            version, train_r2, test_r2,
        )
        return {
            "train_r2": train_r2,
            "test_r2": test_r2,
            "model_version": version,
            "n_samples": len(X),
            "feature_cols": feature_cols,
        }

    def predict_ltv(
        self,
        customer_features: dict[str, float],
    ) -> dict[str, Any]:
        """Predict customer lifetime value.

        Returns
        -------
        dict
            ``predicted_ltv`` and ``model_version``.
        """
        if self._ltv_model is None:
            raise RuntimeError("LTV model is not trained. Call train_ltv_model() first.")

        X = np.array([[v for v in customer_features.values()]])
        X_scaled = self._ltv_scaler.transform(X)

        predicted = float(self._ltv_model.predict(X_scaled)[0])

        return {
            "predicted_ltv": round(max(predicted, 0.0), 2),
            "model_version": self._model_versions.get("ltv", "unknown"),
        }

    # ------------------------------------------------------------------
    # Next-purchase prediction
    # ------------------------------------------------------------------

    def train_next_purchase_model(
        self,
        training_data: pd.DataFrame,
        target_col: str = "days_to_next_purchase",
        test_size: float = 0.2,
    ) -> dict[str, float]:
        """Train a regression model predicting days until the next purchase.

        Parameters
        ----------
        training_data:
            DataFrame with customer feature columns and a numeric
            ``days_to_next_purchase`` target.
        target_col:
            Name of the target column.
        test_size:
            Fraction of data held out for evaluation.

        Returns
        -------
        dict
            Training and test R-squared metrics.
        """
        feature_cols = [c for c in training_data.columns if c != target_col]
        X = training_data[feature_cols].fillna(0).values
        y = training_data[target_col].fillna(0).values.astype(float)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=42,
        )

        X_train_scaled = self._next_purchase_scaler.fit_transform(X_train)
        X_test_scaled = self._next_purchase_scaler.transform(X_test)

        self._next_purchase_model = GradientBoostingRegressor(
            n_estimators=150,
            max_depth=5,
            learning_rate=0.1,
            min_samples_split=5,
            min_samples_leaf=3,
            subsample=0.8,
            random_state=42,
        )
        self._next_purchase_model.fit(X_train_scaled, y_train)

        train_r2 = float(self._next_purchase_model.score(X_train_scaled, y_train))
        test_r2 = float(self._next_purchase_model.score(X_test_scaled, y_test))

        version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._model_versions["next_purchase"] = version

        logger.info(
            "Next-purchase model trained (v%s) – train_r2=%.4f test_r2=%.4f",
            version, train_r2, test_r2,
        )
        return {
            "train_r2": train_r2,
            "test_r2": test_r2,
            "model_version": version,
            "n_samples": len(X),
            "feature_cols": feature_cols,
        }

    def predict_next_purchase(
        self,
        customer_features: dict[str, float],
    ) -> dict[str, Any]:
        """Predict days until the customer's next purchase.

        Returns
        -------
        dict
            ``predicted_days``, ``predicted_date``, and ``model_version``.
        """
        if self._next_purchase_model is None:
            raise RuntimeError(
                "Next-purchase model is not trained. Call train_next_purchase_model() first."
            )

        X = np.array([[v for v in customer_features.values()]])
        X_scaled = self._next_purchase_scaler.transform(X)

        predicted_days = float(self._next_purchase_model.predict(X_scaled)[0])
        predicted_days = max(predicted_days, 0.0)

        predicted_date = (
            datetime.now(timezone.utc) + timedelta(days=predicted_days)
        ).date().isoformat()

        return {
            "predicted_days": round(predicted_days, 1),
            "predicted_date": predicted_date,
            "model_version": self._model_versions.get("next_purchase", "unknown"),
        }

    # ------------------------------------------------------------------
    # Model persistence
    # ------------------------------------------------------------------

    def save_model(self, prediction_type: str) -> Path:
        """Serialize a model, its scaler, and version metadata to disk.

        Parameters
        ----------
        prediction_type:
            One of ``"churn"``, ``"ltv"``, ``"next_purchase"``.

        Returns
        -------
        Path
            Location of the saved artifact.
        """
        model_map = {
            "churn": (self._churn_model, self._churn_scaler),
            "ltv": (self._ltv_model, self._ltv_scaler),
            "next_purchase": (self._next_purchase_model, self._next_purchase_scaler),
        }
        if prediction_type not in model_map:
            raise ValueError(f"Unknown prediction_type: {prediction_type}")

        model, scaler = model_map[prediction_type]
        if model is None:
            raise RuntimeError(f"No trained model for '{prediction_type}'.")

        version = self._model_versions.get(prediction_type, "unknown")
        path = self.model_dir / f"{prediction_type}_{version}.pkl"

        payload = {
            "model": model,
            "scaler": scaler,
            "version": version,
            "prediction_type": prediction_type,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

        logger.info("Prediction model saved to %s", path)
        return path

    def load_model(self, prediction_type: str, version: Optional[str] = None) -> None:
        """Load a previously saved prediction model from disk.

        Parameters
        ----------
        prediction_type:
            One of ``"churn"``, ``"ltv"``, ``"next_purchase"``.
        version:
            Specific version timestamp.  When *None* the latest file for the
            given type is loaded.
        """
        if version:
            path = self.model_dir / f"{prediction_type}_{version}.pkl"
        else:
            # Find the newest file matching the type prefix
            candidates = sorted(
                self.model_dir.glob(f"{prediction_type}_*.pkl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise FileNotFoundError(
                    f"No saved model for '{prediction_type}' in {self.model_dir}"
                )
            path = candidates[0]

        with open(path, "rb") as fh:
            payload = pickle.load(fh)  # noqa: S301

        model = payload["model"]
        scaler = payload["scaler"]
        ver = payload.get("version", "unknown")

        if prediction_type == "churn":
            self._churn_model = model
            self._churn_scaler = scaler
        elif prediction_type == "ltv":
            self._ltv_model = model
            self._ltv_scaler = scaler
        elif prediction_type == "next_purchase":
            self._next_purchase_model = model
            self._next_purchase_scaler = scaler
        else:
            raise ValueError(f"Unknown prediction_type: {prediction_type}")

        self._model_versions[prediction_type] = ver
        logger.info("Prediction model '%s' v%s loaded from %s", prediction_type, ver, path)

    # ------------------------------------------------------------------
    # Async batch processing
    # ------------------------------------------------------------------

    async def batch_predict(
        self,
        db_session: AsyncSession,
        prediction_type: str = "churn",
        batch_size: int = 500,
    ) -> int:
        """Run predictions for all active customers and persist results.

        Parameters
        ----------
        db_session:
            Active async SQLAlchemy session.
        prediction_type:
            One of ``"churn"``, ``"ltv"``, ``"next_purchase"``.
        batch_size:
            Number of customers processed per database round-trip.

        Returns
        -------
        int
            Number of predictions persisted.
        """
        predict_fn_map = {
            "churn": self.predict_churn,
            "ltv": self.predict_ltv,
            "next_purchase": self.predict_next_purchase,
        }
        predict_fn = predict_fn_map.get(prediction_type)
        if predict_fn is None:
            raise ValueError(f"Unsupported prediction_type: {prediction_type}")

        logger.info("Starting batch prediction (%s)", prediction_type)

        # Fetch active customers
        stmt = select(Customer).where(Customer.is_active.is_(True))
        result = await db_session.execute(stmt)
        customers = result.scalars().all()

        if not customers:
            logger.warning("No active customers found for batch prediction.")
            return 0

        total_saved = 0
        model_version = self._model_versions.get(prediction_type, "unknown")

        for offset in range(0, len(customers), batch_size):
            batch = customers[offset: offset + batch_size]

            for customer in batch:
                try:
                    # Fetch events for this customer
                    events_stmt = (
                        select(CustomerEvent)
                        .where(CustomerEvent.customer_id == customer.id)
                        .order_by(CustomerEvent.timestamp.desc())
                        .limit(1000)
                    )
                    events_result = await db_session.execute(events_stmt)
                    events = events_result.scalars().all()

                    event_dicts = [
                        {
                            "timestamp": e.timestamp,
                            "event_type": e.event_type,
                            "properties": e.properties or {},
                            "session_id": e.session_id,
                        }
                        for e in events
                    ]

                    customer_dict = {
                        "created_at": customer.created_at,
                        "lifetime_value": customer.lifetime_value,
                        "is_active": customer.is_active,
                    }

                    features = self.extract_features(customer_dict, event_dicts)
                    prediction = predict_fn(features)

                    # Determine the scalar value to store
                    if prediction_type == "churn":
                        value = prediction["churn_probability"]
                        confidence = prediction["confidence"]
                    elif prediction_type == "ltv":
                        value = prediction["predicted_ltv"]
                        confidence = 1.0
                    else:
                        value = prediction["predicted_days"]
                        confidence = 1.0

                    prediction_record = PredictionResult(
                        customer_id=customer.id,
                        prediction_type=prediction_type,
                        prediction_value=value,
                        confidence=confidence,
                        features_used=features,
                        model_version=model_version,
                    )
                    db_session.add(prediction_record)
                    total_saved += 1

                    # Update customer risk_score for churn predictions
                    if prediction_type == "churn":
                        customer.risk_score = value

                except Exception:
                    logger.exception(
                        "Failed prediction for customer %s", customer.id,
                    )

            await db_session.flush()

        await db_session.commit()
        logger.info(
            "Batch prediction complete: %d %s predictions saved",
            total_saved, prediction_type,
        )
        return total_saved
