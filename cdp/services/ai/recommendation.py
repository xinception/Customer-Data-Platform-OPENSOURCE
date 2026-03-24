"""Recommendation engine combining collaborative filtering, content-based
filtering, and a hybrid approach.

Uses cosine similarity on a user-item interaction matrix (collaborative) and
item feature vectors (content-based) to produce personalised recommendations.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import settings
from cdp.models.ai_model import Recommendation
from cdp.models.customer import Customer, CustomerEvent

logger = logging.getLogger(__name__)


class RecommendationEngine:
    """Produce personalised item recommendations for customers.

    Three strategies are available:

    * **Collaborative filtering** -- recommends items that similar users
      interacted with, built from a user-item interaction matrix.
    * **Content-based filtering** -- recommends items whose features are
      similar to items the customer already engaged with.
    * **Hybrid** (default) -- blends collaborative and content-based scores
      with configurable weighting.

    Usage::

        engine = RecommendationEngine()
        engine.update_user_item_matrix(interactions_df)
        recs = engine.get_recommendations(customer_id, n=10)
    """

    def __init__(
        self,
        collaborative_weight: float = 0.6,
        content_weight: float = 0.4,
    ) -> None:
        if not np.isclose(collaborative_weight + content_weight, 1.0):
            raise ValueError("collaborative_weight + content_weight must equal 1.0")

        self.collaborative_weight = collaborative_weight
        self.content_weight = content_weight

        # Collaborative filtering artefacts
        self._user_item_matrix: pd.DataFrame | None = None
        self._user_similarity: np.ndarray | None = None
        self._item_ids: list[str] = []
        self._user_ids: list[str] = []

        # Content-based filtering artefacts
        self._item_features: pd.DataFrame | None = None
        self._item_similarity: np.ndarray | None = None

    # ------------------------------------------------------------------
    # User-item matrix
    # ------------------------------------------------------------------

    def update_user_item_matrix(
        self,
        interactions: pd.DataFrame,
        value_col: str = "score",
    ) -> None:
        """Build or refresh the user-item interaction matrix.

        Parameters
        ----------
        interactions:
            DataFrame with columns ``customer_id``, ``item_id``, and a
            numeric interaction column (default ``score``).  If the score
            column is missing every interaction counts as ``1.0``.
        value_col:
            Column name that carries the interaction strength.
        """
        df = interactions.copy()
        if value_col not in df.columns:
            df[value_col] = 1.0

        pivot = df.pivot_table(
            index="customer_id",
            columns="item_id",
            values=value_col,
            aggfunc="sum",
            fill_value=0.0,
        )

        self._user_item_matrix = pivot
        self._user_ids = list(pivot.index)
        self._item_ids = list(pivot.columns)

        logger.info(
            "User-item matrix built: %d users x %d items",
            len(self._user_ids),
            len(self._item_ids),
        )

    def compute_similarity_matrix(
        self,
        kind: str = "user",
    ) -> np.ndarray:
        """Compute cosine similarity among users or items.

        Parameters
        ----------
        kind:
            ``"user"`` computes user-user similarity; ``"item"`` computes
            item-item similarity.

        Returns
        -------
        np.ndarray
            Square similarity matrix.
        """
        if self._user_item_matrix is None:
            raise RuntimeError("User-item matrix not built. Call update_user_item_matrix() first.")

        matrix = self._user_item_matrix.values

        if kind == "user":
            self._user_similarity = cosine_similarity(matrix)
            logger.info("User similarity matrix computed (%d users)", len(self._user_ids))
            return self._user_similarity
        elif kind == "item":
            self._item_similarity = cosine_similarity(matrix.T)
            logger.info("Item similarity matrix computed (%d items)", len(self._item_ids))
            return self._item_similarity
        else:
            raise ValueError(f"kind must be 'user' or 'item', got '{kind}'")

    # ------------------------------------------------------------------
    # Content-based helpers
    # ------------------------------------------------------------------

    def set_item_features(self, item_features: pd.DataFrame) -> None:
        """Register item feature vectors for content-based scoring.

        Parameters
        ----------
        item_features:
            DataFrame indexed by ``item_id`` where each column is a numeric
            feature (e.g. category one-hot, price normalised, etc.).
        """
        self._item_features = item_features
        self._item_similarity = cosine_similarity(item_features.fillna(0).values)
        logger.info(
            "Item features set: %d items x %d features",
            item_features.shape[0],
            item_features.shape[1],
        )

    # ------------------------------------------------------------------
    # Scoring helpers (private)
    # ------------------------------------------------------------------

    def _collaborative_scores(self, customer_id: str, n: int) -> dict[str, float]:
        """Return item scores derived from similar users' interactions."""
        if self._user_item_matrix is None or self._user_similarity is None:
            return {}

        if customer_id not in self._user_ids:
            return {}

        user_idx = self._user_ids.index(customer_id)
        sim_row = self._user_similarity[user_idx]

        # Weighted sum of similar users' interaction vectors
        scores = sim_row @ self._user_item_matrix.values

        # Zero out items the user already interacted with
        already_seen = self._user_item_matrix.iloc[user_idx].values > 0
        scores[already_seen] = -np.inf

        # Top-n
        top_indices = np.argsort(scores)[::-1][:n]
        return {
            self._item_ids[i]: float(scores[i])
            for i in top_indices
            if scores[i] > -np.inf
        }

    def _content_scores(self, customer_id: str, n: int) -> dict[str, float]:
        """Return item scores based on content similarity to user history."""
        if (
            self._item_features is None
            or self._item_similarity is None
            or self._user_item_matrix is None
        ):
            return {}

        if customer_id not in self._user_ids:
            return {}

        user_idx = self._user_ids.index(customer_id)
        user_row = self._user_item_matrix.iloc[user_idx].values

        # Items the user interacted with
        interacted_mask = user_row > 0
        if not interacted_mask.any():
            return {}

        item_feature_ids = list(self._item_features.index)
        overlap_item_ids = [
            iid for iid in self._item_ids if iid in item_feature_ids
        ]
        if not overlap_item_ids:
            return {}

        # Mean feature vector of interacted items (among those in item_features)
        interacted_items = [
            self._item_ids[i]
            for i in range(len(self._item_ids))
            if interacted_mask[i] and self._item_ids[i] in item_feature_ids
        ]
        if not interacted_items:
            return {}

        interacted_vecs = self._item_features.loc[interacted_items].fillna(0).values
        user_profile = interacted_vecs.mean(axis=0).reshape(1, -1)

        all_item_vecs = self._item_features.fillna(0).values
        sims = cosine_similarity(user_profile, all_item_vecs)[0]

        scores: dict[str, float] = {}
        for idx, item_id in enumerate(item_feature_ids):
            if item_id not in interacted_items:
                scores[item_id] = float(sims[idx])

        # Sort and take top-n
        sorted_items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return dict(sorted_items)

    # ------------------------------------------------------------------
    # Public recommendation interface
    # ------------------------------------------------------------------

    def get_recommendations(
        self,
        customer_id: str,
        n: int = 10,
        strategy: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Return the top-*n* recommendations for a customer.

        Parameters
        ----------
        customer_id:
            UUID (as string) of the target customer.
        n:
            Number of recommendations to return.
        strategy:
            ``"collaborative"``, ``"content"``, or ``"hybrid"`` (default).

        Returns
        -------
        list[dict]
            Sorted list of ``{"item_id", "score", "strategy"}`` dicts.
        """
        if strategy == "collaborative":
            self._ensure_user_similarity()
            scores = self._collaborative_scores(customer_id, n)
            tag = "collaborative"
        elif strategy == "content":
            scores = self._content_scores(customer_id, n)
            tag = "content"
        elif strategy == "hybrid":
            self._ensure_user_similarity()
            collab = self._collaborative_scores(customer_id, n * 2)
            content = self._content_scores(customer_id, n * 2)
            scores = self._merge_scores(collab, content)
            tag = "hybrid"
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

        # Build result list
        sorted_items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return [
            {"item_id": item_id, "score": round(score, 4), "strategy": tag}
            for item_id, score in sorted_items
        ]

    # ------------------------------------------------------------------
    # Async database integration
    # ------------------------------------------------------------------

    async def generate_recommendations(
        self,
        db_session: AsyncSession,
        customer_id: uuid.UUID,
        n: int = 10,
        strategy: str = "hybrid",
        recommendation_type: str = "product",
    ) -> list[dict[str, Any]]:
        """Generate recommendations for a customer and persist them.

        Parameters
        ----------
        db_session:
            Active async SQLAlchemy session.
        customer_id:
            Target customer UUID.
        n:
            Number of recommendations.
        strategy:
            ``"collaborative"``, ``"content"``, or ``"hybrid"``.
        recommendation_type:
            Label stored alongside each recommendation (e.g. ``"product"``).

        Returns
        -------
        list[dict]
            The generated recommendation dicts.
        """
        logger.info(
            "Generating %d %s recommendations for customer %s",
            n, strategy, customer_id,
        )

        # Build interaction matrix from events if not already loaded
        if self._user_item_matrix is None:
            await self._build_matrix_from_db(db_session)

        cid_str = str(customer_id)
        recs = self.get_recommendations(cid_str, n=n, strategy=strategy)

        # Remove old recommendations for this customer + type
        await db_session.execute(
            delete(Recommendation).where(
                Recommendation.customer_id == customer_id,
                Recommendation.recommendation_type == recommendation_type,
            )
        )

        # Persist new ones
        for rec in recs:
            db_session.add(
                Recommendation(
                    customer_id=customer_id,
                    recommendation_type=recommendation_type,
                    item_id=rec["item_id"],
                    score=rec["score"],
                    reason=f"Generated via {rec['strategy']} filtering",
                    context={"strategy": rec["strategy"]},
                )
            )

        await db_session.commit()
        logger.info(
            "%d recommendations saved for customer %s",
            len(recs), customer_id,
        )
        return recs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_user_similarity(self) -> None:
        """Compute user similarity if it has not been done yet."""
        if self._user_similarity is None and self._user_item_matrix is not None:
            self.compute_similarity_matrix(kind="user")

    def _merge_scores(
        self,
        collab: dict[str, float],
        content: dict[str, float],
    ) -> dict[str, float]:
        """Weighted merge of collaborative and content-based scores."""
        all_items = set(collab) | set(content)
        merged: dict[str, float] = {}

        # Normalise each score dict to [0, 1] for fair blending
        collab_normed = self._normalise_scores(collab)
        content_normed = self._normalise_scores(content)

        for item_id in all_items:
            c_score = collab_normed.get(item_id, 0.0)
            t_score = content_normed.get(item_id, 0.0)
            merged[item_id] = (
                self.collaborative_weight * c_score
                + self.content_weight * t_score
            )

        return merged

    @staticmethod
    def _normalise_scores(scores: dict[str, float]) -> dict[str, float]:
        """Min-max normalise a score dict to [0, 1]."""
        if not scores:
            return {}
        vals = list(scores.values())
        min_v, max_v = min(vals), max(vals)
        rng = max_v - min_v
        if rng == 0:
            return {k: 1.0 for k in scores}
        return {k: (v - min_v) / rng for k, v in scores.items()}

    async def _build_matrix_from_db(self, db_session: AsyncSession) -> None:
        """Populate the user-item matrix from customer events in the DB.

        Treats ``event_type='purchase'`` events whose ``properties`` contain
        an ``item_id`` key as interactions.
        """
        stmt = (
            select(
                CustomerEvent.customer_id,
                CustomerEvent.properties,
                CustomerEvent.event_type,
            )
            .where(
                CustomerEvent.event_type.in_(["purchase", "view", "add_to_cart", "wishlist"])
            )
        )
        result = await db_session.execute(stmt)
        rows = result.all()

        if not rows:
            logger.warning("No interaction events found; matrix will be empty.")
            return

        # Weight map for different interaction types
        interaction_weights = {
            "purchase": 5.0,
            "add_to_cart": 3.0,
            "wishlist": 2.0,
            "view": 1.0,
        }

        records: list[dict[str, Any]] = []
        for customer_id, properties, event_type in rows:
            item_id = (properties or {}).get("item_id")
            if item_id:
                records.append({
                    "customer_id": str(customer_id),
                    "item_id": str(item_id),
                    "score": interaction_weights.get(event_type, 1.0),
                })

        if records:
            interactions_df = pd.DataFrame(records)
            self.update_user_item_matrix(interactions_df)
        else:
            logger.warning("No item_id found in event properties; matrix is empty.")
