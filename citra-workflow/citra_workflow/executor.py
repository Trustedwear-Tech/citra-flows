"""
Workflow Executor
=================
Topologically executes a workflow's node graph using LangGraph StateGraph.
Tracks progress in Redis and stores results in MongoDB.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, TypedDict

from .models import (
    ExecutionStatus, NodeExecutionStatus, NodeType,
    WorkflowDefinition, WorkflowExecution, NodeExecutionResult,
    WorkflowNotifications,
)
from .nodes import get_node, NodeContext
from .config import (
    EXECUTION_TIMEOUT_SECONDS, NODE_TIMEOUT_SECONDS,
    MAX_NODE_STEPS, MAX_CONCURRENT_EXECUTIONS, CONCURRENCY_TTL,
    PROGRESS_CACHE_TTL, DEFAULT_APPROVAL_TIMEOUT_HOURS,
    MAX_RUN_LLM_CALLS, MAX_RUN_LLM_CALLS_HARD,
    CHECKPOINT_INTERVAL_SECONDS,
)
from .llm_budget import start_run_budget

logger = logging.getLogger(__name__)


def _bson_safe(value: Any) -> Any:
    """Convert execution payloads to values MongoDB can store reliably."""
    if isinstance(value, dict):
        return {str(k): _bson_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_bson_safe(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        try:
            return int(value) if value == value.to_integral_value() else float(value)
        except Exception:
            return str(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _bson_safe(item())
        except Exception:
            pass
    return value

# Redis key for global concurrent execution counter
_CONCURRENCY_KEY = "workflow:concurrent_executions"
_CONCURRENCY_TTL = CONCURRENCY_TTL

# ── Condition (if/else) branch routing ──────────────────────────────────────
# A condition node emits meta.branch == "true" | "false". The edge leaving the
# node tells the executor which branch each downstream target is on. That branch
# may be carried on the edge's source_handle OR on its human label
# ("passed"/"failed"/"Met"/"Not Met"/yes/no/…). We normalise both. Returning
# None means "this edge carries no branch info" (legacy/unlabelled) — the caller
# then falls through (does not gate) to preserve old behaviour.
_TRUE_BRANCH_TOKENS = {
    "true", "yes", "y", "pass", "passed", "met", "ok", "success", "1", "t", "then",
}
_FALSE_BRANCH_TOKENS = {
    "false", "no", "n", "fail", "failed", "not met", "notmet", "not_met",
    "reject", "rejected", "0", "f", "else",
}


def _normalize_branch_token(value: Optional[str]) -> Optional[str]:
    """Map an edge handle/label to 'true' | 'false' | None."""
    if value is None:
        return None
    t = str(value).strip().lower()
    if not t:
        return None
    if t in ("true", "false"):
        return t
    if t in _TRUE_BRANCH_TOKENS:
        return "true"
    if t in _FALSE_BRANCH_TOKENS:
        return "false"
    return None


def _edge_condition_branch(edge: Any) -> Optional[str]:
    """The branch ('true'/'false') an edge belongs to, from source_handle then label."""
    return (
        _normalize_branch_token(getattr(edge, "source_handle", None))
        or _normalize_branch_token(getattr(edge, "label", None))
    )


class WorkflowState(TypedDict):
    """State flowing through the LangGraph execution."""
    execution_id: str
    workflow_id: str
    user_id: str
    variables: Dict[str, Any]
    node_outputs: Dict[str, Any]  # node_id -> output
    node_results: Dict[str, Any]  # node_id -> NodeExecutionResult dict
    status: str
    error: Optional[str]
    current_node: Optional[str]


class ConcurrencyLimitError(Exception):
    """Raised when the concurrent execution cap is reached."""
    pass


def _resolve_run_llm_limit(workflow) -> int:
    """Per-run LLM-call ceiling for a workflow: its optional ``max_run_llm_calls``
    override clamped to the admin hard-max, otherwise the global default."""
    raw = getattr(workflow, "max_run_llm_calls", None)
    limit = raw if isinstance(raw, int) and raw > 0 else MAX_RUN_LLM_CALLS
    return max(1, min(limit, MAX_RUN_LLM_CALLS_HARD))


class WorkflowExecutor:
    """Execute a workflow definition step-by-step following the DAG."""

    def __init__(self):
        from citra_cache import get_cache_manager
        self.cache = get_cache_manager()

    def _acquire_slot(self) -> bool:
        """Atomically increment the concurrent counter and check the cap.
        Returns True if a slot was acquired, False if at capacity."""
        # Lua script: atomic INCR + cap check. Returns 1 if acquired, 0 if over.
        lua = """
        local count = redis.call('INCR', KEYS[1])
        if count == 1 then
            redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        if count > tonumber(ARGV[1]) then
            redis.call('DECR', KEYS[1])
            return 0
        end
        return 1
        """
        try:
            result = self.cache.eval(lua, 1, _CONCURRENCY_KEY,
                                     MAX_CONCURRENT_EXECUTIONS, _CONCURRENCY_TTL)
            return result == 1
        except Exception as e:
            logger.warning("Concurrency gate unavailable (Redis error: %s) — allowing execution", e)
            return True  # fail-open: don't block if Redis is down

    def _release_slot(self):
        """Decrement the global concurrent execution counter."""
        lua = """
        local val = redis.call('DECR', KEYS[1])
        if val < 0 then
            redis.call('SET', KEYS[1], 0)
            redis.call('EXPIRE', KEYS[1], ARGV[1])
        end
        return val
        """
        try:
            self.cache.eval(lua, 1, _CONCURRENCY_KEY, _CONCURRENCY_TTL)
        except Exception as e:
            logger.warning("Failed to release concurrency slot: %s", e)

    def get_capacity(self) -> dict:
        """Return current concurrency usage for the capacity endpoint."""
        try:
            current = self.cache.get(_CONCURRENCY_KEY)
            running = int(current) if current else 0
        except Exception:
            running = 0
        return {
            "max_concurrent": MAX_CONCURRENT_EXECUTIONS,
            "running": max(running, 0),
            "available": max(MAX_CONCURRENT_EXECUTIONS - max(running, 0), 0),
        }

    async def _notify_failure(
        self,
        execution: WorkflowExecution,
        workflow: WorkflowDefinition,
        failed_node_label: str = "",
        error_message: str = "",
        retry_count: int = 0,
        retry_errors: Optional[list] = None,
    ):
        """Email a workflow failure to the OWNER only (the BA who authored it).

        Recipient = workflow.author_email — auto-derived from the authenticated
        user, never a free-text address — so a failure alert can never reach an
        outside-org address. Arbitrary "support" recipients are intentionally
        NOT supported. The legacy single-user lookup is kept only as a fallback
        for workflows created before author_email existed.
        See docs/workflow-visibility-ownership.md.
        """
        try:
            notif = getattr(workflow, "notifications", None) or WorkflowNotifications()
            if not notif.notify_on_failure:
                logger.info(
                    "Failure notifications disabled for workflow %s — skipping",
                    workflow.workflow_id,
                )
                return

            # Owner (author/BA) is the only emailed recipient — no arbitrary
            # addresses. Keep only a plausible, deliverable address.
            recipients: List[str] = []
            owner_addr = (workflow.author_email or "").strip().lower()
            if owner_addr and "@" in owner_addr:
                recipients.append(owner_addr)

            # Resolve a friendly display name for the greeting; also use the
            # user-doc email as a last-resort recipient for legacy workflows
            # that predate author_email.
            user_name = ""
            try:
                from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
                client = get_async_mongo_client()
                db = client[MONGODB_DATABASE]
                user_doc = await db["users"].find_one(
                    {"uid": execution.user_id},
                    {"email": 1, "displayName": 1, "name": 1},
                )
                if user_doc:
                    user_name = user_doc.get("displayName") or user_doc.get("name", "")
                    legacy_email = (user_doc.get("email") or "").strip().lower()
                    if not recipients and legacy_email and "@" in legacy_email:
                        recipients.append(legacy_email)
            except Exception as e:
                logger.warning("Could not resolve user details for failure email: %s", e)

            if not recipients:
                # The workflow FAILED and we have no one to tell — author_email
                # is empty or non-deliverable (e.g. a service-account id). Log
                # at ERROR so observability (GlitchTip → support) catches it;
                # otherwise the failure is invisible outside the Workflows tab.
                logger.error(
                    "ALERT HAS NO RECIPIENT — workflow %s (%s) FAILED but no "
                    "deliverable address resolved (author_email empty or a "
                    "service-account id). The BA will not be notified by email.",
                    workflow.workflow_id, workflow.name,
                )
                return

            from .notifications import send_execution_failure_notification
            for addr in recipients:
                await send_execution_failure_notification(
                    to_email=addr,
                    user_name=user_name,
                    workflow_name=workflow.name,
                    execution_id=execution.execution_id,
                    failed_node_label=failed_node_label,
                    error_message=error_message,
                    retry_count=retry_count,
                    retry_errors=retry_errors or [],
                )
            logger.info(
                "Workflow %s failure notified to %d recipient(s)",
                workflow.workflow_id, len(recipients),
            )
        except Exception as e:
            logger.error("Failed to send execution failure notification: %s", e, exc_info=True)

    async def execute(
        self,
        workflow: WorkflowDefinition,
        trigger_data: Optional[Dict[str, Any]] = None,
        execution_id: Optional[str] = None,
        environment: str = "test",
        enforce_concurrency: bool = True,
        jwt_token: Optional[str] = None,
    ) -> WorkflowExecution:
        execution_id = execution_id or str(uuid.uuid4())
        started = datetime.utcnow()

        execution = WorkflowExecution(
            execution_id=execution_id,
            workflow_id=workflow.workflow_id,
            user_id=workflow.user_id,
            status=ExecutionStatus.RUNNING,
            trigger_data=trigger_data or {},
            environment=environment,
            started_at=started,
        )

        # ── Concurrency gate ──
        slot_acquired = False
        if enforce_concurrency:
            if not self._acquire_slot():
                raise ConcurrencyLimitError(
                    f"Server is at capacity ({MAX_CONCURRENT_EXECUTIONS} concurrent executions). "
                    "Please try again shortly."
                )
            slot_acquired = True

        # ── Per-run LLM-call budget ──
        # Cap how many LLM calls this single run may make across all agent/LLM
        # nodes. Resolved from the workflow's optional override, clamped to the
        # admin hard-max, defaulting to the global ceiling. Set on the contextvar
        # NOW — before any node runs — so every node (incl. parallel branches
        # spawned from this context) shares one counter; the run aborts loud via
        # RunLlmBudgetExceeded when the limit is hit.
        start_run_budget(_resolve_run_llm_limit(workflow))

        # Publish initial status
        self._update_progress(execution)

        try:
            result = await asyncio.wait_for(
                self._execute_inner(execution, workflow, trigger_data, environment, jwt_token),
                timeout=EXECUTION_TIMEOUT_SECONDS,
            )
            return result
        except asyncio.TimeoutError:
            logger.error("Workflow %s timed out after %ds", execution_id, EXECUTION_TIMEOUT_SECONDS)
            execution.status = ExecutionStatus.TIMED_OUT
            execution.error = f"Workflow execution timed out after {EXECUTION_TIMEOUT_SECONDS}s"
            execution.completed_at = datetime.utcnow()
            self._update_progress(execution)
            await self._save_execution(execution)
            await self._notify_failure(
                execution, workflow,
                failed_node_label=execution.current_node or "",
                error_message=execution.error,
            )
            return execution
        finally:
            if slot_acquired:
                self._release_slot()

    async def _execute_inner(
        self,
        execution: WorkflowExecution,
        workflow: WorkflowDefinition,
        trigger_data: Optional[Dict[str, Any]],
        environment: str,
        jwt_token: Optional[str] = None,
    ) -> WorkflowExecution:
        try:
            # Build adjacency & indegree from edges
            adjacency: Dict[str, List[str]] = defaultdict(list)   # source -> [targets]
            incoming: Dict[str, List[str]] = defaultdict(list)     # target -> [sources]
            node_map = {n.id: n for n in workflow.nodes}

            for edge in workflow.edges:
                adjacency[edge.source].append(edge.target)
                incoming[edge.target].append(edge.source)

            # Find root nodes (no incoming edges)
            all_node_ids = set(node_map.keys())
            nodes_with_incoming = {e.target for e in workflow.edges}
            root_nodes = all_node_ids - nodes_with_incoming

            # ── Orphan guard: skip disconnected non-trigger nodes ──────────
            # A node with no edges at all (not source or target of any edge) and
            # that is not a trigger type is considered orphaned — it should NOT run.
            _TRIGGER_TYPES = frozenset({
                "manual_trigger", "scheduled_trigger", "webhook_trigger", "start_node",
            })
            connected_node_ids = (
                {e.source for e in workflow.edges} | {e.target for e in workflow.edges}
            )
            orphaned_nodes: Set[str] = set()
            for nid in list(root_nodes):
                node_type_val = node_map[nid].type
                # NodeType is a str enum; comparing to its string value works directly
                if nid not in connected_node_ids and str(node_type_val) not in _TRIGGER_TYPES:
                    orphaned_nodes.add(nid)
                    root_nodes.discard(nid)
                    logger.warning(
                        "Skipping orphaned node '%s' (%s) — not connected to workflow graph",
                        node_map[nid].label or nid, node_type_val,
                    )
            # Pre-mark orphaned nodes as SKIPPED so they appear in execution results
            for nid in orphaned_nodes:
                execution.node_results[nid] = NodeExecutionResult(
                    node_id=nid,
                    status=NodeExecutionStatus.SKIPPED,
                )
            # ────────────────────────────────────────────────────────────────

            if not root_nodes:
                raise ValueError("Workflow has no root node (trigger). Add a trigger node.")

            # Topological BFS execution
            variables = dict(workflow.variables or {})
            if trigger_data:
                variables.update(trigger_data)

            node_outputs: Dict[str, Any] = {}
            executed: Set[str] = set()
            # Nodes that FAILED but were configured continue_on_error — recorded
            # as failed, but they don't abort the run; their branch is pruned
            # (treated like a skip for downstream propagation).
            soft_failed: Set[str] = set()
            queue: deque = deque(root_nodes)
            steps_executed = 0
            # Mid-run checkpointing clock — persist progress after a node
            # completes, throttled to CHECKPOINT_INTERVAL_SECONDS so a crash
            # resumes from the last good node instead of restarting the graph.
            last_checkpoint = time.time()

            while queue:
                # Collect a batch of nodes whose dependencies are all met.
                # Dedup: a fan-in node (e.g. Merge / Wait All) is enqueued once
                # per completed parent, so the SAME node id can appear several
                # times in ``queue``. Without collapsing duplicates the node runs
                # once per parent and that multiplication cascades down every
                # downstream node — an email/output node then fires N times for a
                # single run. Skip ids already executed or already scheduled this
                # round so every node executes at most once per execution.
                batch = []
                next_queue: deque = deque()
                while queue:
                    nid = queue.popleft()
                    if nid in executed or nid in batch:
                        continue
                    deps = incoming.get(nid, [])
                    if all(d in executed for d in deps):
                        batch.append(nid)
                    elif nid not in next_queue:
                        next_queue.append(nid)

                if not batch:
                    # Remaining nodes have unmet deps — skip or error
                    if next_queue:
                        logger.warning(f"Cycle or unresolvable deps for: {list(next_queue)}")
                    break

                # Execute batch (could run in parallel, but sequential is safer for v1)
                for nid in batch:
                    # Cooperative cancel: stop at this node boundary before
                    # starting any further work if a cancel was requested.
                    if self._is_cancel_requested(execution.execution_id):
                        await self._mark_cancelled(execution)
                        return execution
                    node_def = node_map[nid]
                    execution.current_node = nid
                    self._update_progress(execution)

                    # Gather input: merge outputs from all parent nodes
                    parent_ids = incoming.get(nid, [])
                    if len(parent_ids) == 0:
                        input_data = trigger_data or {}
                    elif len(parent_ids) == 1:
                        input_data = node_outputs.get(parent_ids[0], {})
                    else:
                        input_data = [node_outputs.get(pid, {}) for pid in parent_ids]

                    # Handle condition branching AND switch routing
                    skip = False
                    for pid in parent_ids:
                        parent_output = node_outputs.get(pid, {})
                        if isinstance(parent_output, dict):
                            parent_def = node_map.get(pid)
                            parent_meta = parent_output.get("meta", {}) if isinstance(parent_output.get("meta"), dict) else {}

                            # Classic condition (if/else) branching
                            if parent_meta.get("condition_result") is not None and parent_def:
                                edges_from_parent = [e for e in workflow.edges
                                                     if e.source == pid and e.target == nid]
                                branch = parent_meta.get("branch", "true")
                                # Map each edge to its branch via source_handle, then
                                # label. Skip this target only when the edges DO carry
                                # branch info and NONE is on the taken branch. When no
                                # edge carries branch info (legacy/unlabelled), do NOT
                                # gate — preserves old behaviour. Fixes Bug 006b where
                                # unlabelled handles defaulted to "true", so both
                                # branches ran on true / neither on false.
                                _edge_branches = [_edge_condition_branch(e) for e in edges_from_parent]
                                _known_branches = [b for b in _edge_branches if b is not None]
                                if _known_branches and branch not in _known_branches:
                                    skip = True
                                input_data = parent_output

                            # Switch/Router multi-way branching
                            elif parent_meta.get("switch_result") and parent_def:
                                matched_route = parent_meta.get("matched_route")
                                edges_from_parent = [e for e in workflow.edges
                                                     if e.source == pid and e.target == nid]
                                if matched_route is not None:
                                    matching_switch = False
                                    for e in edges_from_parent:
                                        h = e.source_handle or "out-0"
                                        try:
                                            if int(h.replace("out-", "")) == matched_route:
                                                matching_switch = True
                                                break
                                        except (ValueError, AttributeError):
                                            pass
                                    if not matching_switch:
                                        skip = True
                                input_data = parent_output

                    # ── Propagate skip: if ALL parents are skipped (or were
                    # soft-failed via continue_on_error), skip this node too ──
                    if not skip and parent_ids:
                        all_parents_skipped = all(
                            nid_p in soft_failed
                            or (nid_p in execution.node_results
                                and execution.node_results[nid_p].status == NodeExecutionStatus.SKIPPED)
                            for nid_p in parent_ids
                        )
                        if all_parents_skipped:
                            skip = True
                            logger.debug(f"Propagating skip to {nid} — all parents skipped/soft-failed")

                    if skip:
                        # This node is on the non-taken branch
                        execution.node_results[nid] = NodeExecutionResult(
                            node_id=nid,
                            status=NodeExecutionStatus.SKIPPED,
                        )
                        executed.add(nid)
                        for child in adjacency.get(nid, []):
                            if child not in executed and child not in next_queue:
                                next_queue.append(child)
                        continue

                    # Instantiate and run the node
                    try:
                        node_instance = get_node(NodeType(node_def.type))
                    except (ValueError, KeyError) as e:
                        logger.error(f"Unknown node type {node_def.type}: {e}")
                        execution.node_results[nid] = NodeExecutionResult(
                            node_id=nid,
                            status=NodeExecutionStatus.FAILED,
                            error=f"Unknown node type: {node_def.type}",
                        )
                        executed.add(nid)
                        continue

                    # ── Step-limit guard ──
                    steps_executed += 1
                    if steps_executed > MAX_NODE_STEPS:
                        raise RuntimeError(
                            f"Workflow exceeded max step limit ({MAX_NODE_STEPS})"
                        )

                    # For AI Agent nodes, extract upstream schema context
                    wf_context = None
                    if node_def.type in ("ai_agent", NodeType.AI_AGENT):
                        try:
                            from .workflow_context_extractor import extract_workflow_context
                            wf_context = extract_workflow_context(
                                node_id=nid,
                                node_map=node_map,
                                node_outputs=node_outputs,
                                incoming=incoming,
                            )
                        except Exception as e:
                            logger.warning("Workflow context extraction failed (non-fatal): %s", e)

                    ctx = NodeContext(
                        node_id=nid,
                        node_config=node_def.config or {},
                        input_data=input_data,
                        variables=variables,
                        user_id=workflow.user_id,
                        execution_id=execution.execution_id,
                        environment=environment,
                        workflow_context=wf_context,
                        jwt_token=jwt_token,
                        workflow_id=workflow.workflow_id or "",
                        workflow_name=workflow.name or "",
                        owner_type=workflow.owner_type or "",
                        owner_id=workflow.owner_id or "",
                        org_id=workflow.org_id or "",
                        dept_ids=list(workflow.dept_ids or []),
                        author_user_id=workflow.author_user_id or "",
                        linked_smart_app_id=workflow.linked_smart_app_id,
                    )

                    try:
                        result = await asyncio.wait_for(
                            node_instance.run(ctx),
                            timeout=NODE_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        result = NodeExecutionResult(
                            node_id=nid,
                            status=NodeExecutionStatus.FAILED,
                            error=f"Node timed out after {NODE_TIMEOUT_SECONDS}s",
                        )
                    execution.node_results[nid] = result

                    if result.status == NodeExecutionStatus.COMPLETED:
                        node_outputs[nid] = result.output_data
                        executed.add(nid)

                        # Merge variables emitted by trigger / set-variable nodes
                        if isinstance(result.output_data, dict):
                            meta = result.output_data.get("meta", {})
                            if isinstance(meta, dict):
                                emitted_vars = meta.get("variables")
                                if isinstance(emitted_vars, dict):
                                    variables.update(emitted_vars)

                        # Handle human approval pause
                        if (isinstance(result.output_data, dict) and
                                result.output_data.get("meta", {}).get("status") == "waiting_approval"):
                            execution.status = ExecutionStatus.PAUSED
                            execution.paused_at_node = nid

                            # Create approval request and send notification
                            approval_id = await self._create_approval_request(
                                execution=execution,
                                workflow=workflow,
                                node_def=node_def,
                                output_data=result.output_data,
                            )
                            execution.approval_id = approval_id

                            self._update_progress(execution)
                            await self._save_execution(execution)
                            return execution

                        # Enqueue children
                        for child in adjacency.get(nid, []):
                            if child not in executed and child not in next_queue:
                                next_queue.append(child)

                        # Per-node checkpoint (throttled): persist completed
                        # node_results + accumulated variables so a worker crash
                        # resumes from here rather than re-running the graph.
                        if time.time() - last_checkpoint >= CHECKPOINT_INTERVAL_SECONDS:
                            execution.variables = dict(variables)
                            await self._save_execution(execution)
                            last_checkpoint = time.time()
                    elif (node_def.config or {}).get("continue_on_error"):
                        # Soft failure: record it, prune this branch, but keep
                        # the run going so independent branches still complete.
                        logger.warning(
                            "Node %s (%s) failed but continue_on_error is set — "
                            "recording the failure, skipping its branch, run "
                            "continues. Error: %s",
                            nid, node_def.label or "", result.error,
                        )
                        executed.add(nid)
                        soft_failed.add(nid)
                        for child in adjacency.get(nid, []):
                            if child not in executed and child not in next_queue:
                                next_queue.append(child)
                    else:
                        # Node failed — STOP immediately (strict fail-fast)
                        execution.status = ExecutionStatus.FAILED
                        execution.error = f"Node {nid} ({node_def.label}) failed: {result.error}"
                        execution.completed_at = datetime.utcnow()
                        # Log at ERROR so the failure lands in Loki and is captured
                        # as a GlitchTip event (LoggingIntegration event_level=ERROR),
                        # not just recorded in Mongo + emailed.
                        logger.error(
                            "Workflow %s execution %s FAILED at node %s (%s): %s (retries=%d)",
                            workflow.workflow_id, execution.execution_id, nid,
                            node_def.label or "", result.error or "Unknown error",
                            result.retry_count,
                        )
                        self._update_progress(execution)
                        await self._save_execution(execution)
                        await self._notify_failure(
                            execution, workflow,
                            failed_node_label=node_def.label or nid,
                            error_message=result.error or "Unknown error",
                            retry_count=result.retry_count,
                            retry_errors=result.retry_errors,
                        )
                        return execution

                queue = next_queue

            # All done
            execution.status = ExecutionStatus.COMPLETED
            execution.completed_at = datetime.utcnow()
            execution.variables = variables
            self._update_progress(execution)
            await self._save_execution(execution)

        except Exception as exc:
            logger.error(f"Workflow execution error: {exc}", exc_info=True)
            execution.status = ExecutionStatus.FAILED
            execution.error = str(exc)
            execution.completed_at = datetime.utcnow()
            self._update_progress(execution)
            await self._save_execution(execution)
        finally:
            # Reclaim this run's GridFS media on EVERY terminal exit — completed,
            # node-failure return, unhandled error, or cancel — not just the
            # happy path. A PAUSED run is skipped so its media survives to resume.
            await self._sweep_blobs_if_terminal(execution)

        return execution

    async def _sweep_blobs_if_terminal(self, execution: WorkflowExecution) -> None:
        """Delete this run's GridFS media once it reaches a terminal state.

        Called from a ``finally`` on both execute() and resume() so that every
        terminal exit reclaims its blobs, not only the completed path — failed,
        crashed, cancelled and rejected-approval runs previously leaked their
        uploaded/fetched media into ``workflow_blobs`` forever.

        A non-terminal status (PAUSED / RUNNING) is intentionally skipped: a
        paused run's media must survive until the approval is resumed and the
        consuming node runs. Best-effort — a cleanup failure is logged and never
        propagated, so it can't turn a finished run into a failed one.
        """
        terminal = {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
        }
        if execution.status not in terminal:
            return
        try:
            from .blob_store import delete_blobs_for_execution
            n = await delete_blobs_for_execution(execution.execution_id)
            if n:
                logger.info(
                    "Swept %d blob(s) for %s execution %s",
                    n, execution.status.value, execution.execution_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Blob cleanup failed for execution %s (non-fatal): %s",
                execution.execution_id, exc,
            )

    async def resume(
        self,
        execution: WorkflowExecution,
        workflow: WorkflowDefinition,
        approved: bool = True,
        jwt_token: Optional[str] = None,
    ) -> WorkflowExecution:
        """Resume a paused workflow from the paused node's children."""
        # Accept both PAUSED and the transient RESUMING state. The approve/
        # reject endpoint flips the execution to RESUMING (for immediate UI
        # feedback) BEFORE enqueuing the workflow.resume job, so by the time
        # the worker loads the doc here its status is RESUMING, not PAUSED.
        # Rejecting RESUMING left every human-approval run stuck forever.
        if execution.status not in (ExecutionStatus.PAUSED, ExecutionStatus.RESUMING):
            raise ValueError(
                f"Execution is not paused (status={getattr(execution.status, 'value', execution.status)})"
            )

        # Fresh per-run LLM-call budget for the resumed segment. The pre-pause
        # call count lived only in the in-memory contextvar and is gone once the
        # run paused and its job ended — so the resumed portion gets its own
        # full budget. Setting it here also prevents a STALE budget left in this
        # worker task by a prior execution from spuriously aborting the resume.
        start_run_budget(_resolve_run_llm_limit(workflow))

        paused_node = execution.paused_at_node
        if not approved:
            execution.status = ExecutionStatus.FAILED
            execution.error = "Human approval rejected"
            execution.completed_at = datetime.utcnow()
            self._update_progress(execution)
            await self._save_execution(execution)
            # Rejected approval is terminal — sweep the media the run was
            # holding for the (now-abandoned) approved branch.
            await self._sweep_blobs_if_terminal(execution)
            return execution

        execution.status = ExecutionStatus.RUNNING
        execution.paused_at_node = None
        execution.approval_id = None
        self._update_progress(execution)

        try:
            # Rebuild graph
            adjacency: Dict[str, List[str]] = defaultdict(list)
            incoming: Dict[str, List[str]] = defaultdict(list)
            node_map = {n.id: n for n in workflow.nodes}

            for edge in workflow.edges:
                adjacency[edge.source].append(edge.target)
                incoming[edge.target].append(edge.source)

            # Restore already-completed outputs from execution.node_results
            node_outputs: Dict[str, Any] = {}
            executed: Set[str] = set()
            for nid, nr in execution.node_results.items():
                nr_dict = nr if isinstance(nr, dict) else nr.model_dump()
                status = nr_dict.get("status", "")
                if hasattr(status, "value"):
                    status = status.value
                if status in ("completed", "skipped"):
                    executed.add(nid)
                    node_outputs[nid] = nr_dict.get("output_data")

            # Seed queue with the paused node's children
            variables = dict(workflow.variables or {})
            if execution.trigger_data:
                variables.update(execution.trigger_data)
            variables.update(execution.variables or {})

            queue: deque = deque()
            steps_executed = len(executed)  # count already-executed nodes
            if paused_node:
                # Resume-after-approval: continue from the paused node's children.
                for child in adjacency.get(paused_node, []):
                    if child not in executed:
                        queue.append(child)
            else:
                # Resume-after-CRASH: no single pause point. Re-seed every node
                # that hasn't completed/skipped yet; the BFS dependency gate
                # below runs each only once all its parents are executed, so
                # this correctly restarts every unfinished frontier from the
                # last checkpoint. Already-completed nodes (and their committed,
                # idempotent side effects) are not re-run.
                for nid in node_map:
                    if nid not in executed:
                        queue.append(nid)

            # Continue BFS from where we left off (same logic as execute())
            while queue:
                # Dedup duplicate queue entries so fan-in nodes (and everything
                # downstream of them) execute at most once — mirrors execute().
                batch = []
                next_queue: deque = deque()
                while queue:
                    nid = queue.popleft()
                    if nid in executed or nid in batch:
                        continue
                    deps = incoming.get(nid, [])
                    if all(d in executed for d in deps):
                        batch.append(nid)
                    elif nid not in next_queue:
                        next_queue.append(nid)

                if not batch:
                    if next_queue:
                        logger.warning(f"Resume: unresolvable deps for: {list(next_queue)}")
                    break

                for nid in batch:
                    # Cooperative cancel — honour a cancel requested while the
                    # run is paused/resuming, same as the main execute loop.
                    if self._is_cancel_requested(execution.execution_id):
                        await self._mark_cancelled(execution)
                        return execution
                    node_def = node_map[nid]
                    execution.current_node = nid
                    self._update_progress(execution)

                    parent_ids = incoming.get(nid, [])
                    if len(parent_ids) == 0:
                        input_data = execution.trigger_data or {}
                    elif len(parent_ids) == 1:
                        input_data = node_outputs.get(parent_ids[0], {})
                    else:
                        input_data = [node_outputs.get(pid, {}) for pid in parent_ids]

                    # Handle condition/switch branching (same as execute())
                    skip = False
                    for pid in parent_ids:
                        parent_output = node_outputs.get(pid, {})
                        if isinstance(parent_output, dict):
                            parent_meta = parent_output.get("meta", {})
                            parent_def = node_map.get(pid)
                            if parent_meta.get("condition_result") is not None and parent_def:
                                edges_from_parent = [e for e in workflow.edges
                                                     if e.source == pid and e.target == nid]
                                branch = parent_meta.get("branch", "true")
                                # Map each edge to its branch via source_handle, then
                                # label. Skip this target only when the edges DO carry
                                # branch info and NONE is on the taken branch. When no
                                # edge carries branch info (legacy/unlabelled), do NOT
                                # gate — preserves old behaviour. Fixes Bug 006b where
                                # unlabelled handles defaulted to "true", so both
                                # branches ran on true / neither on false.
                                _edge_branches = [_edge_condition_branch(e) for e in edges_from_parent]
                                _known_branches = [b for b in _edge_branches if b is not None]
                                if _known_branches and branch not in _known_branches:
                                    skip = True
                                input_data = parent_output
                            elif parent_meta.get("switch_result") and parent_def:
                                matched_route = parent_meta.get("matched_route")
                                edges_from_parent = [e for e in workflow.edges
                                                     if e.source == pid and e.target == nid]
                                if matched_route is not None:
                                    matching_switch = False
                                    for e in edges_from_parent:
                                        h = e.source_handle or "out-0"
                                        try:
                                            if int(h.replace("out-", "")) == matched_route:
                                                matching_switch = True
                                                break
                                        except (ValueError, AttributeError):
                                            pass
                                    if not matching_switch:
                                        skip = True
                                input_data = parent_output

                    if skip:
                        execution.node_results[nid] = NodeExecutionResult(
                            node_id=nid,
                            status=NodeExecutionStatus.SKIPPED,
                        )
                        executed.add(nid)
                        for child in adjacency.get(nid, []):
                            if child not in executed and child not in next_queue:
                                next_queue.append(child)
                        continue

                    try:
                        node_instance = get_node(NodeType(node_def.type))
                    except (ValueError, KeyError) as e:
                        logger.error(f"Unknown node type {node_def.type}: {e}")
                        execution.node_results[nid] = NodeExecutionResult(
                            node_id=nid,
                            status=NodeExecutionStatus.FAILED,
                            error=f"Unknown node type: {node_def.type}",
                        )
                        executed.add(nid)
                        continue

                    # ── Step-limit guard ──
                    steps_executed += 1
                    if steps_executed > MAX_NODE_STEPS:
                        raise RuntimeError(
                            f"Workflow exceeded max step limit ({MAX_NODE_STEPS})"
                        )

                    ctx = NodeContext(
                        node_id=nid,
                        node_config=node_def.config or {},
                        input_data=input_data,
                        variables=variables,
                        user_id=workflow.user_id,
                        execution_id=execution.execution_id,
                        environment=execution.environment,
                        jwt_token=jwt_token,
                        workflow_id=workflow.workflow_id or "",
                        workflow_name=workflow.name or "",
                        owner_type=workflow.owner_type or "",
                        owner_id=workflow.owner_id or "",
                        org_id=workflow.org_id or "",
                        dept_ids=list(workflow.dept_ids or []),
                        author_user_id=workflow.author_user_id or "",
                        linked_smart_app_id=workflow.linked_smart_app_id,
                    )

                    try:
                        result = await asyncio.wait_for(
                            node_instance.run(ctx),
                            timeout=NODE_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        result = NodeExecutionResult(
                            node_id=nid,
                            status=NodeExecutionStatus.FAILED,
                            error=f"Node timed out after {NODE_TIMEOUT_SECONDS}s",
                        )
                    execution.node_results[nid] = result

                    if result.status == NodeExecutionStatus.COMPLETED:
                        node_outputs[nid] = result.output_data
                        executed.add(nid)

                        if (isinstance(result.output_data, dict) and
                                result.output_data.get("meta", {}).get("status") == "waiting_approval"):
                            execution.status = ExecutionStatus.PAUSED
                            execution.paused_at_node = nid
                            self._update_progress(execution)
                            await self._save_execution(execution)
                            return execution

                        for child in adjacency.get(nid, []):
                            if child not in executed and child not in next_queue:
                                next_queue.append(child)
                    else:
                        # Node failed — STOP immediately (strict fail-fast)
                        execution.status = ExecutionStatus.FAILED
                        execution.error = f"Node {nid} ({node_def.label}) failed: {result.error}"
                        execution.completed_at = datetime.utcnow()
                        # Log at ERROR so the failure lands in Loki and is captured
                        # as a GlitchTip event (LoggingIntegration event_level=ERROR),
                        # not just recorded in Mongo + emailed.
                        logger.error(
                            "Workflow %s execution %s FAILED at node %s (%s): %s (retries=%d)",
                            workflow.workflow_id, execution.execution_id, nid,
                            node_def.label or "", result.error or "Unknown error",
                            result.retry_count,
                        )
                        self._update_progress(execution)
                        await self._save_execution(execution)
                        await self._notify_failure(
                            execution, workflow,
                            failed_node_label=node_def.label or nid,
                            error_message=result.error or "Unknown error",
                            retry_count=result.retry_count,
                            retry_errors=result.retry_errors,
                        )
                        return execution

                queue = next_queue

            execution.status = ExecutionStatus.COMPLETED
            execution.completed_at = datetime.utcnow()
            execution.variables = variables
            self._update_progress(execution)
            await self._save_execution(execution)

        except Exception as exc:
            logger.error(f"Resume execution error: {exc}", exc_info=True)
            execution.status = ExecutionStatus.FAILED
            execution.error = str(exc)
            execution.completed_at = datetime.utcnow()
            self._update_progress(execution)
            await self._save_execution(execution)
        finally:
            # Same terminal-exit sweep as execute(): a resumed run that completes,
            # fails, or is cancelled reclaims its media; a re-pause is skipped.
            await self._sweep_blobs_if_terminal(execution)

        return execution

    # ── Cooperative cancellation ────────────────────────────────────────
    # The /cancel endpoint sets a short-lived Redis flag; the run loop polls
    # it at every node boundary and stops loudly (status=CANCELLED) rather
    # than the worker being killed mid-node — so no node's writes are torn
    # half-way. TTL = PROGRESS_CACHE_TTL (outlives the longest run), and the
    # Mongo cancel_requested flag is the durable backstop across resumes.
    @staticmethod
    def _cancel_key(execution_id: str) -> str:
        return f"workflow:cancel:{execution_id}"

    def _is_cancel_requested(self, execution_id: str) -> bool:
        """True if a cancel was requested for this run.

        A cache-read error is logged and treated as 'not cancelled': a
        transient Redis blip must never abort an otherwise-healthy run. The
        Mongo cancel_requested flag + crash-resume sweep are the durable path
        if the worker dies before observing the flag.
        """
        try:
            return bool(self.cache.get(self._cancel_key(execution_id)))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Cancel-flag probe failed for %s (treating as not cancelled): %s",
                execution_id, e,
            )
            return False

    async def _mark_cancelled(self, execution: WorkflowExecution):
        """Finalise a run as CANCELLED after a cooperative stop."""
        execution.status = ExecutionStatus.CANCELLED
        if not execution.error:
            execution.error = "Execution cancelled by user"
        execution.completed_at = datetime.utcnow()
        self._update_progress(execution)
        await self._save_execution(execution)
        # One-shot flag: clear it so a recycled execution_id can't re-trigger.
        try:
            self.cache.delete(self._cancel_key(execution.execution_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed clearing cancel flag for %s: %s",
                execution.execution_id, e,
            )
        logger.info(
            "Workflow execution %s CANCELLED at node %s",
            execution.execution_id, getattr(execution, "current_node", None),
        )

    def _update_progress(self, execution: WorkflowExecution):
        """Push status to Redis for real-time polling."""
        key = f"workflow:exec:{execution.execution_id}"
        # Build node status map for live canvas highlighting
        node_statuses = {}
        for nid, nr in execution.node_results.items():
            if isinstance(nr, dict):
                node_statuses[nid] = nr.get("status", "pending")
                if hasattr(nr.get("status"), "value"):
                    node_statuses[nid] = nr["status"].value
            elif hasattr(nr, "status"):
                node_statuses[nid] = nr.status.value if hasattr(nr.status, "value") else nr.status

        data = {
            "execution_id": execution.execution_id,
            "workflow_id": execution.workflow_id,
            "user_id": execution.user_id,
            "status": execution.status.value if isinstance(execution.status, ExecutionStatus) else execution.status,
            "current_node": getattr(execution, "current_node", None),
            "node_statuses": node_statuses,
            "error": execution.error,
            "updated_at": datetime.utcnow().isoformat(),
        }
        try:
            self.cache.set(key, json.dumps(data, default=str), ex=PROGRESS_CACHE_TTL)
        except Exception as e:
            logger.warning(f"Failed to update progress in cache: {e}")

    async def _save_execution(self, execution: WorkflowExecution):
        """Persist execution record to MongoDB."""
        try:
            from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
            client = get_async_mongo_client()
            db = client[MONGODB_DATABASE]
            col = db["WorkflowExecutions"]

            doc = _bson_safe(execution.model_dump())
            # Convert enums to strings for MongoDB
            doc["status"] = doc["status"].value if hasattr(doc["status"], "value") else doc["status"]
            for nid, nr in doc.get("node_results", {}).items():
                if isinstance(nr, dict) and "status" in nr:
                    s = nr["status"]
                    nr["status"] = s.value if hasattr(s, "value") else s

            await col.update_one(
                {"execution_id": execution.execution_id},
                {"$set": doc},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Failed to save execution: {e}", exc_info=True)

    async def _create_approval_request(
        self,
        execution: WorkflowExecution,
        workflow: WorkflowDefinition,
        node_def,
        output_data: dict,
    ) -> str:
        """Create an ApprovalRequest in MongoDB and send email notification."""
        from .models import ApprovalRequest

        approval = ApprovalRequest(
            execution_id=execution.execution_id,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            user_id=execution.user_id,
            node_id=node_def.id,
            node_label=node_def.label or node_def.id,
            message=output_data.get("meta", {}).get("message", ""),
            timeout_hours=output_data.get("meta", {}).get("timeout_hours", DEFAULT_APPROVAL_TIMEOUT_HOURS),
            data_preview=output_data.get("meta", {}).get("data_preview"),
            created_at=datetime.utcnow(),
        )

        # Save to MongoDB
        try:
            from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
            client = get_async_mongo_client()
            db = client[MONGODB_DATABASE]
            await db["WorkflowApprovals"].insert_one(approval.model_dump())
        except Exception as e:
            logger.error(f"Failed to save approval request: {e}")

        # Look up user email and send notification.
        # Priority: workflow.author_email (always set for new workflows) →
        # DB lookup by author_user_id → DB lookup by uid (legacy fallback).
        try:
            from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
            client = get_async_mongo_client()
            db = client[MONGODB_DATABASE]

            approver_email = (workflow.author_email or "").strip()
            user_name = ""

            if not approver_email:
                # Try DB lookup by author_user_id first (human email),
                # then by execution.user_id (may be a service account).
                for uid_candidate in filter(None, [workflow.author_user_id, execution.user_id]):
                    user_doc = await db["users"].find_one(
                        {"$or": [{"uid": uid_candidate}, {"email": uid_candidate}]},
                        {"email": 1, "displayName": 1, "name": 1},
                    )
                    if user_doc and user_doc.get("email"):
                        approver_email = user_doc["email"]
                        user_name = user_doc.get("displayName") or user_doc.get("name", "")
                        break

            if approver_email:
                from .notifications import send_approval_notification
                import json

                # Store full data in DB; email shows preview with link to full details
                preview_str = None
                if approval.data_preview:
                    try:
                        full_str = json.dumps(approval.data_preview, indent=2, default=str)
                        preview_str = full_str
                    except Exception:
                        preview_str = str(approval.data_preview)

                await send_approval_notification(
                    to_email=approver_email,
                    user_name=user_name,
                    workflow_name=workflow.name,
                    node_label=approval.node_label,
                    message=approval.message,
                    execution_id=execution.execution_id,
                    approval_id=approval.approval_id,
                    timeout_hours=approval.timeout_hours,
                    data_preview=preview_str,
                )
                # Mark notification sent
                await db["WorkflowApprovals"].update_one(
                    {"approval_id": approval.approval_id},
                    {"$set": {"notification_sent": True}},
                )
            else:
                logger.warning(
                    "No email found for user %s — approval notification not sent",
                    execution.user_id,
                )
        except Exception as e:
            logger.error(f"Failed to send approval notification: {e}", exc_info=True)

        return approval.approval_id
