"""Automation workflow engine with JSON-based workflow definitions, multiple trigger
types, and a background queue processor.

Supports visual workflow builder patterns through a declarative step model with
branching, conditions, waits, and multi-channel actions.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models.customer import Customer, CustomerAttribute, CustomerEvent
from cdp.models.marketing import (
    AutomationWorkflow,
    TriggerType,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from cdp.services.marketing.audience_builder import AudienceBuilder
from cdp.services.marketing.channel_sender import ChannelSender

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step type constants
# ---------------------------------------------------------------------------

class StepType:
    SEND_EMAIL = "send_email"
    SEND_SMS = "send_sms"
    WAIT = "wait"
    CONDITION = "condition"
    SPLIT = "split"
    UPDATE_PROFILE = "update_profile"
    WEBHOOK = "webhook"


# ---------------------------------------------------------------------------
# Workflow templates for common scenarios
# ---------------------------------------------------------------------------

WORKFLOW_TEMPLATES: dict[str, dict[str, Any]] = {
    "welcome_series": {
        "name": "Welcome Series",
        "description": "Onboarding sequence for new customers with graduated engagement.",
        "trigger_type": TriggerType.EVENT.value,
        "trigger_config": {"event_type": "identify", "event_name": "signup"},
        "steps": [
            {
                "step_index": 0,
                "type": StepType.SEND_EMAIL,
                "config": {
                    "subject": "Welcome to {{ company_name }}, {{ first_name }}!",
                    "body": "Thank you for joining us. Here is what to expect...",
                },
            },
            {
                "step_index": 1,
                "type": StepType.WAIT,
                "config": {"duration_hours": 24},
            },
            {
                "step_index": 2,
                "type": StepType.CONDITION,
                "config": {
                    "field": "metadata_.onboarding_complete",
                    "operator": "eq",
                    "value": True,
                    "true_step": 4,
                    "false_step": 3,
                },
            },
            {
                "step_index": 3,
                "type": StepType.SEND_EMAIL,
                "config": {
                    "subject": "Need help getting started, {{ first_name }}?",
                    "body": "We noticed you haven't finished setting up...",
                },
            },
            {
                "step_index": 4,
                "type": StepType.SEND_EMAIL,
                "config": {
                    "subject": "You're all set, {{ first_name }}!",
                    "body": "Great job completing your setup. Here are some tips...",
                },
            },
        ],
    },
    "cart_abandonment": {
        "name": "Cart Abandonment",
        "description": "Recover abandoned carts with timed reminders and a discount incentive.",
        "trigger_type": TriggerType.EVENT.value,
        "trigger_config": {"event_type": "track", "event_name": "cart_abandoned"},
        "steps": [
            {
                "step_index": 0,
                "type": StepType.WAIT,
                "config": {"duration_hours": 1},
            },
            {
                "step_index": 1,
                "type": StepType.CONDITION,
                "config": {
                    "field": "metadata_.cart_recovered",
                    "operator": "eq",
                    "value": True,
                    "true_step": -1,  # -1 signals end
                    "false_step": 2,
                },
            },
            {
                "step_index": 2,
                "type": StepType.SEND_EMAIL,
                "config": {
                    "subject": "You left something behind, {{ first_name }}",
                    "body": "Your cart is still waiting for you...",
                },
            },
            {
                "step_index": 3,
                "type": StepType.WAIT,
                "config": {"duration_hours": 24},
            },
            {
                "step_index": 4,
                "type": StepType.CONDITION,
                "config": {
                    "field": "metadata_.cart_recovered",
                    "operator": "eq",
                    "value": True,
                    "true_step": -1,
                    "false_step": 5,
                },
            },
            {
                "step_index": 5,
                "type": StepType.SEND_EMAIL,
                "config": {
                    "subject": "Last chance! 10% off your cart, {{ first_name }}",
                    "body": "Use code COMEBACK10 to save 10% on your order...",
                },
            },
        ],
    },
    "re_engagement": {
        "name": "Re-engagement",
        "description": "Win-back sequence for customers who have been inactive.",
        "trigger_type": TriggerType.SEGMENT.value,
        "trigger_config": {
            "criteria": {
                "match": "all",
                "rules": [
                    {
                        "type": "behavioral",
                        "event_type": "page_view",
                        "operator": "eq",
                        "value": 0,
                        "time_window_days": 60,
                    },
                ],
            }
        },
        "steps": [
            {
                "step_index": 0,
                "type": StepType.SEND_EMAIL,
                "config": {
                    "subject": "We miss you, {{ first_name }}!",
                    "body": "It has been a while since your last visit...",
                },
            },
            {
                "step_index": 1,
                "type": StepType.WAIT,
                "config": {"duration_hours": 72},
            },
            {
                "step_index": 2,
                "type": StepType.CONDITION,
                "config": {
                    "field": "engagement_score",
                    "operator": "gt",
                    "value": 0,
                    "true_step": -1,
                    "false_step": 3,
                },
            },
            {
                "step_index": 3,
                "type": StepType.SEND_SMS,
                "config": {
                    "message": "Hi {{ first_name }}, we have some exciting updates for you. Check your inbox!",
                },
            },
            {
                "step_index": 4,
                "type": StepType.WAIT,
                "config": {"duration_hours": 48},
            },
            {
                "step_index": 5,
                "type": StepType.UPDATE_PROFILE,
                "config": {
                    "updates": {"segment": "churned"},
                },
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------

class WorkflowEngine:
    """Executes automation workflows defined as JSON step sequences.

    Workflows progress through ordered steps for each customer.  The engine
    handles branching via condition and split steps, timed waits, multi-channel
    sends, profile updates, and outbound webhooks.

    Parameters
    ----------
    audience_builder:
        For resolving segment-based triggers.
    channel_sender:
        For delivering messages across channels.
    """

    def __init__(
        self,
        audience_builder: AudienceBuilder | None = None,
        channel_sender: ChannelSender | None = None,
    ) -> None:
        self._audience_builder = audience_builder or AudienceBuilder()
        self._channel_sender = channel_sender or ChannelSender()

    # ------------------------------------------------------------------
    # Workflow CRUD
    # ------------------------------------------------------------------

    async def create_workflow(
        self,
        db_session: AsyncSession,
        workflow_data: dict[str, Any],
    ) -> AutomationWorkflow:
        """Create a new automation workflow.

        Parameters
        ----------
        workflow_data:
            Column values for :class:`AutomationWorkflow`.  May include
            ``"template"`` key to populate from a built-in template.
        """
        template_name = workflow_data.pop("template", None)
        if template_name and template_name in WORKFLOW_TEMPLATES:
            template = WORKFLOW_TEMPLATES[template_name]
            for key, value in template.items():
                workflow_data.setdefault(key, value)

        workflow_data.setdefault("id", uuid.uuid4())
        workflow_data.setdefault("status", "draft")

        workflow = AutomationWorkflow(**workflow_data)
        db_session.add(workflow)
        await db_session.flush()
        await db_session.refresh(workflow)
        logger.info("Created workflow '%s' (%s)", workflow.name, workflow.id)
        return workflow

    async def get_workflow(
        self,
        db_session: AsyncSession,
        workflow_id: uuid.UUID,
    ) -> AutomationWorkflow | None:
        stmt = select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id)
        result = await db_session.execute(stmt)
        return result.scalars().first()

    async def activate_workflow(
        self,
        db_session: AsyncSession,
        workflow_id: uuid.UUID,
    ) -> AutomationWorkflow:
        """Set a workflow to 'active' so it can accept new executions."""
        workflow = await self._get_workflow_or_raise(db_session, workflow_id)
        workflow.status = "active"
        await db_session.flush()
        logger.info("Activated workflow %s", workflow_id)
        return workflow

    async def deactivate_workflow(
        self,
        db_session: AsyncSession,
        workflow_id: uuid.UUID,
    ) -> AutomationWorkflow:
        """Deactivate a workflow so no new executions are started."""
        workflow = await self._get_workflow_or_raise(db_session, workflow_id)
        workflow.status = "inactive"
        await db_session.flush()
        logger.info("Deactivated workflow %s", workflow_id)
        return workflow

    @staticmethod
    def list_templates() -> dict[str, dict[str, Any]]:
        """Return available workflow templates."""
        return {
            name: {"name": t["name"], "description": t["description"]}
            for name, t in WORKFLOW_TEMPLATES.items()
        }

    # ------------------------------------------------------------------
    # Execution management
    # ------------------------------------------------------------------

    async def start_workflow(
        self,
        db_session: AsyncSession,
        workflow_id: uuid.UUID,
        customer_id: uuid.UUID,
        *,
        context: dict[str, Any] | None = None,
    ) -> WorkflowExecution:
        """Start a workflow execution for a specific customer.

        Raises
        ------
        ValueError
            If the workflow is not active.
        """
        workflow = await self._get_workflow_or_raise(db_session, workflow_id)

        if workflow.status != "active":
            raise ValueError(
                f"Cannot start execution for workflow in '{workflow.status}' status; "
                "must be active"
            )

        execution = WorkflowExecution(
            id=uuid.uuid4(),
            workflow_id=workflow_id,
            customer_id=customer_id,
            current_step=0,
            status=WorkflowExecutionStatus.RUNNING.value,
            context=context or {},
        )
        db_session.add(execution)
        await db_session.flush()
        await db_session.refresh(execution)
        logger.info(
            "Started workflow %s for customer %s (execution %s)",
            workflow_id,
            customer_id,
            execution.id,
        )
        return execution

    async def execute_step(
        self,
        db_session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Execute the current step of a workflow execution and advance.

        Returns a summary dict describing the step outcome.
        """
        execution = await self._get_execution_or_raise(db_session, execution_id)

        if execution.status != WorkflowExecutionStatus.RUNNING.value:
            raise ValueError(
                f"Execution {execution_id} is in '{execution.status}' status; "
                "must be RUNNING"
            )

        workflow = await self._get_workflow_or_raise(db_session, execution.workflow_id)
        steps: list[dict[str, Any]] = workflow.steps or []

        current_index = execution.current_step or 0
        if current_index < 0 or current_index >= len(steps):
            # No more steps -- mark completed
            execution.status = WorkflowExecutionStatus.COMPLETED.value
            execution.completed_at = datetime.now(timezone.utc)
            await db_session.flush()
            return {"execution_id": str(execution_id), "status": "completed", "step": None}

        step = steps[current_index]
        step_type = step.get("type", "")
        config = step.get("config", {})

        # Load customer for steps that need it
        customer = await self._load_customer(db_session, execution.customer_id)
        if customer is None:
            execution.status = WorkflowExecutionStatus.FAILED.value
            execution.completed_at = datetime.now(timezone.utc)
            await db_session.flush()
            raise ValueError(f"Customer {execution.customer_id} not found")

        template_vars = self._customer_template_vars(customer)
        result: dict[str, Any] = {
            "execution_id": str(execution_id),
            "step_index": current_index,
            "step_type": step_type,
        }

        try:
            next_step = current_index + 1  # default: advance linearly

            if step_type == StepType.SEND_EMAIL:
                await self._step_send_email(customer, config, template_vars)
                result["action"] = "email_sent"

            elif step_type == StepType.SEND_SMS:
                await self._step_send_sms(customer, config, template_vars)
                result["action"] = "sms_sent"

            elif step_type == StepType.WAIT:
                wait_until = self._step_wait(config, execution)
                result["action"] = "waiting"
                result["wait_until"] = wait_until.isoformat()
                # Do not advance yet -- the queue processor will re-evaluate
                # once the wait period has elapsed.
                ctx = execution.context or {}
                ctx["wait_until"] = wait_until.isoformat()
                execution.context = ctx
                await db_session.flush()
                return result

            elif step_type == StepType.CONDITION:
                branch = self.evaluate_condition(config, customer)
                next_step = config.get("true_step" if branch else "false_step", next_step)
                result["action"] = "condition_evaluated"
                result["branch"] = branch
                if next_step == -1:
                    execution.status = WorkflowExecutionStatus.COMPLETED.value
                    execution.completed_at = datetime.now(timezone.utc)
                    await db_session.flush()
                    result["status"] = "completed"
                    return result

            elif step_type == StepType.SPLIT:
                next_step = self._step_split(config, customer, current_index)
                result["action"] = "split_evaluated"
                result["next_step"] = next_step

            elif step_type == StepType.UPDATE_PROFILE:
                await self._step_update_profile(db_session, customer, config)
                result["action"] = "profile_updated"

            elif step_type == StepType.WEBHOOK:
                await self._step_webhook(config, customer, template_vars)
                result["action"] = "webhook_sent"

            else:
                raise ValueError(f"Unknown step type: {step_type}")

            # Advance to the next step
            execution.current_step = next_step
            if next_step >= len(steps) or next_step < 0:
                execution.status = WorkflowExecutionStatus.COMPLETED.value
                execution.completed_at = datetime.now(timezone.utc)
                result["status"] = "completed"
            else:
                result["status"] = "running"

            await db_session.flush()

        except Exception as exc:
            execution.status = WorkflowExecutionStatus.FAILED.value
            execution.completed_at = datetime.now(timezone.utc)
            await db_session.flush()
            logger.error(
                "Workflow execution %s failed at step %d: %s",
                execution_id,
                current_index,
                exc,
            )
            result["status"] = "failed"
            result["error"] = str(exc)

        return result

    # ------------------------------------------------------------------
    # Condition evaluator
    # ------------------------------------------------------------------

    @staticmethod
    def evaluate_condition(condition: dict[str, Any], customer: Customer) -> bool:
        """Evaluate a condition dict against a customer, returning ``True``/``False``.

        The condition schema::

            {
                "field": "lifetime_value",  # dotted path into customer attrs
                "operator": "gte",
                "value": 100.0
            }

        Supported operators: eq, neq, gt, gte, lt, lte, in, not_in, contains,
        is_null, is_not_null.
        """
        field_path: str = condition.get("field", "")
        operator: str = condition.get("operator", "eq")
        expected = condition.get("value")

        # Resolve dotted field path (e.g. "metadata_.vip_tier")
        actual = _resolve_field(customer, field_path)

        if operator == "eq":
            return actual == expected
        if operator == "neq":
            return actual != expected
        if operator == "gt":
            return actual is not None and actual > expected
        if operator == "gte":
            return actual is not None and actual >= expected
        if operator == "lt":
            return actual is not None and actual < expected
        if operator == "lte":
            return actual is not None and actual <= expected
        if operator == "in":
            return actual in (expected or [])
        if operator == "not_in":
            return actual not in (expected or [])
        if operator == "contains":
            return expected in (actual or "")
        if operator == "is_null":
            return actual is None
        if operator == "is_not_null":
            return actual is not None

        logger.warning("Unknown condition operator '%s', defaulting to False", operator)
        return False

    # ------------------------------------------------------------------
    # Background queue processor
    # ------------------------------------------------------------------

    async def process_workflow_queue(
        self,
        db_session: AsyncSession,
        *,
        batch_size: int = 50,
        poll_interval: float = 2.0,
    ) -> None:
        """Long-running background worker that advances pending workflow executions.

        Picks up RUNNING executions, respects wait steps, and processes each
        in turn.  Designed to be run as an ``asyncio.Task``.
        """
        logger.info("Workflow queue processor started (batch_size=%d)", batch_size)

        while True:
            try:
                stmt = (
                    select(WorkflowExecution)
                    .where(WorkflowExecution.status == WorkflowExecutionStatus.RUNNING.value)
                    .order_by(WorkflowExecution.started_at.asc())
                    .limit(batch_size)
                )
                result = await db_session.execute(stmt)
                executions = list(result.scalars().all())

                if not executions:
                    await asyncio.sleep(poll_interval)
                    continue

                for execution in executions:
                    try:
                        # Check if a wait period is still active
                        ctx = execution.context or {}
                        wait_until_str = ctx.get("wait_until")
                        if wait_until_str:
                            wait_until = datetime.fromisoformat(wait_until_str)
                            if wait_until.tzinfo is None:
                                wait_until = wait_until.replace(tzinfo=timezone.utc)
                            if datetime.now(timezone.utc) < wait_until:
                                continue  # Still waiting
                            # Clear the wait marker and advance past the wait step
                            ctx.pop("wait_until", None)
                            execution.context = ctx
                            execution.current_step = (execution.current_step or 0) + 1
                            await db_session.flush()

                        await self.execute_step(db_session, execution.id)
                    except Exception:
                        logger.exception(
                            "Error processing workflow execution %s", execution.id
                        )

                await db_session.flush()

            except asyncio.CancelledError:
                logger.info("Workflow queue processor stopping")
                break
            except Exception:
                logger.exception("Workflow queue processor error, retrying")
                await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    async def _step_send_email(
        self,
        customer: Customer,
        config: dict[str, Any],
        template_vars: dict[str, Any],
    ) -> None:
        if not customer.email:
            raise ValueError(f"Customer {customer.id} has no email for workflow email step")
        subject = config.get("subject", "")
        body = config.get("body", "")
        await self._channel_sender.send_email(
            to=customer.email,
            subject=subject,
            body=body,
            template_vars=template_vars,
        )

    async def _step_send_sms(
        self,
        customer: Customer,
        config: dict[str, Any],
        template_vars: dict[str, Any],
    ) -> None:
        if not customer.phone:
            raise ValueError(f"Customer {customer.id} has no phone for workflow SMS step")
        message = config.get("message", "")
        await self._channel_sender.send_sms(
            to=customer.phone,
            message=message,
            template_vars=template_vars,
        )

    @staticmethod
    def _step_wait(
        config: dict[str, Any],
        execution: WorkflowExecution,
    ) -> datetime:
        """Calculate the datetime when the wait step expires."""
        duration_hours = config.get("duration_hours", 1)
        duration_minutes = config.get("duration_minutes", 0)
        total_delta = timedelta(hours=duration_hours, minutes=duration_minutes)
        return datetime.now(timezone.utc) + total_delta

    @staticmethod
    def _step_split(
        config: dict[str, Any],
        customer: Customer,
        current_index: int,
    ) -> int:
        """Evaluate a multi-branch split and return the next step index.

        Split config schema::

            {
                "branches": [
                    {"field": "segment", "operator": "eq", "value": "vip", "goto": 5},
                    {"field": "segment", "operator": "eq", "value": "regular", "goto": 8},
                ],
                "default": 10
            }
        """
        branches: list[dict[str, Any]] = config.get("branches", [])
        for branch in branches:
            actual = _resolve_field(customer, branch.get("field", ""))
            op = branch.get("operator", "eq")
            expected = branch.get("value")
            matched = False
            if op == "eq":
                matched = actual == expected
            elif op == "neq":
                matched = actual != expected
            elif op == "in":
                matched = actual in (expected or [])
            elif op == "gt":
                matched = actual is not None and actual > expected
            elif op == "gte":
                matched = actual is not None and actual >= expected
            elif op == "lt":
                matched = actual is not None and actual < expected
            elif op == "lte":
                matched = actual is not None and actual <= expected
            if matched:
                return branch.get("goto", current_index + 1)
        return config.get("default", current_index + 1)

    async def _step_update_profile(
        self,
        db_session: AsyncSession,
        customer: Customer,
        config: dict[str, Any],
    ) -> None:
        """Update customer profile fields from step config."""
        updates: dict[str, Any] = config.get("updates", {})
        if not updates:
            return
        for field_name, value in updates.items():
            if hasattr(customer, field_name):
                setattr(customer, field_name, value)
        customer.updated_at = datetime.now(timezone.utc)
        await db_session.flush()
        logger.info("Updated profile %s from workflow step", customer.id)

    async def _step_webhook(
        self,
        config: dict[str, Any],
        customer: Customer,
        template_vars: dict[str, Any],
    ) -> None:
        url = config.get("url", "")
        if not url:
            raise ValueError("Webhook step requires a 'url' in config")
        payload = config.get("payload", {})
        payload["customer_id"] = str(customer.id)
        payload.update(template_vars)
        headers = config.get("headers")
        await self._channel_sender.send_webhook(
            url=url,
            payload=payload,
            headers=headers,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_workflow_or_raise(
        self,
        db_session: AsyncSession,
        workflow_id: uuid.UUID,
    ) -> AutomationWorkflow:
        workflow = await self.get_workflow(db_session, workflow_id)
        if workflow is None:
            raise ValueError(f"Workflow {workflow_id} not found")
        return workflow

    async def _get_execution_or_raise(
        self,
        db_session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> WorkflowExecution:
        stmt = select(WorkflowExecution).where(WorkflowExecution.id == execution_id)
        result = await db_session.execute(stmt)
        execution = result.scalars().first()
        if execution is None:
            raise ValueError(f"Workflow execution {execution_id} not found")
        return execution

    @staticmethod
    async def _load_customer(
        db_session: AsyncSession,
        customer_id: uuid.UUID,
    ) -> Customer | None:
        stmt = select(Customer).where(Customer.id == customer_id)
        result = await db_session.execute(stmt)
        return result.scalars().first()

    @staticmethod
    def _customer_template_vars(customer: Customer) -> dict[str, Any]:
        return {
            "customer_id": str(customer.id),
            "email": customer.email or "",
            "first_name": customer.first_name or "",
            "last_name": customer.last_name or "",
            "full_name": f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
            "phone": customer.phone or "",
            "segment": customer.segment or "",
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _resolve_field(customer: Customer, field_path: str) -> Any:
    """Resolve a dotted field path on a Customer object.

    Examples:
        "email"                     -> customer.email
        "metadata_.vip_tier"        -> customer.metadata_["vip_tier"]
        "engagement_score"          -> customer.engagement_score
    """
    if not field_path:
        return None

    parts = field_path.split(".")
    obj: Any = customer

    for part in parts:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            obj = getattr(obj, part, None)

    return obj
