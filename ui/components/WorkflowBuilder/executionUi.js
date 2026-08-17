// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * executionUi.js — Shared presentation helpers for workflow execution UI.
 *
 * One source of truth for status icons/colors, environment badges, duration
 * formatting and timestamp formatting, reused by ExecutionMonitor (live run
 * modal), WorkflowRunHistory (per-workflow runs list) and WorkflowRunDetail
 * (execution detail page).
 *
 * @typedef {Object} WorkflowRunSummary
 * @property {string} execution_id
 * @property {string} workflow_id
 * @property {string} status
 * @property {string} [environment]   stored environment value (source of truth)
 * @property {string} [trigger_type]
 * @property {string} [started_at]
 * @property {string} [completed_at]
 * @property {number|null} [duration_ms]
 * @property {string|null} [current_node]
 * @property {string|null} [paused_at_node]
 * @property {string|null} [error]
 *
 * @typedef {Object} NodeRunResult
 * @property {string} status
 * @property {*} [output_data]
 * @property {string} [output_type]
 * @property {string} [error]
 * @property {number} [retry_count]
 * @property {string[]} [retry_errors]
 * @property {string} [started_at]
 * @property {string} [completed_at]
 * @property {number} [duration_ms]
 */

// Icon + color for every execution-level and node-level status the backend can
// emit (ExecutionStatus + NodeExecutionStatus, plus the transient "queued"/
// "resuming" the API may surface before a terminal state).
export const STATUS_META = {
  queued: { name: 'ellipsis-horizontal-circle', color: '#94a3b8', label: 'Queued' },
  pending: { name: 'ellipsis-horizontal-circle', color: '#94a3b8', label: 'Pending' },
  running: { name: 'sync-outline', color: '#3b82f6', label: 'Running' },
  resuming: { name: 'sync-outline', color: '#3b82f6', label: 'Resuming' },
  waiting: { name: 'pause-circle', color: '#f59e0b', label: 'Waiting' },
  waiting_approval: { name: 'pause-circle', color: '#f59e0b', label: 'Waiting approval' },
  paused: { name: 'pause-circle', color: '#f59e0b', label: 'Paused' },
  completed: { name: 'checkmark-circle', color: '#22c55e', label: 'Completed' },
  failed: { name: 'close-circle', color: '#ef4444', label: 'Failed' },
  timed_out: { name: 'timer-outline', color: '#ef4444', label: 'Timed out' },
  cancelled: { name: 'ban-outline', color: '#64748b', label: 'Cancelled' },
  skipped: { name: 'remove-circle', color: '#64748b', label: 'Skipped' },
};

// Execution statuses that mean polling can stop.
export const TERMINAL_STATUSES = ['completed', 'failed', 'timed_out', 'cancelled'];

/** Icon/color/label for a status, defaulting to "pending" for unknown values. */
export function statusMeta(status) {
  return STATUS_META[String(status || '').toLowerCase()] || STATUS_META.pending;
}

export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(String(status || '').toLowerCase());
}

/**
 * Environment badge built from the STORED environment value only — never
 * inferred from deployment state. `prod` → Production, `test` → Test, anything
 * missing/unknown → Unknown.
 */
export function envMeta(environment) {
  const env = String(environment || '').toLowerCase();
  if (env === 'prod' || env === 'production') {
    return { key: 'prod', label: 'Production', color: '#16a34a', bg: '#dcfce7' };
  }
  if (env === 'test') {
    return { key: 'test', label: 'Test', color: '#d97706', bg: '#fef3c7' };
  }
  return { key: 'unknown', label: 'Unknown', color: '#64748b', bg: '#e2e8f0' };
}

/**
 * True when a deployed workflow produced a run stored as `test` — the manager
 * rule says deployed workflows should run in production. Kept separate from
 * envMeta so the displayed value and the warning never conflate.
 */
export function isEnvMismatch(environment, workflowIsDeployed) {
  return Boolean(workflowIsDeployed) && String(environment || '').toLowerCase() === 'test';
}

/** ms between two ISO timestamps, or null when either is missing/invalid. */
export function deriveDurationMs(startedAt, completedAt) {
  if (!startedAt || !completedAt) return null;
  // Both ends go through the same parser. The offset would cancel out here even
  // if it didn't, but keeping one parser means there is only one place that
  // knows how the API spells a timestamp.
  const s = parseApiTime(startedAt);
  const c = parseApiTime(completedAt);
  if (Number.isNaN(s) || Number.isNaN(c)) return null;
  const d = c - s;
  return d >= 0 ? d : null;
}

/**
 * Human-friendly duration. Prefers an explicit `durationMs`; otherwise derives
 * it from the timestamps. Returns "—" when nothing usable is available so old
 * records with incomplete timestamps render cleanly.
 */
export function formatDuration(durationMs, startedAt, completedAt) {
  let ms = (durationMs == null) ? deriveDurationMs(startedAt, completedAt) : durationMs;
  if (ms == null || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${ms}ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs.toFixed(secs < 10 ? 1 : 0)}s`;
  const mins = Math.floor(secs / 60);
  const remSecs = Math.round(secs % 60);
  if (mins < 60) return `${mins}m ${remSecs}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

/**
 * Parse a timestamp coming from the API.
 *
 * The backend writes `datetime.utcnow()` — UTC, but NAIVE — so the JSON carries
 * no "Z" and no offset: "2026-08-09T12:59:12.054000". `Date.parse` is specified
 * to read a date-time with no offset as LOCAL time, so every timestamp rendered
 * the viewer's UTC offset in the past: a run that had just finished displayed as
 * "5h ago" in IST (UTC+5:30). It reads correctly only for a viewer in UTC, which
 * is why it survived.
 *
 * An offset-less value is therefore parsed as the UTC it actually is. Values
 * that DO carry a zone are left untouched, so this stays correct if the API is
 * later changed to emit proper ISO-8601.
 */
export function parseApiTime(value) {
  if (typeof value !== 'string') return Date.parse(value);
  const s = value.trim();
  // Only date-TIME forms are ambiguous; a date-only value is already UTC by spec.
  const isDateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s);
  const hasZone = /([Zz]|[+-]\d{2}:?\d{2})$/.test(s);
  return Date.parse(isDateTime && !hasZone ? `${s.replace(' ', 'T')}Z` : s);
}

/** Absolute timestamp, locale-formatted, or "—" when missing/invalid. */
export function formatTimestamp(value) {
  if (!value) return '—';
  const t = parseApiTime(value);
  if (Number.isNaN(t)) return '—';
  return new Date(t).toLocaleString();
}

/** Short relative timestamp ("3m ago", "2h ago", "5d ago"), or "—". */
export function formatRelative(value) {
  if (!value) return '—';
  const t = parseApiTime(value);
  if (Number.isNaN(t)) return '—';
  const diff = Date.now() - t;
  if (diff < 0) return 'just now';
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return formatTimestamp(value);
}

/** Normalize the trigger label (API may send `trigger_type` or legacy `trigger`). */
export function triggerLabel(run) {
  const t = run?.trigger_type || run?.trigger || 'manual';
  return String(t);
}

/**
 * Pull the primary human-readable text/markdown out of a node's output_data so
 * AI/LLM reports render as readable content instead of a raw JSON blob.
 *
 * Handles the common executor shapes:
 *   - a bare string
 *   - { items: [{ result: "..." }, ...] }   → joins each item's result
 *   - { result | text | output | content: "..." }
 *   - { items: ["...", ...] }               → joins string items
 * Returns null when there is no obvious text field (caller shows JSON instead).
 */
export function extractPrimaryText(outputData) {
  if (outputData == null) return null;
  if (typeof outputData === 'string') {
    return outputData.trim() ? outputData : null;
  }
  if (typeof outputData !== 'object') return null;

  const pickString = (v) => (typeof v === 'string' && v.trim() ? v : null);

  if (Array.isArray(outputData.items)) {
    const parts = [];
    for (const it of outputData.items) {
      if (typeof it === 'string') { if (it.trim()) parts.push(it); continue; }
      if (it && typeof it === 'object') {
        const s = pickString(it.result) || pickString(it.text)
          || pickString(it.output) || pickString(it.content) || pickString(it.message);
        if (s) parts.push(s);
      }
    }
    if (parts.length) return parts.join('\n\n---\n\n');
  }

  return pickString(outputData.result) || pickString(outputData.text)
    || pickString(outputData.output) || pickString(outputData.content)
    || pickString(outputData.message) || null;
}

/** Stable stringify (sorted keys) for order-insensitive deep equality checks. */
function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => JSON.stringify(k) + ':' + stableStringify(value[k])).join(',')}}`;
}

/** True when two JSON-serializable values are deeply equal (key order ignored). */
export function jsonEqual(a, b) {
  try { return stableStringify(a) === stableStringify(b); }
  catch { return false; }
}

/** True when a value has no meaningful content (null/undefined/{}/[]). */
export function isEmptyValue(v) {
  if (v == null) return true;
  if (typeof v === 'object') return Object.keys(v).length === 0;
  if (typeof v === 'string') return v.trim() === '';
  return false;
}
