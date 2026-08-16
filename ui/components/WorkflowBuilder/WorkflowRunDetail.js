/**
 * WorkflowRunDetail.js — Full-page detail for a single workflow execution.
 *
 * Read-only in phase 1: shows a summary card, a node timeline, trigger input /
 * variables, approval info (with a link to the Approval Queue) and a Raw JSON
 * fallback. Live-polls while the run is non-terminal.
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  View, Text, TouchableOpacity, ScrollView, StyleSheet, ActivityIndicator,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import Markdown from 'react-native-markdown-display';
import WorkflowService from '../../services/WorkflowService';
import authService from '../../services/authService';
import { WORKFLOW_API_BASE } from '../../config/config';
import {
  statusMeta, envMeta, isEnvMismatch, isTerminal,
  formatDuration, formatTimestamp, triggerLabel,
  extractPrimaryText, jsonEqual, isEmptyValue,
} from './executionUi';

const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'input', label: 'Trigger Input' },
  { key: 'variables', label: 'Variables' },
  { key: 'raw', label: 'Raw JSON' },
];

export default function WorkflowRunDetail({ executionId, workflowId, theme, onBack, onShowApprovals }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const codeBg = isDark ? '#0f172a' : '#f1f5f9';

  const [execution, setExecution] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [tab, setTab] = useState('overview');
  const [isDeployed, setIsDeployed] = useState(false);
  const [workflowName, setWorkflowName] = useState('');
  const [rawOpen, setRawOpen] = useState({}); // nodeId -> bool (per-node Raw JSON toggle)
  const [cancelling, setCancelling] = useState(false);     // cancel request in flight
  const [cancelRequested, setCancelRequested] = useState(false); // awaiting terminal
  const pollRef = useRef(null);
  const errorCountRef = useRef(0);
  const MAX_POLL_ERRORS = 10;

  const fetchExecution = useCallback(async () => {
    try {
      // Status endpoint is Redis-backed (fast) for live progress; fall back to
      // the full Mongo doc, and always load the full doc once terminal so the
      // node timeline / downloads are complete.
      let data;
      try {
        data = await WorkflowService.getExecutionStatus(executionId);
      } catch {
        data = await WorkflowService.getExecution(executionId);
      }
      if (isTerminal(data.status) && !data.node_results) {
        try { data = await WorkflowService.getExecution(executionId); } catch { /* keep status */ }
      }
      setExecution(data);
      setLoading(false);
      errorCountRef.current = 0;
      if (isTerminal(data.status)) clearInterval(pollRef.current);
    } catch (err) {
      setLoading(false);
      errorCountRef.current += 1;
      if (errorCountRef.current === 1) setError(err?.message || 'Failed to load execution');
      if (errorCountRef.current >= MAX_POLL_ERRORS) clearInterval(pollRef.current);
    }
  }, [executionId]);

  useEffect(() => {
    if (!executionId) { setLoading(false); setError('No execution selected'); return; }
    setCancelRequested(false); // reset per-execution
    fetchExecution();
    pollRef.current = setInterval(fetchExecution, 2000);
    return () => clearInterval(pollRef.current);
  }, [executionId, fetchExecution]);

  // Request cancellation. Cooperative: the run stops at its next node boundary
  // (or immediately if paused for approval). Polling reflects the new status.
  const handleCancel = useCallback(async () => {
    if (typeof window !== 'undefined' && !window.confirm(
      'Cancel this run? Any in-progress node finishes, but no further nodes will start. This cannot be undone.'
    )) return;
    setCancelling(true);
    try {
      await WorkflowService.cancelExecution(executionId);
      setCancelRequested(true);
      await fetchExecution();
    } catch (err) {
      if (typeof window !== 'undefined') {
        window.alert('Cancel failed: ' + (err?.message || 'unknown error'));
      }
    } finally {
      setCancelling(false);
    }
  }, [executionId, fetchExecution]);

  // Resolve the parent workflow's deploy state for the env-mismatch warning.
  // Stored environment remains the source of truth; deploy state only flags
  // a mismatch. Best-effort — failure simply suppresses the warning.
  useEffect(() => {
    const wfId = workflowId || execution?.workflow_id;
    if (!wfId) return;
    let cancelled = false;
    (async () => {
      try {
        const wf = await WorkflowService.getWorkflow(wfId);
        if (!cancelled) {
          setIsDeployed(String(wf?.status || '').toLowerCase() === 'deployed');
          if (wf?.name) setWorkflowName(wf.name);
        }
      } catch { /* ignore — no mismatch flag / name */ }
    })();
    return () => { cancelled = true; };
  }, [workflowId, execution?.workflow_id]);

  const sm = statusMeta(execution?.status);
  const em = envMeta(execution?.environment);
  const mismatch = isEnvMismatch(execution?.environment, isDeployed);

  const downloadFile = async (filename) => {
    try {
      const url = `${WORKFLOW_API_BASE}/api/workflows/executions/${executionId}/download/${encodeURIComponent(filename)}`;
      const resp = await authService.authenticatedFetch(url);
      if (!resp.ok) throw new Error(`Download failed: ${resp.status}`);
      const blob = await resp.blob();
      if (typeof window !== 'undefined') {
        const blobUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(blobUrl);
      }
    } catch (err) {
      alert('Download failed: ' + err.message);
    }
  };

  const renderJsonBlock = (value, emptyLabel) => {
    const hasValue = value != null && !(typeof value === 'object' && Object.keys(value).length === 0);
    if (!hasValue) {
      return <Text style={[styles.muted, { color: muted }]}>{emptyLabel}</Text>;
    }
    return (
      <View style={[styles.codeBox, { backgroundColor: codeBg }]}>
        <Text style={[styles.codeText, { color: text }]} selectable>
          {typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
        </Text>
      </View>
    );
  };

  const renderTimeline = () => {
    const results = execution?.node_results || {};
    const statuses = execution?.node_statuses || {};
    const hasFull = Object.keys(results).length > 0;
    const entries = hasFull
      ? Object.entries(results)
      : Object.entries(statuses).map(([nid, status]) => [nid, { status }]);
    // Order by start time when available so the timeline reads top-to-bottom.
    entries.sort((a, b) => {
      const ta = Date.parse(a[1]?.started_at || '') || 0;
      const tb = Date.parse(b[1]?.started_at || '') || 0;
      return ta - tb;
    });

    if (entries.length === 0) {
      return <Text style={[styles.muted, { color: muted }]}>No node results yet.</Text>;
    }
    return entries.map(([nodeId, result]) => {
      const nm = statusMeta(result?.status);
      const meta = (result?.output_data && typeof result.output_data === 'object')
        ? (result.output_data.meta || {}) : {};
      const isCurrent = execution?.current_node === nodeId && !isTerminal(execution?.status);
      return (
        <View key={nodeId} style={[styles.nodeCard, { borderColor: isCurrent ? '#3b82f6' : border, backgroundColor: cardBg }]}>
          <View style={styles.nodeHeader}>
            <Ionicons name={nm.name} size={16} color={nm.color} />
            <Text style={[styles.nodeId, { color: text }]} numberOfLines={1}>{nodeId}</Text>
            {isCurrent && (
              <View style={styles.currentChip}>
                <ActivityIndicator size="small" color="#3b82f6" />
                <Text style={styles.currentText}>current</Text>
              </View>
            )}
            <View style={{ flex: 1 }} />
            {result?.duration_ms != null && (
              <Text style={[styles.nodeDuration, { color: muted }]}>{result.duration_ms}ms</Text>
            )}
          </View>

          {result?.error && (
            <Text style={styles.nodeError}>{result.error}</Text>
          )}
          {Array.isArray(result?.retry_errors) && result.retry_errors.length > 0 && (
            <Text style={[styles.muted, { color: muted, marginTop: 4 }]}>
              Retried {result.retry_count || result.retry_errors.length}×
            </Text>
          )}

          {result?.output_data != null && (() => {
            const primaryText = extractPrimaryText(result.output_data);
            const isObj = typeof result.output_data === 'object';
            const rawJson = isObj ? JSON.stringify(result.output_data, null, 2) : String(result.output_data);
            const showRaw = !primaryText || !!rawOpen[nodeId];
            return (
              <View style={{ marginTop: 8 }}>
                {primaryText ? (
                  <>
                    <Text style={[styles.outLabel, { color: muted }]}>Output</Text>
                    <View style={[styles.outputBox, { backgroundColor: codeBg, borderColor: border }]}>
                      <MarkdownView content={primaryText} text={text} muted={muted} link="#6366f1" codeBg={isDark ? '#020617' : '#e2e8f0'} />
                    </View>
                    <TouchableOpacity
                      style={styles.rawToggle}
                      onPress={() => setRawOpen((p) => ({ ...p, [nodeId]: !p[nodeId] }))}
                    >
                      <Ionicons name={rawOpen[nodeId] ? 'chevron-down' : 'chevron-forward'} size={13} color={muted} />
                      <Text style={[styles.rawToggleText, { color: muted }]}>
                        {rawOpen[nodeId] ? 'Hide raw JSON' : 'Show raw JSON'}
                      </Text>
                    </TouchableOpacity>
                  </>
                ) : (
                  <Text style={[styles.outLabel, { color: muted }]}>Raw output</Text>
                )}
                {showRaw && (
                  <View style={[styles.codeBox, { backgroundColor: codeBg, marginTop: 6 }]}>
                    <Text style={[styles.codeText, { color: muted }]} selectable>{rawJson}</Text>
                  </View>
                )}
              </View>
            );
          })()}

          {meta.download && meta.filename && (
            <TouchableOpacity style={styles.downloadBtn} onPress={() => downloadFile(meta.filename)}>
              <Ionicons name="download-outline" size={16} color="#fff" />
              <Text style={styles.downloadText}>Download {meta.filename}</Text>
            </TouchableOpacity>
          )}
        </View>
      );
    });
  };

  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {/* Header */}
      <View style={styles.header}>
        <TouchableOpacity onPress={onBack} style={{ marginRight: 12 }} accessibilityLabel="Back">
          <Ionicons name="arrow-back" size={22} color={text} />
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={[styles.heading, { color: text }]} numberOfLines={1}>
            Execution{workflowName ? ` · ${workflowName}` : ''}
          </Text>
          <Text style={[styles.execId, { color: muted }]} selectable numberOfLines={1}>
            {executionId}
          </Text>
        </View>
        {execution && !isTerminal(execution.status) && (
          <View style={styles.headerActions}>
            <ActivityIndicator size="small" color="#3b82f6" />
            {cancelRequested ? (
              <Text style={[styles.cancellingText, { color: muted }]}>Cancelling…</Text>
            ) : (
              <TouchableOpacity
                style={[styles.cancelBtn, cancelling && { opacity: 0.6 }]}
                onPress={handleCancel}
                disabled={cancelling}
                accessibilityLabel="Cancel run"
              >
                <Ionicons name="stop-circle-outline" size={16} color="#fff" />
                <Text style={styles.cancelBtnText}>{cancelling ? 'Cancelling…' : 'Cancel run'}</Text>
              </TouchableOpacity>
            )}
          </View>
        )}
      </View>

      {loading ? (
        <View style={styles.center}><ActivityIndicator size="large" color="#6366f1" /></View>
      ) : error && !execution ? (
        <View style={styles.center}>
          <Ionicons name="alert-circle-outline" size={44} color={muted} />
          <Text style={[styles.muted, { color: muted, marginTop: 8 }]}>{error}</Text>
        </View>
      ) : (
        <ScrollView contentContainerStyle={styles.body} showsVerticalScrollIndicator={false}>
          {/* Summary card */}
          <View style={[styles.summary, { backgroundColor: cardBg, borderColor: border }]}>
            <View style={styles.summaryTop}>
              <View style={[styles.statusPill, { backgroundColor: sm.color + '1a' }]}>
                <Ionicons name={sm.name} size={16} color={sm.color} />
                <Text style={[styles.statusPillText, { color: sm.color }]}>{sm.label}</Text>
              </View>
              <View style={[styles.envBadge, { backgroundColor: em.bg }]}>
                <Text style={[styles.envBadgeText, { color: em.color }]}>{em.label}</Text>
              </View>
              {mismatch && (
                <View style={styles.mismatchChip}>
                  <Ionicons name="warning-outline" size={12} color="#b45309" />
                  <Text style={styles.mismatchText}>env mismatch — deployed workflow ran in Test</Text>
                </View>
              )}
            </View>

            <View style={styles.kvGrid}>
              {workflowName ? <Kv label="Workflow" value={workflowName} muted={muted} text={text} /> : null}
              <Kv label="Trigger" value={triggerLabel(execution)} muted={muted} text={text} />
              <Kv label="Duration" value={formatDuration(execution?.duration_ms, execution?.started_at, execution?.completed_at)} muted={muted} text={text} />
              <Kv label="Started" value={formatTimestamp(execution?.started_at)} muted={muted} text={text} />
              <Kv label="Completed" value={formatTimestamp(execution?.completed_at)} muted={muted} text={text} />
              {execution?.current_node && !isTerminal(execution?.status) && (
                <Kv label="Current node" value={execution.current_node} muted={muted} text={text} />
              )}
            </View>

            {execution?.error && (
              <View style={styles.errorBox}>
                <Ionicons name="alert-circle" size={15} color="#ef4444" />
                <Text style={styles.errorText}>{execution.error}</Text>
              </View>
            )}

            {/* Approval info (read-only) */}
            {(execution?.approval_id || execution?.paused_at_node) && (
              <View style={[styles.approvalBox, { borderColor: border }]}>
                <View style={{ flex: 1 }}>
                  <Text style={[styles.approvalTitle, { color: text }]}>Paused for approval</Text>
                  <Text style={[styles.muted, { color: muted }]}>
                    Node: {execution.paused_at_node || 'unknown'}
                    {execution.approval_id ? ` · ${execution.approval_id}` : ''}
                  </Text>
                </View>
                {onShowApprovals && (
                  <TouchableOpacity style={styles.approvalLink} onPress={onShowApprovals}>
                    <Text style={styles.approvalLinkText}>Approval Queue</Text>
                    <Ionicons name="arrow-forward" size={14} color="#6366f1" />
                  </TouchableOpacity>
                )}
              </View>
            )}
          </View>

          {/* Tabs */}
          <View style={[styles.tabs, { borderBottomColor: border }]}>
            {TABS.map((t) => (
              <TouchableOpacity
                key={t.key}
                style={[styles.tab, tab === t.key && styles.tabActive]}
                onPress={() => setTab(t.key)}
              >
                <Text style={[styles.tabText, { color: tab === t.key ? '#6366f1' : muted }]}>
                  {t.label}
                </Text>
              </TouchableOpacity>
            ))}
          </View>

          {/* Tab content */}
          <View style={{ marginTop: 14 }}>
            {tab === 'overview' && renderTimeline()}
            {tab === 'input' && renderJsonBlock(execution?.trigger_data, 'No trigger input recorded.')}
            {tab === 'variables' && (
              <>
                {!isEmptyValue(execution?.variables)
                  && jsonEqual(execution?.variables, execution?.trigger_data) && (
                  <View style={[styles.noteBox, { borderColor: border }]}>
                    <Ionicons name="information-circle-outline" size={15} color={muted} />
                    <Text style={[styles.noteText, { color: muted }]}>
                      Variables were initialized from the trigger input (identical values).
                    </Text>
                  </View>
                )}
                {renderJsonBlock(execution?.variables, 'No variables recorded.')}
              </>
            )}
            {tab === 'raw' && renderJsonBlock(execution, 'No data.')}
          </View>
        </ScrollView>
      )}
    </View>
  );
}

function Kv({ label, value, muted, text }) {
  return (
    <View style={styles.kv}>
      <Text style={[styles.kvLabel, { color: muted }]}>{label}</Text>
      <Text style={[styles.kvValue, { color: text }]} numberOfLines={1}>{value}</Text>
    </View>
  );
}

// Renders LLM/agent output as readable markdown. Guards against the markdown
// parser throwing on odd content by falling back to plain text.
function MarkdownView({ content, text, muted, link, codeBg }) {
  const mdStyles = {
    body: { color: text, fontSize: 13, lineHeight: 19 },
    heading1: { color: text, fontSize: 18, fontWeight: '700', marginTop: 6, marginBottom: 4 },
    heading2: { color: text, fontSize: 16, fontWeight: '700', marginTop: 6, marginBottom: 4 },
    heading3: { color: text, fontSize: 14, fontWeight: '700', marginTop: 4, marginBottom: 2 },
    paragraph: { color: text, marginTop: 0, marginBottom: 8 },
    bullet_list: { marginBottom: 6 },
    ordered_list: { marginBottom: 6 },
    list_item: { color: text },
    strong: { fontWeight: '700' },
    link: { color: link },
    code_inline: { backgroundColor: codeBg, color: text, fontFamily: 'monospace' },
    code_block: { backgroundColor: codeBg, color: text, fontFamily: 'monospace', padding: 10, borderRadius: 6 },
    fence: { backgroundColor: codeBg, color: text, fontFamily: 'monospace', padding: 10, borderRadius: 6 },
    table: { borderColor: muted },
    hr: { backgroundColor: muted },
  };
  try {
    return <Markdown style={mdStyles}>{String(content)}</Markdown>;
  } catch {
    return <Text style={{ color: text, fontSize: 13, lineHeight: 19 }}>{String(content)}</Text>;
  }
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 24 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 24 },
  header: { flexDirection: 'row', alignItems: 'center', marginBottom: 16 },
  headerActions: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  cancelBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingVertical: 7, paddingHorizontal: 12,
    backgroundColor: '#ef4444', borderRadius: 8,
  },
  cancelBtnText: { color: '#fff', fontSize: 13, fontWeight: '700' },
  cancellingText: { fontSize: 13, fontWeight: '600', fontStyle: 'italic' },
  heading: { fontSize: 24, fontWeight: '700' },
  execId: { fontSize: 11, marginTop: 2, fontFamily: 'monospace' },
  body: { paddingBottom: 48 },
  summary: { borderWidth: 1, borderRadius: 14, padding: 16 },
  summaryTop: { flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  statusPill: {
    flexDirection: 'row', alignItems: 'center', gap: 5,
    paddingHorizontal: 10, paddingVertical: 4, borderRadius: 10,
  },
  statusPillText: { fontSize: 13, fontWeight: '700' },
  envBadge: { paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8 },
  envBadgeText: { fontSize: 11, fontWeight: '700' },
  mismatchChip: {
    flexDirection: 'row', alignItems: 'center', gap: 4,
    backgroundColor: '#fef3c7', paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8,
  },
  mismatchText: { fontSize: 10, fontWeight: '700', color: '#b45309' },
  kvGrid: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 14, gap: 14 },
  kv: { minWidth: 120 },
  kvLabel: { fontSize: 11, marginBottom: 2 },
  kvValue: { fontSize: 13, fontWeight: '600' },
  errorBox: {
    flexDirection: 'row', alignItems: 'flex-start', gap: 8,
    marginTop: 14, padding: 10, backgroundColor: '#fef2f2', borderRadius: 8,
  },
  errorText: { flex: 1, color: '#ef4444', fontSize: 12, lineHeight: 17 },
  approvalBox: {
    flexDirection: 'row', alignItems: 'center', gap: 10,
    marginTop: 14, padding: 12, borderWidth: 1, borderRadius: 10,
  },
  approvalTitle: { fontSize: 13, fontWeight: '700' },
  approvalLink: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  approvalLinkText: { color: '#6366f1', fontWeight: '700', fontSize: 13 },
  tabs: { flexDirection: 'row', borderBottomWidth: 1, marginTop: 18 },
  tab: { paddingVertical: 10, paddingHorizontal: 14, marginBottom: -1 },
  tabActive: { borderBottomWidth: 2, borderBottomColor: '#6366f1' },
  tabText: { fontSize: 13, fontWeight: '600' },
  muted: { fontSize: 13, fontStyle: 'italic' },
  codeBox: { padding: 12, borderRadius: 8 },
  codeText: { fontSize: 12, fontFamily: 'monospace', lineHeight: 17 },
  outLabel: { fontSize: 11, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4 },
  outputBox: { padding: 12, borderRadius: 8, borderWidth: 1 },
  rawToggle: { flexDirection: 'row', alignItems: 'center', gap: 4, marginTop: 8 },
  rawToggleText: { fontSize: 12, fontWeight: '600' },
  noteBox: {
    flexDirection: 'row', alignItems: 'center', gap: 8,
    padding: 10, borderWidth: 1, borderRadius: 8, marginBottom: 12,
  },
  noteText: { fontSize: 12, flex: 1 },
  nodeCard: { borderWidth: 1, borderRadius: 12, padding: 12, marginBottom: 10 },
  nodeHeader: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  nodeId: { fontSize: 13, fontWeight: '600', fontFamily: 'monospace', maxWidth: '50%' },
  nodeDuration: { fontSize: 11 },
  currentChip: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  currentText: { fontSize: 10, fontWeight: '700', color: '#3b82f6' },
  nodeError: { color: '#ef4444', fontSize: 12, marginTop: 6 },
  downloadBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 6, alignSelf: 'flex-start',
    marginTop: 10, paddingVertical: 8, paddingHorizontal: 12,
    backgroundColor: '#06b6d4', borderRadius: 8,
  },
  downloadText: { color: '#fff', fontSize: 12, fontWeight: '600' },
});
