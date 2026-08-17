// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * WorkflowService.js — API client for the workflow engine backend
 */
import authService from './authService';
import { WORKFLOW_API_BASE } from '../config/config';
// The backend's `detail` field has three shapes and only one of them survives
// `new Error()` — see apiError.js.
import { buildApiError } from './apiError';

class WorkflowService {
  constructor() {
    this.baseURL = `${WORKFLOW_API_BASE}/api/workflows`;
    this.defaultTimeout = 30000; // 30s
  }

  _headers() {
    return { 'Content-Type': 'application/json' };
  }

  async _fetch(url, options = {}, timeout = this.defaultTimeout) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      return await authService.authenticatedFetch(url, {
        ...options,
        signal: controller.signal,
      });
    } catch (err) {
      const message = err?.message || '';
      if (err?.name === 'AbortError' || message.toLowerCase().includes('aborted')) {
        const seconds = Math.round((timeout || this.defaultTimeout) / 1000);
        throw new Error(`Workflow request timed out after ${seconds}s. Try a smaller prompt or retry once the service is free.`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  async _fetchWithRetry(url, options = {}, { timeout, retries = 2, baseDelay = 500 } = {}) {
    let lastErr;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        return await this._fetch(url, options, timeout || this.defaultTimeout);
      } catch (err) {
        lastErr = err;
        if (attempt < retries) {
          const delay = baseDelay * Math.pow(2, attempt) + Math.random() * 200;
          await new Promise((r) => setTimeout(r, delay));
        }
      }
    }
    throw lastErr;
  }

  // ── Node palette ──────────────────────────────────
  async getNodeSchemas() {
    const resp = await this._fetch(`${this.baseURL}/node-schemas`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to fetch node schemas');
    return resp.json();
  }

  // ── CRUD ──────────────────────────────────────────
  /**
   * List workflows under one of three scopes (matches
   * citra-workflow's GET /api/workflows):
   * Returns every workflow in the caller's org. Under org-only ownership
   * the legacy mine/shared/admin tabs collapse into one — there's no
   * separate "my workflows" scope to filter on. The backend ignores any
   * ``scope`` param.
   *
   * @param {Object} [opts]
   * @param {number}  [opts.skip=0]
   * @param {number}  [opts.limit=50]
   * @param {string}  [opts.search=''] — case-insensitive substring matched
   *   against workflow name and description, server-side (so it spans all
   *   workflows, not just the current page). Empty/whitespace is ignored.
   * @returns {Promise<{workflows: Array, total?: number, has_more?: boolean}>}
   */
  async listWorkflows({ skip = 0, limit = 50, search = '' } = {}) {
    const params = new URLSearchParams({
      skip: String(skip),
      limit: String(limit),
    });
    const trimmedSearch = (search || '').trim();
    if (trimmedSearch) {
      params.set('search', trimmedSearch);
    }
    const resp = await this._fetch(
      `${this.baseURL}?${params.toString()}`,
      { method: 'GET', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to list workflows');
    return resp.json();
  }

  async getWorkflow(workflowId) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Workflow not found');
    return resp.json();
  }

  async createWorkflow(data) {
    const resp = await this._fetch(this.baseURL, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      throw await buildApiError(resp, 'Failed to create workflow');
    }
    return resp.json();
  }

  async updateWorkflow(workflowId, data) {
    const resp = await this._fetchWithRetry(`${this.baseURL}/${workflowId}`, {
      method: 'PUT',
      headers: this._headers(),
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      throw await buildApiError(resp, 'Failed to update workflow');
    }
    return resp.json();
  }

  async deleteWorkflow(workflowId) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}`, {
      method: 'DELETE',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to delete workflow');
    return resp.json();
  }

  async duplicateWorkflow(workflowId) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/duplicate`, {
      method: 'POST',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to duplicate workflow');
    return resp.json();
  }

  /**
   * Admin / owner transfer. Re-homes a workflow's owner_type/owner_id.
   * Server-side RBAC: owner OR admin role; non-global admins must be a
   * member of the new SA/dept/org. Matches the existing endpoint at
   * citra-workflow/router.py:2474.
   *
   * @param {string} workflowId
   * @param {{ targetOwnerType, targetOwnerId, reason? }} body
   */
  async transferWorkflow(workflowId, { targetOwnerType, targetOwnerId, reason } = {}) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/transfer`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({
        new_owner_type: targetOwnerType,
        new_owner_id: targetOwnerId,
        reason: reason || '',
      }),
    });
    if (!resp.ok) throw new Error('Failed to transfer workflow');
    return resp.json();
  }

  // ── Execution ─────────────────────────────────────
  async executeWorkflow(workflowId, body = {}) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/execute`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify(body),
    }, 120000); // 2min timeout for execution
    if (resp.status === 503) {
      const err = await resp.json().catch(() => ({}));
      const e = new Error(err.detail || 'Server is at capacity. Please try again shortly.');
      e.isCapacity = true;
      throw e;
    }
    if (!resp.ok) throw new Error('Failed to execute workflow');
    return resp.json();
  }

  async getExecutionCapacity() {
    const resp = await this._fetch(`${this.baseURL}/execution-capacity`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) return null;
    return resp.json();
  }

  async listExecutions(workflowId, opts = {}) {
    // opts: { skip, limit, status } — all optional.
    const params = new URLSearchParams();
    if (opts.skip != null) params.set('skip', String(opts.skip));
    if (opts.limit != null) params.set('limit', String(opts.limit));
    if (opts.status) params.set('status', String(opts.status));
    const qs = params.toString();
    const resp = await this._fetch(
      `${this.baseURL}/${workflowId}/executions${qs ? `?${qs}` : ''}`,
      { method: 'GET', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to list executions');
    return resp.json();
  }

  async getExecution(executionId) {
    const resp = await this._fetch(
      `${this.baseURL}/executions/${executionId}`,
      { method: 'GET', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Execution not found');
    return resp.json();
  }

  async getExecutionStatus(executionId) {
    const resp = await this._fetch(
      `${this.baseURL}/executions/${executionId}/status`,
      { method: 'GET', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to get status');
    return resp.json();
  }

  async approveExecution(executionId) {
    const resp = await this._fetch(
      `${this.baseURL}/executions/${executionId}/approve`,
      { method: 'POST', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to approve');
    return resp.json();
  }

  async rejectExecution(executionId) {
    const resp = await this._fetch(
      `${this.baseURL}/executions/${executionId}/reject`,
      { method: 'POST', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to reject');
    return resp.json();
  }

  // Request cancellation of a non-terminal run. Cooperative: the run stops at
  // its next node boundary (or immediately if it was paused for approval).
  async cancelExecution(executionId) {
    const resp = await this._fetch(
      `${this.baseURL}/executions/${executionId}/cancel`,
      { method: 'POST', headers: this._headers() }
    );
    if (!resp.ok) {
      let detail = 'Failed to cancel';
      try { detail = (await resp.json())?.detail || detail; } catch { /* keep default */ }
      throw new Error(detail);
    }
    return resp.json();
  }

  // ── Maintenance ───────────────────────────────────
  // Workflow nodes stash binary media (uploaded images, fetched PDFs, audio)
  // in GridFS. A run normally sweeps its own media when it finishes, but a
  // hard-killed worker can leave orphans behind. These two admin-only calls
  // measure and reclaim that orphaned media.

  // Dry-run scan — counts stored + orphaned media. Deletes nothing.
  async getBlobMaintenanceUsage() {
    const resp = await this._fetch(`${this.baseURL}/maintenance/blob-usage`, {
      method: 'GET', headers: this._headers(),
    });
    if (!resp.ok) throw await buildApiError(resp, 'Failed to scan workflow media');
    return resp.json();
  }

  // Reclaim orphaned media (terminal/missing runs only; live runs untouched).
  async sweepOrphanBlobs() {
    const resp = await this._fetch(`${this.baseURL}/maintenance/blob-sweep`, {
      method: 'POST', headers: this._headers(),
    });
    if (!resp.ok) throw await buildApiError(resp, 'Failed to reclaim workflow media');
    return resp.json();
  }

  // ── Templates ─────────────────────────────────────
  async listTemplates() {
    const resp = await this._fetch(`${this.baseURL}/templates`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to list templates');
    return resp.json();
  }

  async getTemplate(templateId) {
    const resp = await this._fetch(`${this.baseURL}/templates/${templateId}`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Template not found');
    return resp.json();
  }

  async createFromTemplate(templateId) {
    const resp = await this._fetch(
      `${this.baseURL}/templates/${templateId}/create`,
      { method: 'POST', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to create from template');
    return resp.json();
  }

  // ── Agent Tools ───────────────────────────────────
  async getAgentTools() {
    const resp = await this._fetch(`${this.baseURL}/agent-tools`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to fetch agent tools');
    return resp.json();
  }

  // ── AI Code Generation ────────────────────────────
  async generateCode(prompt, inputSchema = null) {
    const resp = await this._fetch(`${this.baseURL}/generate-code`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ prompt, input_schema: inputSchema }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to generate code');
    }
    return resp.json();
  }

  // ── Deploy / Undeploy ─────────────────────────────
  async deployWorkflow(workflowId, note = '') {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/deploy`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ action: 'deploy', note }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to deploy workflow');
    }
    return resp.json();
  }

  async undeployWorkflow(workflowId) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/deploy`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ action: 'undeploy' }),
    });
    if (!resp.ok) throw new Error('Failed to undeploy workflow');
    return resp.json();
  }

  // ── Version history / rollback ────────────────────
  async listWorkflowVersions(workflowId) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/versions`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to load version history');
    }
    return resp.json();
  }

  async getWorkflowVersion(workflowId, versionNumber) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/versions/${versionNumber}`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to load version');
    }
    return resp.json();
  }

  // Structured diff of what restoring versionNumber would change.
  // against='current' (default) = live graph → snapshot, i.e. the exact
  // patch a rollback applies; against=<int> compares two snapshots.
  async getWorkflowVersionDiff(workflowId, versionNumber, against = 'current') {
    const resp = await this._fetch(
      `${this.baseURL}/${workflowId}/versions/${versionNumber}/diff?against=${encodeURIComponent(against)}`,
      { method: 'GET', headers: this._headers() },
    );
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to load version diff');
    }
    return resp.json();
  }

  async rollbackWorkflow(workflowId, versionNumber, note = '') {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/rollback`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ version_number: versionNumber, note }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to roll back workflow');
    }
    return resp.json();
  }

  async deleteWorkflowVersion(workflowId, versionNumber) {
    const resp = await this._fetch(`${this.baseURL}/${workflowId}/versions/${versionNumber}`, {
      method: 'DELETE',
      headers: this._headers(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to delete version');
    }
    return resp.json();
  }

  // ── Approval Queue ────────────────────────────────
  async listPendingApprovals() {
    const resp = await this._fetch(`${this.baseURL}/approvals`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to list approvals');
    return resp.json();
  }

  async listAllApprovals(skip = 0, limit = 50) {
    const resp = await this._fetch(
      `${this.baseURL}/approvals/all?skip=${skip}&limit=${limit}`,
      { method: 'GET', headers: this._headers() }
    );
    if (!resp.ok) throw new Error('Failed to list approvals');
    return resp.json();
  }

  // ── Connections ───────────────────────────────────
  async listConnections(type = null) {
    const qs = type ? `?type=${type}` : '';
    const resp = await this._fetch(`${this.baseURL}/connections${qs}`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to list connections');
    return resp.json();
  }

  async getConnection(connectionId) {
    const resp = await this._fetch(`${this.baseURL}/connections/${connectionId}`, {
      method: 'GET',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Connection not found');
    return resp.json();
  }

  async createConnection(data) {
    const resp = await this._fetch(`${this.baseURL}/connections`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify(data),
    });
    if (!resp.ok) throw await buildApiError(resp, 'Failed to create connection');
    return resp.json();
  }

  async updateConnection(connectionId, data) {
    const resp = await this._fetch(`${this.baseURL}/connections/${connectionId}`, {
      method: 'PUT',
      headers: this._headers(),
      body: JSON.stringify(data),
    });
    if (!resp.ok) throw await buildApiError(resp, 'Failed to update connection');
    return resp.json();
  }

  async deleteConnection(connectionId) {
    const resp = await this._fetch(`${this.baseURL}/connections/${connectionId}`, {
      method: 'DELETE',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to delete connection');
    return resp.json();
  }

  async testConnection(connectionId, env = 'test') {
    const resp = await this._fetch(
      `${this.baseURL}/connections/${connectionId}/test?env=${env}`,
      { method: 'POST', headers: this._headers() },
      15000
    );
    if (!resp.ok) throw new Error('Connection test failed');
    return resp.json();
  }

  async testDraftConnection(type, config) {
    const resp = await this._fetch(
      `${this.baseURL}/connections/test-draft`,
      {
        method: 'POST',
        headers: this._headers(),
        body: JSON.stringify({ type, config }),
      },
      15000
    );
    if (!resp.ok) throw new Error('Connection test failed');
    return resp.json();
  }

  // ── AI Workflow Generation ────────────────────────
  async generateWorkflow(prompt, conversation = []) {
    const resp = await this._fetch(`${this.baseURL}/generate-workflow`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ prompt, conversation }),
    }, 900000); // 15min — reasoning model (deepseek-v4-pro) can exceed 2min on larger workflows;
                // 120s aborted results the backend had already produced + billed.
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to generate workflow');
    }
    return resp.json();
  }

  async refineWorkflow(workflow, prompt, conversation = [], { returnDiff = true } = {}) {
    const resp = await this._fetch(`${this.baseURL}/generate-workflow/refine`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ prompt, workflow, conversation, return_diff: returnDiff }),
    }, 900000); // 15min — match generateWorkflow; reasoning model can exceed 2min.
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to refine workflow');
    }
    return resp.json();
  }

  // Per-node AI edit (Phase 4). Returns only the updated node + the
  // validation errors that specifically apply to it.
  async editNodeWithAI(workflow, nodeId, prompt, conversation = []) {
    const resp = await this._fetch(`${this.baseURL}/edit-node`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ prompt, workflow, node_id: nodeId, conversation }),
    }, 120000);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to edit node');
    }
    return resp.json();
  }

  // Agentic AI Assistant (streaming). One endpoint replaces the
  // generate/refine/edit-node routing: the model decides what to do from the
  // prompt and streams Server-Sent Events. Handlers:
  //   onStatus(text), onOperation(op), onValidation(v), onDone(evt), onError(msg)
  // We do NOT route through _fetch (its 30s abort would kill the stream); we
  // read resp.body directly and the backend heartbeat keeps it alive.
  async aiChatStream(body, handlers = {}) {
    const { onStatus, onOperation, onValidation, onRunResult, onDone, onError } = handlers;
    let resp;
    try {
      resp = await authService.authenticatedFetch(`${this.baseURL}/ai-chat`, {
        method: 'POST',
        headers: this._headers(),
        body: JSON.stringify(body),
      });
    } catch (err) {
      onError?.(err?.message || 'Failed to reach the assistant');
      return;
    }
    if (!resp.ok) {
      const e = await buildApiError(resp, 'Assistant request failed');
      onError?.(e.message);
      return;
    }

    const dispatch = (evt) => {
      switch (evt && evt.type) {
        case 'status': onStatus?.(evt.text); break;
        case 'operation': onOperation?.(evt.operation); break;
        case 'validation': onValidation?.(evt.validation); break;
        case 'run_result': onRunResult?.(evt.run); break;
        case 'done': onDone?.(evt); break;
        case 'error': onError?.(evt.message); break;
        default: break;
      }
    };
    const parseFrame = (frame) => {
      // A frame is the text between blank lines; ignore SSE comments (": …").
      const dataLine = frame.split('\n').find((l) => l.startsWith('data:'));
      if (!dataLine) return;
      const payload = dataLine.slice(5).trim();
      if (!payload) return;
      try { dispatch(JSON.parse(payload)); } catch { /* skip malformed frame */ }
    };

    // Preferred path: incremental read of the response stream.
    if (resp.body && typeof resp.body.getReader === 'function') {
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            parseFrame(buffer.slice(0, idx));
            buffer = buffer.slice(idx + 2);
          }
        }
        if (buffer.trim()) parseFrame(buffer);
      } catch (err) {
        onError?.(err?.message || 'Stream interrupted');
      }
      return;
    }

    // Fallback (no ReadableStream): the whole body arrives at once.
    try {
      const textBody = await resp.text();
      textBody.split('\n\n').forEach((f) => f.trim() && parseFrame(f));
    } catch (err) {
      onError?.(err?.message || 'Failed to read assistant response');
    }
  }

  // Starter prompts shown in the AI chat on an empty canvas (Phase 3).
  // Replaces the separate Templates view.
  async getStarterPrompts() {
    const resp = await this._fetch(`${this.baseURL}/starter-prompts`, {
      method: 'GET',
      headers: this._headers(),
    }, 10000);
    if (!resp.ok) return { prompts: [] };
    return resp.json();
  }

  // ── User Templates ────────────────────────────────
  async saveAsTemplate(data) {
    const resp = await this._fetch(`${this.baseURL}/user-templates`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to save template');
    }
    return resp.json();
  }

  async deleteUserTemplate(templateId) {
    const resp = await this._fetch(`${this.baseURL}/user-templates/${templateId}`, {
      method: 'DELETE',
      headers: this._headers(),
    });
    if (!resp.ok) throw new Error('Failed to delete template');
    return resp.json();
  }

  // ── Automation control (kill switches + per-workflow schedule) ──────────
  async listWorkflowAutomation() {
    const resp = await this._fetch(`${WORKFLOW_API_BASE}/api/admin/workflow-automation`);
    if (!resp.ok) throw await buildApiError(resp, 'Failed to load workflow automation');
    return resp.json();
  }
  // Start/stop schedule or change cron for ONE workflow.
  async setWorkflowSchedule(workflowId, patch) {
    const resp = await this._fetch(`${this.baseURL}/${encodeURIComponent(workflowId)}/schedule`, {
      method: 'PATCH', headers: this._headers(), body: JSON.stringify(patch || {}),
    });
    if (!resp.ok) throw await buildApiError(resp, 'Failed to update schedule');
    return resp.json();
  }
  async listWorkflowHalt() {
    const resp = await this._fetch(`${WORKFLOW_API_BASE}/api/admin/workflow-halt`);
    if (!resp.ok) throw await buildApiError(resp, 'Failed to load workflow halt status');
    return resp.json();
  }
  // Stop-all / resume-all (global) or org/dept halt for the workflow engine.
  async setWorkflowHalt({ scopeType, scopeId = null, enabled, reason = '' }) {
    const resp = await this._fetch(`${WORKFLOW_API_BASE}/api/admin/workflow-halt`, {
      method: 'POST', headers: this._headers(),
      body: JSON.stringify({ scope_type: scopeType, scope_id: scopeId, enabled, reason }),
    });
    if (!resp.ok) throw await buildApiError(resp, 'Failed to set workflow halt');
    return resp.json();
  }
}

export default new WorkflowService();
