# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Logic / control-flow nodes — conditions, loops, approvals."""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, List

from ..models import NodeType, NodeCategory, NodeFieldSchema
from . import BaseNode, NodeContext, register_node, interpolate_variables
from ..config import MAX_FIELD_DEPTH as _MAX_FIELD_DEPTH, MAX_LOOP_ITERATIONS

logger = logging.getLogger(__name__)


@register_node
class ConditionNode(BaseNode):
    node_type = NodeType.CONDITION
    category = NodeCategory.LOGIC
    label = "Condition (If/Else)"
    description = "Branch workflow based on a condition"
    icon = "🔀"
    color = "#ec4899"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="field", label="Field to Check", type="text", required=True,
                            placeholder="count", help_text="Dot notation supported: evaluations.0.passed"),
            NodeFieldSchema(name="operator", label="Operator", type="select", required=True,
                            options=[
                                {"label": "Equals", "value": "=="},
                                {"label": "Not Equals", "value": "!="},
                                {"label": "Greater Than", "value": ">"},
                                {"label": "Less Than", "value": "<"},
                                {"label": "Contains", "value": "contains"},
                                {"label": "Is Empty", "value": "empty"},
                                {"label": "Is Not Empty", "value": "not_empty"},
                            ]),
            NodeFieldSchema(name="value", label="Value", type="text", placeholder="10"),
        ]

    @classmethod
    def get_schema(cls):
        schema = super().get_schema()
        schema.outputs = 2  # true branch, false branch
        return schema

    MAX_FIELD_DEPTH = _MAX_FIELD_DEPTH

    async def execute(self, ctx: NodeContext) -> Any:
        field_path = ctx.config["field"]
        operator = ctx.config["operator"]
        compare_value = ctx.config.get("value", "")

        # Navigate nested field with depth limit
        # Search order: input_data first, then workflow variables (for trigger-emitted vars)
        data = ctx.input_data
        parts = field_path.split(".")
        if len(parts) > self.MAX_FIELD_DEPTH:
            raise ValueError(
                f"Field path depth ({len(parts)}) exceeds maximum ({self.MAX_FIELD_DEPTH}). "
                f"Simplify the field path or increase MAX_FIELD_DEPTH."
            )
        for part in parts:
            if isinstance(data, dict):
                data = data.get(part)
            elif isinstance(data, list) and part.isdigit():
                data = data[int(part)] if int(part) < len(data) else None
            else:
                data = None
                break

        # Fallback 1: if field not found in input_data, check workflow variables
        # (Variables emitted by trigger/set_variable nodes live in ctx.variables)
        if data is None and parts:
            top_key = parts[0]
            if top_key in ctx.variables:
                data = ctx.variables[top_key]
                for part in parts[1:]:
                    if isinstance(data, dict):
                        data = data.get(part)
                    elif isinstance(data, list) and part.isdigit():
                        data = data[int(part)] if int(part) < len(data) else None
                    else:
                        data = None
                        break

        # Fallback 2: node outputs are shaped {"items": [ {...} ], "meta": {...}}.
        # A bare field name (e.g. "score") naturally refers to a field on the
        # record, not the envelope — so when the path didn't resolve against the
        # envelope or variables, retry it against the FIRST item. This makes the
        # intuitive `field=score` work without forcing users (or the AI builder)
        # to write `items.0.score` (Bug 006a — silent-false on bare field path).
        if data is None and parts and isinstance(ctx.input_data, dict):
            first_item = None
            items = ctx.input_data.get("items")
            if isinstance(items, list) and items:
                first_item = items[0]
            if isinstance(first_item, (dict, list)):
                data = first_item
                for part in parts:
                    if isinstance(data, dict):
                        data = data.get(part)
                    elif isinstance(data, list) and part.isdigit():
                        data = data[int(part)] if int(part) < len(data) else None
                    else:
                        data = None
                        break

        # Not-found is no longer silent: log a warning so an unresolved field is
        # debuggable (it still evaluates with data=None, e.g. `empty` → True).
        if data is None:
            logger.warning(
                "Condition field '%s' did not resolve against input_data, items[0], "
                "or variables — evaluating with no value (operator=%s).",
                field_path, operator,
            )

        # Evaluate condition
        result = False
        if operator == "==":
            result = str(data) == str(compare_value)
        elif operator == "!=":
            result = str(data) != str(compare_value)
        elif operator == ">":
            try:
                result = float(data) > float(compare_value)
            except (TypeError, ValueError):
                result = False
        elif operator == "<":
            try:
                result = float(data) < float(compare_value)
            except (TypeError, ValueError):
                result = False
        elif operator == "contains":
            result = str(compare_value).lower() in str(data or "").lower()
        elif operator == "empty":
            result = data is None or data == "" or data == [] or data == {}
        elif operator == "not_empty":
            result = data is not None and data != "" and data != [] and data != {}

        return self._make_output(
            items=ctx.items,
            condition_result=result,
            branch="true" if result else "false",
        )


@register_node
class LoopNode(BaseNode):
    node_type = NodeType.LOOP
    category = NodeCategory.LOGIC
    label = "Loop (For Each)"
    description = "Iterate over records and process each one"
    icon = "🔁"
    color = "#ec4899"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="items_field", label="Items Field", type="text", default="records",
                            help_text="Field name containing the array to iterate over"),
            NodeFieldSchema(name="batch_size", label="Batch Size", type="number", default=1,
                            help_text="Process N records at a time (1 = one by one)"),
        ]

    MAX_ITERATIONS = MAX_LOOP_ITERATIONS

    async def execute(self, ctx: NodeContext) -> Any:
        items_field = ctx.config.get("items_field", "items")
        data = ctx.input_data
        # Read items from envelope or fallback field
        if isinstance(data, dict):
            items = data.get(items_field, data.get("items", []))
        else:
            items = []
        batch_size = max(1, int(ctx.config.get("batch_size", 1)))

        # Reject oversized loops — never truncate silently
        if len(items) > self.MAX_ITERATIONS:
            raise ValueError(
                f"Loop items count ({len(items)}) exceeds maximum allowed iterations ({self.MAX_ITERATIONS}). "
                f"Add a filter node upstream to reduce the dataset, or set WF_MAX_LOOP_ITERATIONS "
                f"environment variable to increase the limit."
            )

        # The executor handles actual iteration; this node just prepares batches
        batches = []
        for i in range(0, len(items), batch_size):
            batches.append(items[i:i + batch_size])

        return self._make_output(
            items=items,
            batches=batches,
            total_items=len(items),
            total_batches=len(batches),
            batch_size=batch_size,
        )


@register_node
class HumanApprovalNode(BaseNode):
    node_type = NodeType.HUMAN_APPROVAL
    category = NodeCategory.LOGIC
    label = "Human Approval"
    description = "Pause workflow and wait for manual approval"
    icon = "👤"
    color = "#ec4899"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="message", label="Approval Message", type="textarea",
                            placeholder="Please review the processed records and approve to continue"),
            NodeFieldSchema(name="timeout_hours", label="Timeout (hours)", type="number", default=24,
                            help_text="Auto-reject after this many hours"),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        # The executor interprets this special status to pause
        return self._make_output(
            items=ctx.items,
            status="waiting_approval",
            message=ctx.config.get("message", "Approval required to continue"),
            timeout_hours=int(ctx.config.get("timeout_hours", 24)),
        )


@register_node
class DelayNode(BaseNode):
    node_type = NodeType.DELAY
    category = NodeCategory.LOGIC
    label = "Delay"
    description = "Wait before continuing"
    icon = "⏳"
    color = "#ec4899"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="seconds", label="Delay (seconds)", type="number", default=60),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        seconds = min(int(ctx.config.get("seconds", 60)), 3600)  # Cap at 1 hour
        await asyncio.sleep(seconds)
        return self._make_output(items=ctx.items, delayed_seconds=seconds)


@register_node
class ParallelSplitNode(BaseNode):
    node_type = NodeType.PARALLEL_SPLIT
    category = NodeCategory.LOGIC
    label = "Parallel Split"
    description = "Fan out data to multiple parallel branches"
    icon = "⑂"
    color = "#ec4899"

    @classmethod
    def get_schema(cls):
        schema = super().get_schema()
        schema.outputs = 3  # Up to 3 parallel branches
        return schema

    async def execute(self, ctx: NodeContext) -> Any:
        return self._make_output(items=ctx.items, split=True)


@register_node
class MergeWaitNode(BaseNode):
    node_type = NodeType.MERGE_WAIT
    category = NodeCategory.LOGIC
    label = "Merge / Wait All"
    description = "Wait for all parallel branches to complete and merge results"
    icon = "⑃"
    color = "#ec4899"

    async def execute(self, ctx: NodeContext) -> Any:
        # Input should be list of branch results (assembled by executor)
        if isinstance(ctx.input_data, list):
            merged_items = []
            for branch in ctx.input_data:
                if isinstance(branch, dict) and isinstance(branch.get("items"), list):
                    merged_items.extend(branch["items"])
                elif isinstance(branch, list):
                    merged_items.extend(branch)
                else:
                    merged_items.append(branch)
            return self._make_output(items=merged_items, branch_count=len(ctx.input_data))
        return self._make_output(items=ctx.items, branch_count=1)


@register_node
class SwitchRouterNode(BaseNode):
    node_type = NodeType.SWITCH_ROUTER
    category = NodeCategory.LOGIC
    label = "Switch / Router"
    description = "Route to one of multiple branches based on a field value (up to 6 outputs)"
    icon = "🔀"
    color = "#d946ef"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="field", label="Field to Match", type="text", required=True,
                placeholder="category",
                help_text="Field name from input data to match against. Supports dot notation.",
            ),
            NodeFieldSchema(
                name="routes", label="Routes (JSON Array)", type="json", required=True,
                default=[
                    {"label": "Route A", "value": "a"},
                    {"label": "Route B", "value": "b"},
                    {"label": "Default", "value": "__default__"},
                ],
                help_text='Array of {label, value}. Use "__default__" for the fallback route.',
            ),
        ]

    @classmethod
    def get_schema(cls):
        schema = super().get_schema()
        schema.outputs = 6  # Up to 6 outputs — UI should respect routes length
        schema.output_labels = ["Route 0", "Route 1", "Route 2", "Route 3", "Route 4", "Default"]
        return schema

    async def execute(self, ctx: NodeContext) -> Any:
        field_path = ctx.config.get("field", "")
        routes = ctx.config.get("routes", [])
        if isinstance(routes, str):
            import json as _json
            routes = _json.loads(routes)

        # Navigate nested field
        data = ctx.input_data
        parts = field_path.split(".")
        for part in parts:
            if isinstance(data, dict):
                data = data.get(part)
            elif isinstance(data, list) and part.isdigit():
                idx = int(part)
                data = data[idx] if idx < len(data) else None
            else:
                data = None
                break

        # Fallback: check ctx.variables when field not found in input_data
        if data is None and parts:
            top_key = parts[0]
            if top_key in ctx.variables:
                data = ctx.variables[top_key]
                for part in parts[1:]:
                    if isinstance(data, dict):
                        data = data.get(part)
                    else:
                        data = None
                        break

        # Match ConditionNode's behavior for the common envelope shape:
        # {"items": [{...}], "meta": {...}}. A bare field name such as
        # "region" should resolve against the first record.
        if data is None and parts and isinstance(ctx.input_data, dict):
            items = ctx.input_data.get("items")
            first_item = items[0] if isinstance(items, list) and items else None
            if isinstance(first_item, (dict, list)):
                data = first_item
                for part in parts:
                    if isinstance(data, dict):
                        data = data.get(part)
                    elif isinstance(data, list) and part.isdigit():
                        idx = int(part)
                        data = data[idx] if idx < len(data) else None
                    else:
                        data = None
                        break

        if data is None:
            logger.warning(
                "Switch/Router field '%s' did not resolve against input_data, "
                "items[0], or variables; no route value will be matched.",
                field_path,
            )

        field_value = str(data) if data is not None else ""

        # Find matching route
        matched_index = None
        default_index = None
        for i, route in enumerate(routes):
            if route.get("value") == "__default__":
                default_index = i
            elif str(route.get("value", "")) == field_value:
                matched_index = i
                break

        if matched_index is None:
            matched_index = default_index

        matched_label = routes[matched_index]["label"] if matched_index is not None else "none"

        return self._make_output(
            items=ctx.items,
            switch_result=True,
            matched_route=matched_index,
            matched_label=matched_label,
            field_value=field_value,
        )


@register_node
class SetVariableNode(BaseNode):
    node_type = NodeType.SET_VARIABLE
    category = NodeCategory.LOGIC
    label = "Set Variable"
    description = "Set or update workflow variables for downstream nodes"
    icon = "📝"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="assignments", label="Variable Assignments", type="variable_assignments",
                required=True,
                default=[{"name": "", "value": ""}],
                help_text='Define variables and their values. Values support {{variable}} placeholders.',
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        assignments = ctx.config.get("assignments", [])
        if not assignments:
            # Return the universal envelope on every path so callers
            # always read result["meta"]["variables_set"]. Used to return
            # a flat {"data", "variables_set"} dict on the empty path
            # which broke uniformity.
            return self._make_output(
                items=ctx.items,
                variables_set={},
                variables={},
                data=ctx.input_data,
            )

        updated = {}
        for entry in assignments:
            name = entry.get("name", "").strip()
            raw_value = entry.get("value", "")
            if not name:
                continue
            # Interpolate existing variables in the value
            if isinstance(raw_value, str):
                resolved = interpolate_variables(raw_value, ctx.variables)
            else:
                resolved = raw_value
            # Update the shared variables dict so downstream nodes see it
            ctx.variables[name] = resolved
            updated[name] = resolved

        logger.info(f"SetVariable: set {list(updated.keys())}")
        return self._make_output(
            items=ctx.items,
            variables_set=updated,
            variables=updated,
            data=ctx.input_data,
        )
