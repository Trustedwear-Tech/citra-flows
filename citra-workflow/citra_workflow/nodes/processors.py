# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Processing nodes — AI, rules, transforms, validation."""

from __future__ import annotations
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..models import NodeType, NodeCategory, NodeFieldSchema
from . import BaseNode, NodeContext, register_node, interpolate_variables
from ..config import (
    MAX_LLM_PROMPT_SIZE, MAX_LLM_ITEM_SIZE,
    MAX_LLM_RULES_SIZE, MAX_LLM_SUMMARY_SIZE,
    MAX_PER_ITEM_FANOUT,
)

logger = logging.getLogger(__name__)

# Workflow LLM tiers — exposed in the UI so users can pick the right
# size/cost/quality trade-off per LLM-using node. The actual model name
# for each tier is resolved from environment via llm_tiers / llm_client.
_VALID_WORKFLOW_TIERS = {"small", "medium", "large"}
_DEFAULT_WORKFLOW_TIER = "large"

_TIER_HELP_TEXT = (
    "Pick the LLM size that fits this step (defaults to Large):\n"
    "• Small — fastest & cheapest. Best for simple classification, label/route "
    "decisions, field extraction, and lightweight transforms.\n"
    "• Medium — balanced quality and speed. Good for summarization, rules with "
    "reasoning, and structured extraction.\n"
    "• Large — highest quality and the DEFAULT. Use for complex reasoning, "
    "long-form content, agents using tools, and code-generation. Drop to a "
    "smaller tier only when the step is genuinely simpler."
)


def _tier_field() -> NodeFieldSchema:
    """Standard \"LLM Tier\" select field shared by every built-in LLM node.

    Custom LLM nodes (where the user supplies their own endpoint/model) do
    NOT use this field — they keep their explicit base_url + model_name.
    """
    return NodeFieldSchema(
        name="tier", label="LLM Tier", type="select",
        default=_DEFAULT_WORKFLOW_TIER,
        options=[
            {
                "label": "Small — fast & cheap",
                "value": "small",
                "description": "Best for simple classification, extraction, and lightweight transforms.",
            },
            {
                "label": "Medium — balanced",
                "value": "medium",
                "description": "Balanced quality and speed for moderately complex tasks.",
            },
            {
                "label": "Large — highest quality (default)",
                "value": "large",
                "description": "Default. For complex reasoning, agents with tools, and code generation.",
            },
        ],
        help_text=_TIER_HELP_TEXT,
    )


def _coerce_tier(value: Any) -> str:
    """Coerce a config value to a valid tier, defaulting to large
    (``_DEFAULT_WORKFLOW_TIER``) for missing/unknown values."""
    if isinstance(value, str) and value in _VALID_WORKFLOW_TIERS:
        return value
    return _DEFAULT_WORKFLOW_TIER


def _enforce_llm_size(data_str: str, limit: int, node_label: str) -> str:
    """Raise ValueError if serialized data exceeds the LLM prompt limit."""
    if len(data_str) > limit:
        raise ValueError(
            f"{node_label} input data ({len(data_str)} chars) exceeds LLM prompt limit ({limit}). "
            f"Add a filter/transform node upstream to reduce data size, "
            f"or set the corresponding WF_MAX_LLM_* environment variable to increase the limit."
        )
    return data_str


# ── Common processing-mode fields ────────────────────────────────────────
def _processing_mode_fields() -> List[NodeFieldSchema]:
    """Shared fields for processing_mode control on LLM-backed processors."""
    return [
        NodeFieldSchema(
            name="processing_mode", label="Processing Mode", type="select", default="all",
            options=[
                {"label": "All at Once", "value": "all"},
                {"label": "Each Item", "value": "each"},
                {"label": "Batch", "value": "batch"},
            ],
            help_text="'All' sends entire input. 'Each' processes items one-by-one. 'Batch' groups items.",
        ),
        NodeFieldSchema(
            name="batch_size", label="Batch Size", type="number", default=10,
            help_text="Number of items per batch (only used in Batch mode)",
            visible_when={"field": "processing_mode", "value": "batch"}),
    ]


async def _run_per_item(node: BaseNode, ctx: NodeContext, process_fn) -> Any:
    """Execute process_fn based on the node's processing_mode config.

    process_fn(items, ctx) -> result dict
    """
    mode = ctx.config.get("processing_mode", "all")
    items = node._extract_items(ctx.input_data)

    if mode == "all" or len(items) <= 1:
        return await process_fn(items, ctx)

    # Runaway-cost guard: `each` mode makes one LLM call per item, `batch`
    # one per batch. Fail loudly rather than silently firing thousands of
    # model calls when a large source is mis-wired into a per-item node.
    if len(items) > MAX_PER_ITEM_FANOUT:
        raise ValueError(
            f"Per-item processing received {len(items)} items in '{mode}' "
            f"mode, exceeding the fan-out cap of {MAX_PER_ITEM_FANOUT}. "
            f"Filter or aggregate upstream, switch to 'all' mode, or raise "
            f"WF_MAX_PER_ITEM_FANOUT if this volume is intentional."
        )

    import asyncio
    from ..config import PER_ITEM_CONCURRENCY

    def _flatten_into(acc, res):
        if isinstance(res, dict) and "items" in res:
            acc.extend(res["items"])
        else:
            acc.append(res)

    async def _run_groups(groups):
        """Run process_fn over each group with BOUNDED CONCURRENCY, preserving
        input order. Sequential per-record processing of a slow LLM easily
        blows the per-node timeout; running a few in parallel keeps total time
        close to the slowest call instead of their sum."""
        sem = asyncio.Semaphore(max(1, PER_ITEM_CONCURRENCY))

        async def _one(group):
            async with sem:
                return await process_fn(group, ctx)

        # gather preserves order, so results map back to the input ordering.
        return await asyncio.gather(*[_one(g) for g in groups])

    if mode == "each":
        results = await _run_groups([[item] for item in items])
        all_items = []
        for res in results:
            _flatten_into(all_items, res)
        return {"items": all_items, "meta": {"processing_mode": "each", "total": len(all_items)}}

    # batch mode
    batch_size = max(1, int(ctx.config.get("batch_size", 10)))
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    results = await _run_groups(batches)
    all_items = []
    for res in results:
        _flatten_into(all_items, res)
    return {"items": all_items, "meta": {"processing_mode": "batch",
            "batch_size": batch_size, "total_batches": len(batches)}}


import re as _re

def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from LLM responses."""
    return _re.sub(r'<think>.*?</think>', '', text, flags=_re.DOTALL).strip()


def _strip_code_fences(text: str) -> str:
    """Strip a leading ```json / ``` fence and the trailing ``` from an LLM
    response, returning the inner JSON payload.

    Robust against the two failure modes that previously crashed callers with
    ``IndexError: list index out of range``:
      1. A fence with NO newline after it, e.g. `````{...}````` — the old
         ``text.split("\\n", 1)[1]`` produced a 1-element list.
      2. (rules_engine only) a literal-backslash typo ``split("\\\\n")`` that
         never matched a real newline at all.

    If no fence is present, the input is returned stripped, unchanged.
    """
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    # Drop the opening fence + optional language tag.
    nl = s.find("\n")
    if nl == -1:
        # Single-line fence: ```{...}``` or ```json {...}```
        inner = s[3:]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        s = inner
    else:
        # Multi-line: drop everything up to and including the first newline
        # (the ``` or ```json marker line).
        s = s[nl + 1:]
    # Drop the trailing fence if present.
    if "```" in s:
        s = s.rsplit("```", 1)[0]
    return s.strip()


async def _charged_offload(fn, *args):
    """Charge ONE call against the run's per-run LLM-call budget (raises
    ``RunLlmBudgetExceeded`` when the cap is hit) then run the blocking LLM call
    in the default executor. The charge MUST happen here, in the async context —
    contextvars are not copied into run_in_executor threads, so a charge inside
    the offloaded function would read an empty budget and silently under-count.
    """
    import asyncio
    from ..llm_budget import charge_llm_call
    charge_llm_call()
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


def _get_llm_response(prompt: str, tier: str = _DEFAULT_WORKFLOW_TIER, system: str = "") -> str:
    """Call LLM using llm_oss with the user-selected tier (small/medium/large).

    The actual model name for the tier is resolved from environment by
    llm_oss / llm_client. Custom LLM nodes have their own execution path
    and do not go through this helper.
    """
    tier = _coerce_tier(tier)
    # Smaller tiers typically have tighter context budgets; cap accordingly.
    max_tokens = 4096 if tier == "small" else 16000

    from citra_llm.oss import llm_call
    return llm_call(
        system_prompt=system,
        user_prompt=prompt,
        model=None,
        temperature=0.2,
        max_tokens=max_tokens,
        tier=tier,
    )


@register_node
class LLMProcessorNode(BaseNode):
    node_type = NodeType.LLM_PROCESSOR
    category = NodeCategory.PROCESSOR
    label = "LLM Processor"
    description = "Send data to an LLM with a custom prompt"
    icon = "🤖"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="system_prompt", label="System Prompt", type="textarea",
                            placeholder="You are a data analyst..."),
            NodeFieldSchema(name="user_prompt", label="User Prompt Template", type="textarea", required=True,
                            placeholder="Evaluate this applicant: {{data}}\n\nReturn a JSON object with these fields:\n- qualified (boolean)\n- score (number 0-100)\n- reason (string)",
                            help_text="Use {{data}} to inject input data, {{item}} for current item in Each/Batch mode. "
                                      "Tip: Ask the LLM to return JSON and describe the fields you want — the output is auto-parsed and passed to the next node."),
            _tier_field(),
            NodeFieldSchema(name="merge_with_input", label="Merge Output with Input", type="boolean", default=False,
                            help_text="When enabled, LLM output fields are merged into the original input records (useful for enrichment workflows). "
                                      "Applies item-by-item when the item counts match."),
            *_processing_mode_fields(),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        merge_with_input = bool(ctx.config.get("merge_with_input", False))

        async def _process(items, ctx):
            import asyncio
            system_prompt = ctx.config.get("system_prompt", "")
            user_prompt_template = ctx.config["user_prompt"]
            tier = _coerce_tier(ctx.config.get("tier", _DEFAULT_WORKFLOW_TIER))

            data_str = _enforce_llm_size(json.dumps(items, default=str), MAX_LLM_PROMPT_SIZE, "LLM Processor")
            user_prompt = user_prompt_template.replace("{{data}}", data_str)
            # In each/batch mode, also support {{item}} placeholder
            if len(items) == 1:
                user_prompt = user_prompt.replace("{{item}}", _enforce_llm_size(json.dumps(items[0], default=str), MAX_LLM_ITEM_SIZE, "LLM Processor item"))

            user_prompt = interpolate_variables(user_prompt, ctx.variables)

            try:
                result = await _charged_offload(_get_llm_response, user_prompt, tier, system_prompt)
            except Exception as llm_err:
                # Fail loud: do NOT emit a fake "success" fallback. A silently-passed
                # fallback let the run report COMPLETED while bad output flowed to
                # downstream export/writer nodes. Raise a clear, user-facing error so
                # the node + run are marked FAILED and downstream nodes do not run.
                logger.error("LLM Processor (tier=%s): LLM call failed: %s", tier, llm_err)
                raise RuntimeError(
                    f"LLM Processor failed: the LLM call did not succeed, so this step "
                    f"produced no valid output (tier='{tier}'). The run was stopped here "
                    f"instead of passing placeholder data downstream. "
                    f"Check the LLM tier/model and provider availability. Details: {llm_err}"
                ) from llm_err

            # Auto-detect: try JSON first, fall back to plain text
            try:
                cleaned = _strip_code_fences(_strip_think_tags(result))
                parsed = json.loads(cleaned)
                items_out = parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, ValueError):
                items_out = [{"result": result}]

            # Merge with original input items when requested and counts match
            if merge_with_input and len(items_out) == len(items):
                merged = []
                for orig, llm_result in zip(items, items_out):
                    if isinstance(orig, dict) and isinstance(llm_result, dict):
                        merged.append({**orig, **llm_result})
                    else:
                        merged.append(llm_result)
                items_out = merged

            return {"items": items_out, "meta": {}}

        return await _run_per_item(self, ctx, _process)


# ---------------------------------------------------------------------------
# SSRF guard — shared by nodes that call user-supplied URLs
# ---------------------------------------------------------------------------
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata.google.com"}


def _validate_external_url(url: str) -> None:
    """Reject URLs pointing at private / internal addresses."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("A valid URL with a hostname is required")
    if hostname in _BLOCKED_HOSTS:
        raise ValueError("Requests to internal/private addresses are not allowed")
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
        for _family, _type, _proto, _canonname, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("Requests to internal/private addresses are not allowed")
    except socket.gaierror:
        raise ValueError(f"Could not resolve hostname: {hostname}")


@register_node
class CustomLLMProcessorNode(BaseNode):
    """Call any OpenAI-compatible LLM endpoint with a user-provided URL and optional API key."""

    node_type = NodeType.CUSTOM_LLM
    category = NodeCategory.PROCESSOR
    label = "Custom LLM"
    description = "Call your own OpenAI-compatible LLM endpoint"
    icon = "🔗"
    color = "#6d28d9"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="base_url", label="Endpoint URL", type="text", required=True,
                placeholder="https://my-model.example.com/v1",
                help_text="OpenAI-compatible base URL (must end with /v1 or similar). "
                          "Supports {{variable}} placeholders.",
            ),
            NodeFieldSchema(
                name="api_key", label="API Key", type="password",
                placeholder="sk-...",
                help_text="Optional — some self-hosted models don't require a key",
            ),
            NodeFieldSchema(
                name="model_name", label="Model Name", type="text",
                placeholder="my-model",
                help_text="Model identifier sent in the API request",
            ),
            NodeFieldSchema(
                name="system_prompt", label="System Prompt", type="textarea",
                placeholder="You are a data analyst...",
            ),
            NodeFieldSchema(
                name="user_prompt", label="User Prompt Template", type="textarea", required=True,
                placeholder="Evaluate this applicant: {{data}}\n\nReturn a JSON object with these fields:\n"
                            "- qualified (boolean)\n- score (number 0-100)\n- reason (string)",
                help_text="Use {{data}} to inject input data, {{item}} for current item in Each/Batch mode. "
                          "Tip: Ask the LLM to return JSON — the output is auto-parsed.",
            ),
            NodeFieldSchema(name="temperature", label="Temperature", type="number", default=0.2),
            NodeFieldSchema(name="max_tokens", label="Max Tokens", type="number", default=4096),
            *_processing_mode_fields(),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        async def _process(items, ctx):
            import asyncio

            base_url = ctx.config.get("base_url", "").strip()
            if not base_url:
                raise ValueError("Endpoint URL is required")
            base_url = interpolate_variables(base_url, ctx.variables)
            _validate_external_url(base_url)

            api_key = ctx.config.get("api_key") or "not-needed"
            model_name = ctx.config.get("model_name", "") or ""
            system_prompt = ctx.config.get("system_prompt", "")
            user_prompt_template = ctx.config["user_prompt"]
            temperature = float(ctx.config.get("temperature", 0.2))
            max_tokens = int(ctx.config.get("max_tokens", 4096))

            data_str = _enforce_llm_size(json.dumps(items, default=str), MAX_LLM_PROMPT_SIZE, "Custom LLM")
            user_prompt = user_prompt_template.replace("{{data}}", data_str)
            if len(items) == 1:
                user_prompt = user_prompt.replace(
                    "{{item}}",
                    _enforce_llm_size(json.dumps(items[0], default=str), MAX_LLM_ITEM_SIZE, "Custom LLM item"),
                )
            user_prompt = interpolate_variables(user_prompt, ctx.variables)

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})

            def _call_custom_llm():
                from openai import OpenAI
                client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content or ""

            try:
                result = await _charged_offload(_call_custom_llm)
            except Exception as llm_err:
                # Fail loud rather than emitting a fallback that masquerades as success.
                logger.error("Custom LLM: API call to %s failed: %s", base_url, llm_err)
                raise RuntimeError(
                    f"Custom LLM call failed against {base_url!r}: {llm_err}. "
                    f"The run was stopped here instead of passing placeholder data downstream."
                ) from llm_err

            # Auto-detect: try JSON first, fall back to plain text
            try:
                cleaned = _strip_code_fences(_strip_think_tags(result))
                parsed = json.loads(cleaned)
                items_out = parsed if isinstance(parsed, list) else [parsed]
                return {"items": items_out, "meta": {}}
            except (json.JSONDecodeError, ValueError):
                return {"items": [{"result": result}], "meta": {}}

        return await _run_per_item(self, ctx, _process)


# ── Helpers for deterministic rule evaluation ────────────────────────

def _to_number(val) -> float | None:
    """Try to extract a numeric value from various formats."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        import re as _re
        # Handle "74/100", "15 (currency not specified)", "Score: 70/100" etc.
        m = _re.search(r'(-?\d+(?:\.\d+)?)', val)
        if m:
            return float(m.group(1))
    if isinstance(val, dict):
        # Try common keys
        for k in ("score", "value", "amount", "total"):
            if k in val:
                return _to_number(val[k])
        # Any numeric value in the dict
        for v in val.values():
            n = _to_number(v)
            if n is not None:
                return n
    if isinstance(val, list) and val:
        # List of dicts: take first element and recurse
        return _to_number(val[0])
    return None


def _extract_score(item: dict) -> int:
    """Extract a numeric score from the item's evaluation fields."""
    # Try _score first (already set), then evaluation info, then score
    for key in ("_score", "score", "evaluation info", "evaluation_info"):
        if key in item:
            n = _to_number(item[key])
            if n is not None:
                return int(n)
    return 0


@register_node
class RulesEngineNode(BaseNode):
    node_type = NodeType.RULES_ENGINE
    category = NodeCategory.PROCESSOR
    label = "Text Rules Engine"
    description = "Evaluate records against plain-text business rules using LLM"
    icon = "📋"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="rules", label="Business Rules", type="textarea", required=True,
                            placeholder="1. Company must have >2 years history\n2. Revenue must exceed $1M\n3. Must be headquartered in the US",
                            help_text="Each record is evaluated against these rules. Output per record: {record_index, passed (bool), reason}."),
            _tier_field(),
            *_processing_mode_fields(),
        ]

    # ── Deterministic numeric rule parser ────────────────────────
    @staticmethod
    def _try_deterministic_eval(rules_text: str, items: list) -> list | None:
        """Attempt to parse simple numeric rules and evaluate deterministically.

        Supports rules like:
          - "salary < 12 AND score > 70"
          - "if salary < 12, evaluation score > 70"
          - "field_name operator value [AND/OR field_name operator value ...]"

        Returns a list of evaluation dicts, or None if the rules are too
        complex for deterministic evaluation (fall back to LLM).
        """
        import re as _re

        # Normalize common phrasing
        text = rules_text.strip().lower()
        # Remove common preambles
        for prefix in ("for each candidate:", "for each record:", "for each item:",
                       "each candidate must have:", "if "):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()

        # Split on AND / comma (treat comma as AND)
        conjuncts = _re.split(r'\band\b|,', text)
        conditions = []
        for conj in conjuncts:
            conj = conj.strip()
            if not conj:
                continue
            # Match patterns like: "salary < 12", "evaluation score > 70", "score >= 80"
            m = _re.match(
                r'(?:if\s+)?(.+?)\s*(<=|>=|!=|<>|<|>|==|=)\s*(-?\d+(?:\.\d+)?)\s*$',
                conj,
            )
            if not m:
                return None  # Can't parse → fall back to LLM
            field_phrase, op, threshold = m.group(1).strip(), m.group(2), float(m.group(3))
            # Normalize operator
            if op == "=":
                op = "=="
            if op == "<>":
                op = "!="
            conditions.append((field_phrase, op, threshold))

        if not conditions:
            return None

        def _extract_numeric(item, field_phrase):
            """Try to find a numeric value in the item matching the field phrase."""
            # Direct key match
            for key in item:
                if key.lower().replace("_", " ") == field_phrase:
                    return _to_number(item[key])

            # Partial key match (e.g. "evaluation score" matches "evaluation info")
            for key in item:
                key_norm = key.lower().replace("_", " ")
                phrase_norm = field_phrase
                # Check if the first word of the phrase matches the first word of the key
                phrase_words = phrase_norm.split()
                key_words = key_norm.split()
                if phrase_words and key_words and phrase_words[0] == key_words[0]:
                    val = item[key]
                    # Handle list-of-dicts: [{"score": "74/100", ...}]
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        # Try "score" key, or any numeric value in first dict
                        d = val[0]
                        if "score" in d:
                            n = _to_number(d["score"])
                            if n is not None:
                                return n
                        for v in d.values():
                            n = _to_number(v)
                            if n is not None:
                                return n
                    n = _to_number(val)
                    if n is not None:
                        return n

            # Direct substring containment (looser match)
            for key in item:
                key_stripped = key.lower().replace("_", "").replace(" ", "")
                phrase_stripped = field_phrase.replace(" ", "")
                if phrase_stripped in key_stripped or key_stripped in phrase_stripped:
                    val = item[key]
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        d = val[0]
                        if "score" in d:
                            n = _to_number(d["score"])
                            if n is not None:
                                return n
                    return _to_number(val)

            # Check nested dict for "score" in evaluation info etc.
            words = field_phrase.split()
            if len(words) >= 2:
                container_word = words[0]
                value_word = words[-1]
                for key in item:
                    if container_word in key.lower().replace("_", " "):
                        val = item[key]
                        if isinstance(val, dict) and value_word in val:
                            return _to_number(val[value_word])
                        # If container key matched and value is already numeric, use it directly
                        n = _to_number(val)
                        if n is not None:
                            return n

            # Last resort: check _score (enriched field)
            if "_score" in item:
                return _to_number(item["_score"])

            return None

        results = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                results.append({"record_index": i, "passed": False, "reason": "Record is not a dict"})
                continue
            all_pass = True
            reasons = []
            for field_phrase, op, threshold in conditions:
                val = _extract_numeric(item, field_phrase)
                if val is None:
                    all_pass = False
                    reasons.append(f"{field_phrase} could not be evaluated (field not found)")
                    continue
                if op == "<":
                    ok = val < threshold
                elif op == "<=":
                    ok = val <= threshold
                elif op == ">":
                    ok = val > threshold
                elif op == ">=":
                    ok = val >= threshold
                elif op == "==":
                    ok = val == threshold
                elif op == "!=":
                    ok = val != threshold
                else:
                    ok = False
                if ok:
                    reasons.append(f"{field_phrase} ({val}) meets requirement ({op} {threshold})")
                else:
                    all_pass = False
                    reasons.append(f"{field_phrase} ({val}) does not meet requirement ({op} {threshold})")
            results.append({
                "record_index": i,
                "passed": all_pass,
                "reason": "; ".join(reasons),
            })
        return results

    async def execute(self, ctx: NodeContext) -> Any:
        async def _process(items, ctx):
            import asyncio
            rules = ctx.config["rules"]
            tier = _coerce_tier(ctx.config.get("tier", _DEFAULT_WORKFLOW_TIER))

            # Try deterministic evaluation first
            det_results = RulesEngineNode._try_deterministic_eval(rules, items)
            if det_results is not None:
                evals_list = det_results
            else:
                # Fall back to LLM evaluation with improved prompt
                system = (
                    "You are a strict compliance evaluator. Given business rules and data records, "
                    "evaluate whether EACH record meets ALL the stated conditions. "
                    "Return a JSON array of objects with fields: record_index (int), passed (bool), reason (string). "
                    "IMPORTANT: 'passed' must be true ONLY when ALL rule conditions are satisfied for that record. "
                    "If ANY condition is not met, 'passed' must be false. "
                    "The 'reason' must clearly state which conditions were met and which were not."
                )
                prompt = f"RULES:\\n{rules}\\n\\nDATA:\\n{_enforce_llm_size(json.dumps(items, default=str), MAX_LLM_RULES_SIZE, 'Rules Engine')}\\n\\nEvaluate each record. Return JSON array."
                resp = await _charged_offload(_get_llm_response, prompt, tier, system)
                try:
                    cleaned = _strip_code_fences(_strip_think_tags(resp))
                    evals = json.loads(cleaned)
                    evals_list = evals if isinstance(evals, list) else [evals]
                except (json.JSONDecodeError, TypeError):
                    evals_list = [{"raw": resp}]

            # Merge evaluation fields into original input items (preserve pipeline data)
            merged = []
            for i, item in enumerate(items):
                enriched = dict(item) if isinstance(item, dict) else {}
                for ev in evals_list:
                    if ev.get("record_index") == i:
                        enriched["_rule_passed"] = ev.get("passed", False)
                        enriched["_rule_reason"] = ev.get("reason", "")
                        break
                else:
                    if i < len(evals_list) and isinstance(evals_list[i], dict):
                        enriched["_rule_passed"] = evals_list[i].get("passed", False)
                        enriched["_rule_reason"] = evals_list[i].get("reason", "")

                # Derive human-friendly status and compute score for ranking
                passed = enriched.get("_rule_passed", False)
                enriched["_status"] = "Selected" if passed else "Rejected"

                # Extract numeric score for ranking
                score = _extract_score(enriched)
                enriched["_score"] = score

                merged.append(enriched)

            # Rank: passed first, then by score desc, then by salary asc
            def _sort_key(rec):
                p = 0 if rec.get("_rule_passed") else 1
                s = -(rec.get("_score") or 0)
                sal = _to_number(rec.get("salary expectation", rec.get("salary", 9999))) or 9999
                return (p, s, sal)

            merged.sort(key=_sort_key)
            for rank, rec in enumerate(merged, 1):
                rec["_rank"] = rank

            return {"items": merged, "meta": {"total": len(items)}}

        result = await _run_per_item(self, ctx, _process)

        # ── Global re-ranking across ALL items (handles "each"/"batch" modes) ──
        # When processing_mode is "each" or "batch", _process runs on sub-groups
        # and each sub-group gets its own local rank. Re-rank globally here.
        if isinstance(result, dict) and "items" in result:
            all_items = result["items"]
            if len(all_items) > 1:
                def _global_sort_key(rec):
                    p = 0 if rec.get("_rule_passed") else 1
                    s = -(rec.get("_score") or 0)
                    sal = _to_number(rec.get("salary expectation", rec.get("salary", 9999))) or 9999
                    return (p, s, sal)
                all_items.sort(key=_global_sort_key)
                for rank, rec in enumerate(all_items, 1):
                    rec["_rank"] = rank

        return result


def _coerce_rename_map(params: Any) -> Dict[str, Any]:
    """Normalize Data Transform `rename` params into a direct {existing: new} map.

    Accepts the canonical direct map ``{"current_role": "role"}`` as-is, and ALSO
    the literal-template shape ``{"old_name": "current_role", "new_name": "role"}``
    (or ``{"from": ..., "to": ...}``, or a LIST of such objects) that the builder
    LLM sometimes emits by reading the help-text example literally. Converting it
    here turns what would be a silent no-op rename into the intended rename.
    """
    def _from_pair_obj(obj: Any) -> Optional[Dict[str, Any]]:
        if isinstance(obj, dict):
            keys = {str(k).lower() for k in obj.keys()}
            if keys == {"old_name", "new_name"}:
                old, new = obj.get("old_name"), obj.get("new_name")
            elif keys == {"from", "to"}:
                old, new = obj.get("from"), obj.get("to")
            else:
                return None
            if old is not None and new is not None:
                return {old: new}
        return None

    if isinstance(params, list):
        out: Dict[str, Any] = {}
        for item in params:
            conv = _from_pair_obj(item)
            if conv is not None:
                out.update(conv)
            elif isinstance(item, dict):
                out.update(item)  # already a direct {existing: new} map
        return out
    if isinstance(params, dict):
        conv = _from_pair_obj(params)
        return conv if conv is not None else params
    return {}


@register_node
class DataTransformNode(BaseNode):
    node_type = NodeType.DATA_TRANSFORM
    category = NodeCategory.PROCESSOR
    label = "Data Transform"
    description = "Filter, rename, aggregate data columns using pandas"
    icon = "🔄"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="operation", label="Operation", type="select", required=True,
                            options=[
                                {"label": "Filter Rows", "value": "filter"},
                                {"label": "Select Columns", "value": "select"},
                                {"label": "Rename Columns", "value": "rename"},
                                {"label": "Sort", "value": "sort"},
                                {"label": "Group & Aggregate", "value": "aggregate"},
                                {"label": "Add Column (Expression)", "value": "add_column"},
                            ]),
            NodeFieldSchema(name="params", label="Parameters (JSON)", type="json", required=True,
                            help_text='This MUST be a real JSON object (NOT a quoted/stringified JSON).\n'
                                      'Filter: {"column": "status", "operator": "==", "value": "active"}\n'
                                      'Select: {"columns": ["name","email"]}\n'
                                      'Rename — a direct map of {"<existing_column>": "<new_column>"}, '
                                      'e.g. {"current_role": "role", "_classification_label": "classification"}. '
                                      'Do NOT use {"old_name": ..., "new_name": ...} — the keys are the EXISTING '
                                      'column names and the values are the NEW names.\n'
                                      'Sort: {"column": "date", "ascending": false}\n'
                                      'Aggregate: {"group_by": "dept", "agg": {"salary": "mean"}}\n'
                                      'Add Column: {"name": "total", "expression": "price * quantity"}'),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import pandas as pd
        import asyncio

        records = ctx.items
        if not records:
            return self._make_output(items=[], count=0)

        op = ctx.config["operation"]
        params = ctx.config.get("params", {}) or {}
        # Defensive coercion: the builder LLM (and hand-authored configs) sometimes
        # emit a `json`-type field as a STRINGIFIED JSON instead of a real object,
        # e.g. params = '{"column": "score", "ascending": false}'. Without this,
        # `params["column"]` raises the cryptic "string indices must be integers,
        # not 'str'" at runtime. Parse it here (mirrors ValidatorNode's `rules`
        # handling) or fail early with a clear, actionable config-type error.
        if isinstance(params, str):
            text = params.strip()
            if not text:
                params = {}
            else:
                try:
                    params = json.loads(text)
                except (ValueError, TypeError):
                    raise ValueError(
                        "Data Transform 'params' must be a JSON object, not a "
                        f"string. Got an unparseable string: {params!r}. Set "
                        "params to a real object, e.g. {\"column\": \"score\", "
                        "\"ascending\": false}."
                    )
        # `rename` additionally accepts a LIST of {old_name,new_name} objects
        # (normalized by _coerce_rename_map); every other operation needs a dict.
        if not isinstance(params, dict) and not (op == "rename" and isinstance(params, list)):
            raise ValueError(
                "Data Transform 'params' must be a JSON object (field→value), but "
                f"is a {type(params).__name__}. Set it to an object like "
                "{\"column\": \"score\", \"ascending\": false}."
            )

        # An operation outside this map matches no branch below and returns the
        # rows untouched — a typo'd operation silently becomes a no-op, which is
        # the worst outcome for a data step. Required keys are checked here too:
        # `params["column"]` on a missing key raises a bare KeyError whose entire
        # message is the key name, so the shipped "Data Processing Pipeline"
        # template (params: {}) reported itself to the user as just 'column'.
        _REQUIRED_PARAMS = {
            "filter": (("column", "value"), '{"column": "status", "operator": "==", "value": "active"}'),
            "select": (("columns",), '{"columns": ["name", "email"]}'),
            "rename": ((), '{"<existing_column>": "<new_column>"}'),
            "sort": (("column",), '{"column": "score", "ascending": false}'),
            "aggregate": (("group_by", "agg"), '{"group_by": "region", "agg": {"amount": "sum"}}'),
            "add_column": (("name", "expression"), '{"name": "total", "expression": "price * qty"}'),
        }
        if op not in _REQUIRED_PARAMS:
            raise ValueError(
                f"Data Transform: unknown operation {op!r}. Supported operations "
                f"are: {', '.join(sorted(_REQUIRED_PARAMS))}."
            )
        if isinstance(params, dict):
            _required, _example = _REQUIRED_PARAMS[op]
            _missing = [k for k in _required if k not in params]
            if _missing:
                raise ValueError(
                    f"Data Transform operation '{op}' is missing required "
                    f"param(s): {', '.join(_missing)}. Set the node's "
                    f"Parameters (JSON) field, e.g. {_example}"
                )

        def _transform():
            df = pd.DataFrame(records)
            if op == "filter":
                col, operator, val = params["column"], params.get("operator", "=="), params["value"]
                if operator == "==":
                    df = df[df[col] == val]
                elif operator == "!=":
                    df = df[df[col] != val]
                elif operator == ">":
                    df = df[df[col] > val]
                elif operator == "<":
                    df = df[df[col] < val]
                elif operator == "contains":
                    df = df[df[col].astype(str).str.contains(str(val), case=False, na=False)]
            elif op == "select":
                df = df[params["columns"]]
            elif op == "rename":
                # Expected format is a DIRECT column map {"existing": "new"}.
                # The builder LLM sometimes emits the {"old_name": X, "new_name": Y}
                # shape (reading the help-text template literally), which would
                # rename a column literally named "old_name" → a silent no-op.
                # Accept that shape (single object OR a list of them) and convert
                # it to the direct map so the rename actually happens.
                rename_map = _coerce_rename_map(params)
                df = df.rename(columns=rename_map)
            elif op == "sort":
                sort_col = params["column"]
                if sort_col not in df.columns:
                    raise ValueError(f"Sort column '{sort_col}' not found. Available: {list(df.columns)}")
                df = df.sort_values(by=sort_col, ascending=params.get("ascending", True))
            elif op == "aggregate":
                df = df.groupby(params["group_by"]).agg(params["agg"]).reset_index()
            elif op == "add_column":
                # Validate expression with AST to prevent code injection
                import ast
                expr = params["expression"]
                try:
                    tree = ast.parse(expr, mode="eval")
                except SyntaxError:
                    raise ValueError(f"Invalid expression: {expr}")
                # Only allow safe AST nodes (names, numbers, strings, binary/unary ops, comparisons)
                SAFE_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp,
                              ast.Name, ast.Constant, ast.Load, ast.Add, ast.Sub, ast.Mult,
                              ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
                              ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                              ast.And, ast.Or, ast.Not, ast.IfExp, ast.Attribute)
                for node in ast.walk(tree):
                    if not isinstance(node, SAFE_NODES):
                        raise ValueError(f"Unsafe operation in expression: {type(node).__name__}")
                df[params["name"]] = df.eval(expr)
            return df.to_dict(orient="records")

        result = await asyncio.get_event_loop().run_in_executor(None, _transform)
        return self._make_output(items=result, count=len(result))


@register_node
class ClassifierNode(BaseNode):
    node_type = NodeType.CLASSIFIER
    category = NodeCategory.PROCESSOR
    label = "Classifier"
    description = "Use LLM to classify each record into labels"
    icon = "🏷️"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="labels", label="Classification Labels", type="textarea", required=True,
                            placeholder="approved, rejected, needs_review",
                            help_text="Comma-separated list of possible labels. Output per record: {record_index, label, confidence}."),
            NodeFieldSchema(name="instructions", label="Classification Instructions", type="textarea",
                            placeholder="Classify each application based on eligibility criteria",
                            help_text="Optional extra instructions to guide classification."),
            _tier_field(),
            *_processing_mode_fields(),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        async def _process(items, ctx):
            import asyncio
            raw_labels = ctx.config["labels"]
            labels = raw_labels if isinstance(raw_labels, list) else [l.strip() for l in raw_labels.split(",")]
            instructions = ctx.config.get("instructions", "")
            tier = _coerce_tier(ctx.config.get("tier", _DEFAULT_WORKFLOW_TIER))

            system = (
                f"You are a data classifier. Classify each record into one of: {labels}. "
                f"Additional instructions: {instructions}\n"
                "Return a JSON array of objects with: record_index (int), label (string), confidence (float 0-1)."
            )
            prompt = f"DATA:\n{_enforce_llm_size(json.dumps(items, default=str), MAX_LLM_RULES_SIZE, 'Classifier')}\n\nClassify each record."
            resp = await _charged_offload(_get_llm_response, prompt, tier, system)
            try:
                cleaned = _strip_code_fences(_strip_think_tags(resp))
                classifications = json.loads(cleaned)
            except (json.JSONDecodeError, TypeError):
                classifications = [{"raw": resp}]
            # Merge classification fields into original input items (preserve pipeline data)
            merged = []
            for i, item in enumerate(items):
                enriched = dict(item) if isinstance(item, dict) else {}
                for cls_item in (classifications if isinstance(classifications, list) else []):
                    if cls_item.get("record_index") == i:
                        enriched["_classification_label"] = cls_item.get("label", "")
                        enriched["_classification_confidence"] = cls_item.get("confidence", 0.0)
                        break
                else:
                    # No matching classification found — assign first if only one
                    if i < len(classifications) and isinstance(classifications[i], dict):
                        enriched["_classification_label"] = classifications[i].get("label", "")
                        enriched["_classification_confidence"] = classifications[i].get("confidence", 0.0)
                merged.append(enriched)
            return {"items": merged, "meta": {"labels": labels, "total": len(items)}}

        return await _run_per_item(self, ctx, _process)


# ── Post-processing normalizer for ExtractorNode ────────────────────

def _normalize_extracted_fields(items: list, fields: list) -> list:
    """Ensure consistent types across all items for each extracted field.

    Strategy per field:
    - If majority type is list → coerce strings to single-element lists
    - If majority type is dict → wrap strings in a dict with a 'value' key
    - If a numeric field name suggests a score/salary → coerce to int/float
    - Otherwise keep as-is
    """
    import re as _re

    if not items or not isinstance(items[0], dict):
        return items

    # Fields that should be numeric
    _NUMERIC_HINTS = {"score", "salary", "rating", "revenue", "age", "experience_years",
                      "years", "amount", "price", "cost", "evaluation"}
    # Fields that should be lists
    _LIST_HINTS = {"skills", "experiences", "educations", "qualifications", "languages",
                   "certifications", "projects", "achievements", "responsibilities"}

    for field in fields:
        field_lower = field.lower().replace(" ", "_")
        vals = [item.get(field) for item in items if field in item]
        if not vals:
            continue

        # Determine target type from hints or majority
        if any(h in field_lower for h in _NUMERIC_HINTS):
            # Coerce to numeric
            for item in items:
                if field in item:
                    n = _to_number(item[field])
                    if n is not None:
                        item[field] = int(n) if n == int(n) else n
        elif any(h in field_lower for h in _LIST_HINTS):
            # Coerce to list
            for item in items:
                if field in item:
                    v = item[field]
                    if isinstance(v, str):
                        # Split comma/semicolon-separated strings into lists
                        parts = _re.split(r'[,;]\s*', v)
                        item[field] = [p.strip() for p in parts if p.strip()]
                    elif isinstance(v, dict):
                        item[field] = [v]
                    elif not isinstance(v, list):
                        item[field] = [v]
        else:
            # Use majority type normalization
            type_counts = {}
            for v in vals:
                t = type(v).__name__
                type_counts[t] = type_counts.get(t, 0) + 1
            majority_type = max(type_counts, key=type_counts.get) if type_counts else "str"

            if majority_type == "list":
                for item in items:
                    if field in item and not isinstance(item[field], list):
                        item[field] = [item[field]] if item[field] else []
            elif majority_type == "dict":
                for item in items:
                    if field in item and not isinstance(item[field], dict):
                        item[field] = {"value": item[field]}
            else:
                # String fields: clean semicolon artifacts injected by LLM
                # e.g. "Proficient in Java.; Strong team player" → "Proficient in Java. Strong team player"
                for item in items:
                    if field in item and isinstance(item[field], str):
                        import re as _re
                        # Remove .; → . and standalone ; between sentences → .
                        cleaned = _re.sub(r'\.\s*;\s*', '. ', item[field])
                        cleaned = _re.sub(r';\s*([A-Z])', r'. \1', cleaned)
                        item[field] = cleaned.strip()

    return items


@register_node
class ExtractorNode(BaseNode):
    node_type = NodeType.EXTRACTOR
    category = NodeCategory.PROCESSOR
    label = "Field Extractor"
    description = "Extract structured fields from unstructured text using LLM"
    icon = "🔎"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="fields_to_extract", label="Fields to Extract", type="textarea", required=True,
                            placeholder="company_name, revenue, founding_year, headquarters",
                            help_text="Comma-separated field names to extract. Output per record: an object with these fields."),
            _tier_field(),
            *_processing_mode_fields(),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        async def _process(items, ctx):
            import asyncio
            raw_fields = ctx.config["fields_to_extract"]
            fields = raw_fields if isinstance(raw_fields, list) else [f.strip() for f in raw_fields.split(",")]
            tier = _coerce_tier(ctx.config.get("tier", _DEFAULT_WORKFLOW_TIER))

            system = (
                f"Extract these fields from the data: {fields}. "
                "Return a JSON array of objects, one per record, with the extracted fields."
            )
            prompt = f"DATA:\n{_enforce_llm_size(json.dumps(items, default=str), MAX_LLM_RULES_SIZE, 'Field Extractor')}"
            resp = await _charged_offload(_get_llm_response, prompt, tier, system)
            try:
                cleaned = _strip_code_fences(_strip_think_tags(resp))
                extracted = json.loads(cleaned)
                items_out = extracted if isinstance(extracted, list) else [extracted]

                # Post-process: normalize field types across all items
                # so that the same field isn't a string in one record and a list in another.
                items_out = _normalize_extracted_fields(items_out, fields)

                return {"items": items_out, "meta": {"fields": fields}}
            except (json.JSONDecodeError, TypeError):
                return {"items": [{"raw": resp}], "meta": {"fields": fields}}

        return await _run_per_item(self, ctx, _process)


@register_node
class SummarizerNode(BaseNode):
    node_type = NodeType.SUMMARIZER
    category = NodeCategory.PROCESSOR
    label = "Summarizer"
    description = "Summarize a batch of data using LLM"
    icon = "📝"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="instructions", label="Summary Instructions", type="textarea",
                            placeholder="Summarize the key findings, risks, and recommendations",
                            help_text="Output: a single object with a summary field. The next node receives this text."),
            NodeFieldSchema(name="max_length", label="Max Length (words)", type="number", default=500),
            _tier_field(),
            *_processing_mode_fields(),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        async def _process(items, ctx):
            import asyncio
            instructions = ctx.config.get("instructions", "Provide a comprehensive summary")
            max_length = int(ctx.config.get("max_length", 500))
            tier = _coerce_tier(ctx.config.get("tier", _DEFAULT_WORKFLOW_TIER))

            system = f"Summarize the data in {max_length} words or fewer. {instructions}"
            prompt = f"DATA:\n{_enforce_llm_size(json.dumps(items, default=str), MAX_LLM_SUMMARY_SIZE, 'Summarizer')}"
            resp = await _charged_offload(_get_llm_response, prompt, tier, system)
            # Strip think tags and markdown code fences if present
            cleaned = _strip_code_fences(_strip_think_tags(resp))
            # Try to return parsed JSON if the summary is a JSON object
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return {"items": [parsed], "meta": {}}
                if isinstance(parsed, list):
                    return {"items": parsed, "meta": {}}
            except (json.JSONDecodeError, ValueError):
                pass
            return {"items": [{"summary": cleaned}], "meta": {}}

        return await _run_per_item(self, ctx, _process)


@register_node
class ValidatorNode(BaseNode):
    node_type = NodeType.VALIDATOR
    category = NodeCategory.PROCESSOR
    label = "Validator"
    description = "Validate records against schema rules"
    icon = "✅"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="rules", label="Validation Rules (JSON)", type="json", required=True,
                            help_text=(
                                'Rules map a field name to its constraints. '
                                'Supported constraints: "required" (bool), "pattern" (regex string), '
                                '"min"/"max" (number). '
                                'Example: {"email": {"required": true, "pattern": ".*@.*"}, "age": {"min": 18, "max": 120}}. '
                                'Input shape: the upstream node\'s records — either the standard envelope '
                                '{"items": [ {…}, {…} ]}, a "records" list, a bare list, or a single flat record '
                                'object. Each rule\'s field is read directly off each record (so a record '
                                'like {"id": "R-001"} satisfies a {"id": {"required": true}} rule).'
                            )),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import re
        rules = ctx.config["rules"]
        if isinstance(rules, str):
            import json as _json
            try:
                rules = _json.loads(rules)
            except (ValueError, TypeError):
                raise ValueError("Validation rules must be valid JSON")
        # Normalize input into a flat list of records. _extract_items handles
        # the standard {"items": [...]} envelope, a legacy "records" list, a
        # bare list, AND a single flat record dict (e.g. {"id": "R-001", ...}).
        # Using ctx.items alone missed every shape except "items", causing the
        # whole envelope dict to be validated as one record → spurious
        # "<field> is required" errors even when the inner records had the field.
        records = self._extract_items(ctx.input_data)

        valid, invalid = [], []
        for i, rec in enumerate(records):
            # Records must be objects (dicts) — the rules address fields by name.
            # A non-dict record (e.g. a bare scalar in the input list) is invalid
            # input; flag it clearly instead of crashing on rec.get(...).
            if not isinstance(rec, dict):
                invalid.append({
                    "record_index": i,
                    "errors": [f"record is not an object (got {type(rec).__name__})"],
                    "record": rec,
                })
                continue
            errors = []
            for field_name, field_rules in rules.items():
                # Each field's rules MUST be a constraints object. Tolerate the
                # common shorthand `{"field": true}` (= required) and a string
                # "required", but fail with a clear, actionable message on any
                # other scalar instead of crashing on `int.get(...)` — which is
                # what produced the cryptic "'int' object has no attribute 'get'".
                if isinstance(field_rules, bool):
                    field_rules = {"required": field_rules}
                elif not isinstance(field_rules, dict):
                    if isinstance(field_rules, str) and field_rules.strip().lower() in ("required", "true"):
                        field_rules = {"required": True}
                    else:
                        raise ValueError(
                            f"Validation rule for '{field_name}' must be an object like "
                            f'{{"required": true, "pattern": ".*@.*"}}; got '
                            f"{type(field_rules).__name__} ({field_rules!r}). "
                            "Fix the Validator rules so each field maps to a constraints object."
                        )
                val = rec.get(field_name)
                if field_rules.get("required") and (val is None or val == ""):
                    errors.append(f"{field_name} is required")
                    continue
                if val is not None:
                    # min/max only apply to numeric values — comparing a string
                    # (e.g. "11 LPA") against a number would raise TypeError.
                    _num = val if isinstance(val, (int, float)) and not isinstance(val, bool) else None
                    if "min" in field_rules and _num is not None and _num < field_rules["min"]:
                        errors.append(f"{field_name} below minimum ({field_rules['min']})")
                    if "max" in field_rules and _num is not None and _num > field_rules["max"]:
                        errors.append(f"{field_name} above maximum ({field_rules['max']})")
                    if "pattern" in field_rules and not re.match(field_rules["pattern"], str(val)):
                        errors.append(f"{field_name} doesn't match pattern")
            if errors:
                invalid.append({"record_index": i, "errors": errors, "record": rec})
            else:
                valid.append(rec)

        return self._make_output(
            items=valid,
            invalid_records=invalid,
            valid_count=len(valid),
            invalid_count=len(invalid),
        )


@register_node
class DeduplicatorNode(BaseNode):
    node_type = NodeType.DEDUPLICATOR
    category = NodeCategory.PROCESSOR
    label = "Deduplicator"
    description = "Remove duplicate records by key fields"
    icon = "🧹"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="key_fields", label="Key Fields", type="text", required=True,
                            placeholder="email,name", help_text="Comma-separated fields to check for duplicates"),
            NodeFieldSchema(name="keep", label="Keep", type="select", default="first",
                            options=[{"label": "First", "value": "first"}, {"label": "Last", "value": "last"}]),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        # Pure-Python dedup by key — no pandas dependency, so it runs in every
        # runtime (including the lean assistant self-test container) and handles
        # records whose values are lists/dicts (e.g. a `skills` array) that a
        # DataFrame would choke on.
        records = ctx.items or []
        raw_keys = ctx.config["key_fields"]
        if isinstance(raw_keys, list):
            keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        else:
            keys = [k.strip() for k in str(raw_keys).split(",") if k.strip()]
        keep = (ctx.config.get("keep") or "first").lower()

        def _key_for(rec):
            # Stable, hashable signature from the key fields present on this record.
            present = [k for k in keys if isinstance(rec, dict) and k in rec]
            if not present:
                return None  # no key fields → never treated as a duplicate
            return tuple(json.dumps(rec.get(k), sort_keys=True, default=str) for k in present)

        before = len(records)
        order = []          # output records in original order
        index_by_key = {}   # signature -> position in `order`
        for rec in records:
            sig = _key_for(rec)
            if sig is None or sig not in index_by_key:
                if sig is not None:
                    index_by_key[sig] = len(order)
                order.append(rec)
            elif keep == "last":
                order[index_by_key[sig]] = rec  # later record wins its slot
            # keep == "first": ignore the duplicate
        return self._make_output(items=order, count=len(order), removed=before - len(order))


@register_node
class MergeDataNode(BaseNode):
    node_type = NodeType.MERGE_DATA
    category = NodeCategory.PROCESSOR
    label = "Merge / Join"
    description = "Combine data from two input branches"
    icon = "🔀"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="join_type", label="Join Type", type="select", default="inner",
                            options=[{"label": "Inner", "value": "inner"}, {"label": "Left", "value": "left"},
                                     {"label": "Right", "value": "right"}, {"label": "Outer", "value": "outer"}]),
            NodeFieldSchema(name="join_key", label="Join Key", type="text", required=True,
                            placeholder="id", help_text="Column name to join on"),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import pandas as pd
        # Input should be two datasets (from parallel branches)
        data = ctx.input_data
        if isinstance(data, list) and len(data) >= 2:
            left = data[0].get("items", []) if isinstance(data[0], dict) else data[0]
            right = data[1].get("items", []) if isinstance(data[1], dict) else data[1]
        elif isinstance(data, dict) and "left" in data and "right" in data:
            left = data["left"].get("items", data["left"]) if isinstance(data["left"], dict) else data["left"]
            right = data["right"].get("items", data["right"]) if isinstance(data["right"], dict) else data["right"]
        else:
            return self._make_output(items=ctx.items, count=0)

        join_type = ctx.config.get("join_type", "inner")
        join_key = ctx.config["join_key"]

        # Handle empty inputs gracefully instead of crashing on KeyError
        if not left and not right:
            return self._make_output(items=[], count=0)
        if not left:
            result = right if join_type in ("right", "outer") else []
            return self._make_output(items=result, count=len(result))
        if not right:
            result = left if join_type in ("left", "outer") else []
            return self._make_output(items=result, count=len(result))

        df = pd.merge(pd.DataFrame(left), pd.DataFrame(right), on=join_key, how=join_type)
        result = df.to_dict(orient="records")
        return self._make_output(items=result, count=len(result))


@register_node
class CodeBlockNode(BaseNode):
    node_type = NodeType.CODE_BLOCK
    category = NodeCategory.PROCESSOR
    label = "Python Code"
    description = "Run custom Python code in an isolated Docker sandbox"
    icon = "🐍"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="code", label="Python Code", type="textarea", required=True,
                            placeholder="# Access input items via data.get('items', [])\n_items = data.get('items', [])\nresult = [r for r in _items if r.get('score', 0) > 80]",
                            help_text="Runs in an isolated Docker sandbox (no network). Use `data.get('items', [])` for input and `variables` for workflow variables; assign the output to `result`. Imports are allowed."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import ast
        from ..config import CODE_SANDBOX_IMAGE, CODE_SANDBOX_TIMEOUT
        from ..utils.code_sandbox import run_in_sandbox, RESULT_MARKER

        code = ctx.config["code"]

        # Fast syntax feedback before spinning up a container.
        try:
            ast.parse(code, mode="exec")
        except SyntaxError as e:
            raise ValueError(f"Syntax error in code: {e}")

        # Wrap the author code so the existing node contract still holds:
        # `data` (input), `variables`, and an assignable `result`. The
        # wrapper loads the input payload the worker stages into the
        # container, then prints `result` on a marked stdout line.
        # Execution is ISOLATED — author code never runs in-process.
        wrapped = (
            "import json as _json\n"
            "with open('/workspace/input/data.json') as _f:\n"
            "    _payload = _json.load(_f)\n"
            "data = _payload.get('data')\n"
            "variables = _payload.get('variables') or {}\n"
            "result = None\n"
            "# --- user code ---\n"
            + code + "\n"
            "# --- end user code ---\n"
            "print('" + RESULT_MARKER + "' + _json.dumps({'result': result}, default=str))\n"
        )

        sandbox = await run_in_sandbox(
            script=wrapped,
            input_payload={"data": ctx.input_data, "variables": ctx.variables or {}},
            image=CODE_SANDBOX_IMAGE,
            timeout=CODE_SANDBOX_TIMEOUT,
        )
        if not sandbox["success"]:
            detail = (sandbox.get("stderr") or sandbox.get("stdout") or "unknown error").strip()
            raise ValueError(f"Code execution failed: {detail[:2000]}")

        # Pull the marked result line out of stdout.
        result = None
        for line in sandbox["stdout"].splitlines():
            if line.startswith(RESULT_MARKER):
                try:
                    result = json.loads(line[len(RESULT_MARKER):]).get("result")
                except (ValueError, json.JSONDecodeError):
                    result = None
                break

        if isinstance(result, dict):
            items = [result]
        elif isinstance(result, list):
            items = result
        else:
            items = [{"result": result}]
        return self._make_output(items=items)


def _scrub_pii(value: Any, patterns: Dict[str, str]) -> tuple:
    """Scrub PII inline in string values. Returns (cleaned_value, hits)."""
    import re as _re
    if not isinstance(value, str) or not value:
        return value, 0
    hits = 0
    out = value
    for pat, repl in patterns.items():
        new, n = _re.subn(pat, repl, out)
        if n:
            hits += n
            out = new
    return out, hits


def _scrub_pii_in_dict(d: Dict[str, Any], patterns: Dict[str, str]) -> tuple:
    """Recursively scrub PII inside a dict. Returns (clean_dict, total_hits)."""
    if not isinstance(d, dict):
        return d, 0
    total = 0
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            cleaned, hits = _scrub_pii_in_dict(v, patterns)
            out[k] = cleaned
            total += hits
        elif isinstance(v, list):
            cleaned_list = []
            for item in v:
                if isinstance(item, dict):
                    sub, hits = _scrub_pii_in_dict(item, patterns)
                    cleaned_list.append(sub)
                    total += hits
                else:
                    new_v, hits = _scrub_pii(item, patterns)
                    cleaned_list.append(new_v)
                    total += hits
            out[k] = cleaned_list
        else:
            new_v, hits = _scrub_pii(v, patterns)
            out[k] = new_v
            total += hits
    return out, total


def _project(d: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    """Pick a subset of keys from a dict; preserves nested dict shape if dotted path."""
    if not keys:
        return dict(d)
    out: Dict[str, Any] = {}
    for k in keys:
        if "." in k:
            parts = k.split(".")
            cur: Any = d
            for p in parts:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    cur = None
                    break
            if cur is not None:
                out[parts[-1]] = cur
        elif k in d:
            out[k] = d[k]
    return out


def _serialize_input_for_embed(input_obj: Dict[str, Any]) -> str:
    """Stable JSON serialization of an input dict for embedding.

    Sorted keys so semantically-identical inputs always produce identical
    embeddings.
    """
    try:
        return json.dumps(input_obj, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(input_obj)


    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _diversity_fill(items: List[Dict[str, Any]], chosen: set, candidates: List[int], slots: int) -> set:
    """Greedy max-min cosine distance fill. Picks the candidate most unlike everything already picked."""
    chosen = set(chosen)
    pool = [i for i in candidates if i not in chosen]
    if not chosen and pool:
        chosen.add(pool.pop(0))
        slots -= 1
    while slots > 0 and pool:
        best_i = None
        best_score = float("inf")
        for i in pool:
            v = items[i].get("embedding") or []
            max_sim = 0.0
            for c in chosen:
                cv = items[c].get("embedding") or []
                sim = _cosine(v, cv)
                if sim > max_sim:
                    max_sim = sim
            if max_sim < best_score:
                best_score = max_sim
                best_i = i
        if best_i is None:
            break
        chosen.add(best_i)
        pool = [p for p in pool if p != best_i]
        slots -= 1
    return chosen


def _round_robin_fill(by_class: Dict[str, List[int]], chosen: set, slots: int) -> set:
    """Round-robin across decision classes when no embeddings are available."""
    chosen = set(chosen)
    queues = {cls: [i for i in idxs if i not in chosen] for cls, idxs in by_class.items()}
    classes = list(queues.keys())
    if not classes:
        return chosen
    pos = 0
    while slots > 0:
        progressed = False
        for _ in range(len(classes)):
            cls = classes[pos % len(classes)]
            pos += 1
            if queues[cls]:
                chosen.add(queues[cls].pop(0))
                slots -= 1
                progressed = True
                if slots <= 0:
                    break
        if not progressed:
            break
    return chosen
