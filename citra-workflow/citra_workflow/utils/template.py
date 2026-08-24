# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Dotted-template interpolation for the smart-app + state nodes.

The base ``interpolate_variables`` in ``nodes/__init__.py`` matches
``{{name}}`` (single token only, no dots). For the SmartAppInvokerNode
and WorkflowState* nodes we need ``{{item.field}}`` and ``{{var.field}}``
dotted lookups so workflow authors can interpolate per-row fields and
workflow variables uniformly.

Usage::

    text = interpolate_dotted(
        "POST {{var.api_base}}/claims/{{item.claim_id}}",
        item={"claim_id": "C-001"},
        variables={"api_base": "https://api.example.com"},
    )
    # → "POST https://api.example.com/claims/C-001"

Resolution order for ``{{a.b}}``:
  1. If ``a == "item"``: read from ``item[b]``.
  2. If ``a == "var"``: read from ``variables[b]``.
  3. Otherwise: leave the placeholder as-is (no silent error).

Plain ``{{x}}`` (no dot) is treated as ``{{var.x}}`` for back-compat
with the existing single-token convention.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Whitespace inside the braces is optional. It has to be: every node field
# placeholder in this repo is written "{{ item.question }}" WITH spaces,
# and the original pattern only matched "{{item.question}}" — so the
# documented spelling silently never interpolated.
_DOTTED_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def interpolate_dotted(
    text: str,
    *,
    item: Optional[Dict[str, Any]] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> str:
    """Replace ``{{item.x}}`` / ``{{var.x}}`` / ``{{x}}`` placeholders.

    Unresolved placeholders are left untouched (so a downstream parser
    sees them and can fail with a clear error rather than silent
    substitution of an empty string).
    """
    if not text:
        return text
    item = item or {}
    variables = variables or {}

    def _lookup(key: str) -> Optional[Any]:
        if "." in key:
            ns, name = key.split(".", 1)
            if ns == "item":
                return item.get(name) if isinstance(item, dict) else None
            # `var`, `vars` and `variables` are synonyms. They have to be:
            # nodes/retrieval.py and nodes/mcp.py document `{{ vars.x }}` in
            # their own field help while this module documented `{{ var.x }}`,
            # and each silently resolved the other's spelling to nothing.
            # Accepting all three is the only version where a workflow author
            # cannot pick the wrong one.
            if ns in ("var", "vars", "variables"):
                return variables.get(name)
            # Unknown namespace — treat as missing.
            return None
        # Bare token: implicit var lookup (back-compat).
        return variables.get(key)

    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        value = _lookup(key)
        if value is None:
            return match.group(0)  # leave unresolved placeholders alone
        return str(value)

    return _DOTTED_PATTERN.sub(_replacer, text)
