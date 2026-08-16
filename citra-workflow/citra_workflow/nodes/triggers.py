"""Trigger nodes — entry points for workflows."""

from __future__ import annotations
import json
from typing import Any, List

from ..models import NodeType, NodeCategory, NodeFieldSchema
from . import BaseNode, NodeContext, register_node


def _coerce_schema_fields(value: Any) -> List[dict]:
    """Normalize schema-builder values saved as native JSON or JSON text."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, dict):
        for key in ("fields", "schema", "input_schema"):
            nested = value.get(key)
            if isinstance(nested, list):
                value = nested
                break
        else:
            return [value] if value.get("name") else []
    if not isinstance(value, list):
        return []
    return [field for field in value if isinstance(field, dict)]


@register_node
class ManualTriggerNode(BaseNode):
    node_type = NodeType.MANUAL_TRIGGER
    category = NodeCategory.TRIGGER
    label = "Manual Trigger"
    description = "Starts workflow when user clicks Run"
    icon = "▶️"
    color = "#22c55e"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="input_schema", label="Variables", type="schema_builder",
                default=[],
                help_text="Define variables with default values. "
                          "These become {{variable}} placeholders in downstream nodes.",
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        schema_fields = _coerce_schema_fields(ctx.config.get("input_schema", []))
        variables = dict(ctx.variables or {})

        if schema_fields:
            for field_def in schema_fields:
                name = field_def.get("name", "")
                default = field_def.get("default")
                value = variables.get(name)
                if value is None and default is not None:
                    value = default
                if value is not None:
                    field_type = field_def.get("type", "string")
                    if field_type == "number":
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            pass
                    elif field_type == "boolean":
                        value = str(value).lower() in ("true", "1", "yes")
                    variables[name] = value

        # Bug 002/#5: emit the trigger variables as a single row so downstream
        # tabular nodes (Data Transform, etc.) have data to operate on — matching
        # how Start/Webhook triggers already surface their inputs as items.
        # Variables remain in meta.variables for {{placeholder}} use.
        return self._make_output(
            items=[variables] if variables else [],
            triggered=True, trigger_type="manual", variables=variables,
        )


@register_node
class StartNode(BaseNode):
    node_type = NodeType.START_NODE
    category = NodeCategory.TRIGGER
    label = "Start"
    description = "Workflow entry point with typed input schema — defines what data the workflow expects"
    icon = "🚀"
    color = "#10b981"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="input_schema", label="Input Schema", type="schema_builder",
                default=[],
                help_text="Define the input fields this workflow expects (name, type, required, default)",
            ),
            NodeFieldSchema(
                name="description", label="Workflow Description", type="textarea",
                placeholder="Describe what this workflow does and what inputs it needs",
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        schema_fields = _coerce_schema_fields(ctx.config.get("input_schema", []))
        variables = dict(ctx.variables or {})
        input_data = ctx.input_data or {}

        # Validate inputs against schema
        errors = []
        validated = {}
        for field_def in schema_fields:
            name = field_def.get("name", "")
            field_type = field_def.get("type", "string")
            required = field_def.get("required", False)
            default = field_def.get("default")

            value = input_data.get(name) or variables.get(name)
            if value is None and default is not None:
                value = default
            if value is None and required:
                errors.append(f"Missing required input: {name}")
                continue

            # Type coercion
            if value is not None:
                if field_type == "number":
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        errors.append(f"Input '{name}' must be a number")
                elif field_type == "boolean":
                    value = str(value).lower() in ("true", "1", "yes")
                elif field_type == "json":
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            errors.append(f"Input '{name}' must be valid JSON")

            validated[name] = value

        if errors:
            return self._make_output(items=[], errors=errors, valid=False)

        # Merge validated inputs into variables
        variables.update(validated)
        return self._make_output(
            items=[validated] if validated else [],
            triggered=True, trigger_type="start",
            inputs=validated, variables=variables,
            input_schema=schema_fields,
        )


@register_node
class ScheduledTriggerNode(BaseNode):
    node_type = NodeType.SCHEDULED_TRIGGER
    category = NodeCategory.TRIGGER
    label = "Scheduled Trigger"
    description = "Runs workflow on a cron schedule"
    icon = "⏰"
    color = "#22c55e"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="cron_expression", label="Cron Expression", type="cron",
                            required=True, placeholder="0 9 * * 1", help_text="e.g. 0 9 * * 1 = Monday 9 AM"),
            NodeFieldSchema(name="timezone", label="Timezone", type="text", default="America/New_York"),
            NodeFieldSchema(
                name="input_schema", label="Variables", type="schema_builder",
                default=[],
                help_text="Define variables with default values. "
                          "These become {{variable}} placeholders in downstream nodes.",
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        schema_fields = _coerce_schema_fields(ctx.config.get("input_schema", []))
        variables = dict(ctx.variables or {})

        if schema_fields:
            for field_def in schema_fields:
                name = field_def.get("name", "")
                default = field_def.get("default")
                value = variables.get(name)
                if value is None and default is not None:
                    value = default
                if value is not None:
                    field_type = field_def.get("type", "string")
                    if field_type == "number":
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            pass
                    elif field_type == "boolean":
                        value = str(value).lower() in ("true", "1", "yes")
                    variables[name] = value

        # Bug 002/#5: emit variables as a single row (consistent with Manual/
        # Start/Webhook) so downstream tabular nodes have data on each fire.
        return self._make_output(
            items=[variables] if variables else [],
            triggered=True, trigger_type="scheduled",
            cron=ctx.config.get("cron_expression"), variables=variables,
        )


@register_node
class WebhookTriggerNode(BaseNode):
    node_type = NodeType.WEBHOOK_TRIGGER
    category = NodeCategory.TRIGGER
    label = "Webhook Trigger"
    description = "Starts workflow when an external system sends a POST request"
    icon = "🔗"
    color = "#22c55e"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="input_schema", label="Expected Parameters", type="schema_builder",
                default=[],
                help_text="Define the fields that the webhook payload should contain. "
                          "These become {{variable}} placeholders in downstream nodes.",
            ),
            NodeFieldSchema(
                name="sample_payload", label="Sample Payload (JSON)", type="json",
                default={},
                help_text="Paste a sample JSON payload for testing with the Run button",
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        schema_fields = _coerce_schema_fields(ctx.config.get("input_schema", []))
        variables = dict(ctx.variables or {})
        payload = ctx.input_data or {}

        # If schema is defined, validate & coerce incoming payload
        if schema_fields:
            errors = []
            validated = {}
            for field_def in schema_fields:
                name = field_def.get("name", "")
                field_type = field_def.get("type", "string")
                required = field_def.get("required", False)
                default = field_def.get("default")

                value = payload.get(name) or variables.get(name)
                if value is None and default is not None:
                    value = default
                if value is None and required:
                    errors.append(f"Missing required webhook field: {name}")
                    continue

                # Type coercion
                if value is not None:
                    if field_type == "number":
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            errors.append(f"Webhook field '{name}' must be a number")
                    elif field_type == "boolean":
                        value = str(value).lower() in ("true", "1", "yes")
                    elif field_type == "json":
                        if isinstance(value, str):
                            try:
                                value = json.loads(value)
                            except json.JSONDecodeError:
                                errors.append(f"Webhook field '{name}' must be valid JSON")

                validated[name] = value

            if errors:
                raise ValueError(f"Webhook validation failed: {'; '.join(errors)}")

            variables.update(validated)
            return self._make_output(
                items=[validated] if validated else [],
                triggered=True, trigger_type="webhook",
                payload=payload, variables=variables,
                inputs=validated, input_schema=schema_fields,
            )

        # No schema defined — pass everything through
        variables.update(payload)
        return self._make_output(
            items=[payload] if payload else [],
            triggered=True, trigger_type="webhook",
            payload=payload, variables=variables,
        )
