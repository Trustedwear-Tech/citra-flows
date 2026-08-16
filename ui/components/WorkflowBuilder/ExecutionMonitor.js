/**
 * ExecutionMonitor.js — Real-time execution progress and results viewer
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  View, Text, TouchableOpacity, ScrollView, StyleSheet, ActivityIndicator,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import authService from '../../services/authService';
import { WORKFLOW_API_BASE } from '../../config/config';
import { STATUS_META as STATUS_ICONS } from './executionUi';

export default function ExecutionMonitor({ executionId, theme, onClose }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  const [execution, setExecution] = useState(null);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  const pollRef = useRef(null);
  const errorCountRef = useRef(0);
  const TERMINAL_STATUSES = ['completed', 'failed', 'timed_out', 'cancelled'];
  const MAX_POLL_ERRORS = 10;

  const fetchStatus = useCallback(async () => {
    try {
      // Use status endpoint (Redis-backed) for real-time progress during execution,
      // fall back to full execution endpoint (MongoDB) for terminal states with node_results
      let data;
      try {
        data = await WorkflowService.getExecutionStatus(executionId);
      } catch {
        // Fall back to full execution endpoint
        data = await WorkflowService.getExecution(executionId);
      }
      // Once terminal, fetch full results from MongoDB for download buttons etc.
      if (TERMINAL_STATUSES.includes(data.status) && !data.node_results) {
        try {
          data = await WorkflowService.getExecution(executionId);
        } catch { /* keep status data */ }
      }
      setExecution(data);
      setLoading(false);
      errorCountRef.current = 0; // reset on success
      // Stop polling when execution is done
      if (TERMINAL_STATUSES.includes(data.status)) {
        clearInterval(pollRef.current);
      }
    } catch (err) {
      console.error('Failed to fetch execution:', err);
      setLoading(false);
      errorCountRef.current += 1;
      if (errorCountRef.current >= MAX_POLL_ERRORS) {
        console.warn('Polling stopped after', MAX_POLL_ERRORS, 'consecutive errors');
        clearInterval(pollRef.current);
      }
    }
  }, [executionId]);

  useEffect(() => {
    fetchStatus();
    pollRef.current = setInterval(fetchStatus, 2000);
    return () => clearInterval(pollRef.current);
  }, [fetchStatus]);

  const handleApprove = async () => {
    setApproving(true);
    try {
      await WorkflowService.approveExecution(executionId);
      fetchStatus();
    } catch (err) {
      alert('Approve failed: ' + err.message);
    } finally {
      setApproving(false);
    }
  };

  const handleReject = async () => {
    setApproving(true);
    try {
      await WorkflowService.rejectExecution(executionId);
      fetchStatus();
    } catch (err) {
      alert('Reject failed: ' + err.message);
    } finally {
      setApproving(false);
    }
  };

  const handleCancel = async () => {
    if (typeof window !== 'undefined' && !window.confirm(
      'Cancel this run? Any in-progress node finishes, but no further nodes will start. This cannot be undone.'
    )) return;
    setCancelling(true);
    try {
      await WorkflowService.cancelExecution(executionId);
      setCancelRequested(true);
      fetchStatus();
    } catch (err) {
      alert('Cancel failed: ' + (err?.message || 'unknown error'));
    } finally {
      setCancelling(false);
    }
  };

  const statusIcon = STATUS_ICONS[execution?.status] || STATUS_ICONS.pending;
  const canCancel = execution && !TERMINAL_STATUSES.includes(execution.status)
    && execution.status !== 'paused' && !cancelRequested;

  return (
    <View style={[styles.overlay, { backgroundColor: isDark ? 'rgba(0,0,0,.7)' : 'rgba(0,0,0,.4)' }]}>
      <View style={[styles.panel, { backgroundColor: bg }]}>
        {/* Header */}
        <View style={[styles.header, { borderBottomColor: border }]}>
          <Ionicons name={statusIcon.name} size={22} color={statusIcon.color} />
          <View style={{ flex: 1 }}>
            <Text style={[styles.title, { color: text }]}>Execution</Text>
            <Text style={[styles.executionId, { color: muted }]}>{executionId?.substring(0, 12)}…</Text>
          </View>
          <TouchableOpacity
            onPress={onClose}
            accessibilityRole="button"
            accessibilityLabel="Close execution monitor"
          >
            <Ionicons name="close" size={22} color={muted} />
          </TouchableOpacity>
        </View>

        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator size="large" color="#3b82f6" />
            <Text style={[styles.loadingText, { color: muted }]}>Loading execution…</Text>
          </View>
        ) : (
          <ScrollView style={styles.body} showsVerticalScrollIndicator={false}>
            {/* Overall status */}
            <View style={[styles.statusBar, { borderColor: statusIcon.color, backgroundColor: statusIcon.color + '10' }]}>
              <Ionicons name={statusIcon.name} size={18} color={statusIcon.color} />
              <Text style={[styles.statusText, { color: statusIcon.color }]}>
                {(execution?.status || 'unknown').toUpperCase()}
              </Text>
              {execution?.status === 'running' && (
                <ActivityIndicator size="small" color="#3b82f6" style={{ marginLeft: 8 }} />
              )}
              <View style={{ flex: 1 }} />
              {cancelRequested && !TERMINAL_STATUSES.includes(execution?.status) && (
                <Text style={[styles.cancellingText, { color: muted }]}>Cancelling…</Text>
              )}
              {canCancel && (
                <TouchableOpacity
                  style={[styles.cancelBtn, cancelling && { opacity: 0.5 }]}
                  onPress={handleCancel}
                  disabled={cancelling}
                  accessibilityRole="button"
                  accessibilityLabel="Cancel execution"
                >
                  <Ionicons name="stop-circle-outline" size={15} color="#fff" />
                  <Text style={styles.cancelBtnText}>{cancelling ? 'Cancelling…' : 'Cancel'}</Text>
                </TouchableOpacity>
              )}
            </View>

            {/* Paused — approval actions */}
            {execution?.status === 'paused' && (
              <View style={styles.approvalRow}>
                <Text style={[styles.approvalLabel, { color: text }]}>
                  Waiting for approval at: {execution.paused_at_node || 'unknown node'}
                </Text>
                <View style={styles.approvalBtns}>
                  <TouchableOpacity
                    style={[styles.approveBtn, approving && { opacity: 0.5 }]}
                    onPress={handleApprove}
                    disabled={approving}
                    accessibilityRole="button"
                    accessibilityLabel="Approve execution"
                  >
                    <Ionicons name="checkmark" size={16} color="#fff" />
                    <Text style={styles.approveBtnText}>Approve</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.rejectBtn, approving && { opacity: 0.5 }]}
                    onPress={handleReject}
                    disabled={approving}
                    accessibilityRole="button"
                    accessibilityLabel="Reject execution"
                  >
                    <Ionicons name="close" size={16} color="#fff" />
                    <Text style={styles.rejectBtnText}>Reject</Text>
                  </TouchableOpacity>
                </View>
              </View>
            )}

            {/* Error */}
            {execution?.error && (
              <View style={styles.errorBox}>
                <Ionicons name="alert-circle" size={16} color="#ef4444" />
                <Text style={styles.errorText}>{execution.error}</Text>
              </View>
            )}

            {/* Node results */}
            <Text style={[styles.sectionTitle, { color: text }]}>Node Results</Text>
            {/* Support both node_results (full endpoint) and node_statuses (status endpoint) */}
            {(() => {
              const results = execution?.node_results || {};
              const statuses = execution?.node_statuses || {};
              const hasFullResults = Object.keys(results).length > 0;
              const entries = hasFullResults
                ? Object.entries(results)
                : Object.entries(statuses).map(([nodeId, status]) => [nodeId, { status }]);
              if (entries.length === 0) {
                return <Text style={{ color: muted, fontSize: 13, marginBottom: 12 }}>No node results yet…</Text>;
              }
              return entries.map(([nodeId, result]) => {
              const nodeStatus = STATUS_ICONS[result.status] || STATUS_ICONS.pending;
              return (
                <View key={nodeId} style={[styles.nodeResult, { borderColor: border }]}>
                  <View style={styles.nodeResultHeader}>
                    <Ionicons name={nodeStatus.name} size={16} color={nodeStatus.color} />
                    <Text style={[styles.nodeResultId, { color: text }]}>{nodeId}</Text>
                    {result.duration_ms != null && (
                      <Text style={[styles.duration, { color: muted }]}>{result.duration_ms}ms</Text>
                    )}
                  </View>
                  {result.error && (
                    <Text style={styles.nodeError}>{result.error}</Text>
                  )}
                  {result.output_data && (
                    <View style={[styles.outputPreview, { backgroundColor: isDark ? '#0f172a' : '#f8fafc' }]}>
                      <Text style={[styles.outputText, { color: muted }]} numberOfLines={8}>
                        {typeof result.output_data === 'object'
                          ? JSON.stringify(result.output_data, null, 2)
                          : String(result.output_data)}
                      </Text>
                    </View>
                  )}
                  {/* Download button for file_download nodes */}
                  {result.output_data?.meta?.download && result.output_data?.meta?.filename && (
                    <TouchableOpacity
                      onPress={async () => {
                        const filename = result.output_data.meta.filename;
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
                          console.error('Download error:', err);
                          alert('Download failed: ' + err.message);
                        }
                      }}
                      style={{
                        flexDirection: 'row', alignItems: 'center', gap: 6,
                        marginTop: 8, paddingVertical: 8, paddingHorizontal: 12,
                        backgroundColor: '#06b6d4', borderRadius: 8, alignSelf: 'flex-start',
                      }}
                      accessibilityRole="button"
                      accessibilityLabel={`Download ${result.output_data.meta.filename}`}
                    >
                      <Ionicons name="download-outline" size={16} color="#fff" />
                      <Text style={{ color: '#fff', fontSize: 12, fontWeight: '600' }}>
                        Download {result.output_data.meta.filename}
                      </Text>
                    </TouchableOpacity>
                  )}
                </View>
              );
            });
            })()}
          </ScrollView>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  overlay: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  panel: { width: 520, maxHeight: '85%', borderRadius: 16, overflow: 'hidden', elevation: 8 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 16,
    borderBottomWidth: 1,
    gap: 10,
  },
  title: { fontSize: 16, fontWeight: '700' },
  executionId: { fontSize: 11, marginTop: 1, fontFamily: 'monospace' },
  center: { padding: 40, alignItems: 'center', gap: 12 },
  loadingText: { fontSize: 13 },
  body: { padding: 16 },
  statusBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    padding: 10,
    borderWidth: 1,
    borderRadius: 10,
    marginBottom: 16,
  },
  statusText: { fontSize: 13, fontWeight: '700', letterSpacing: 0.5 },
  approvalRow: { marginBottom: 16 },
  approvalLabel: { fontSize: 13, marginBottom: 8 },
  approvalBtns: { flexDirection: 'row', gap: 8 },
  approveBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#22c55e',
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 8,
  },
  approveBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  rejectBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#ef4444',
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 8,
  },
  rejectBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  cancelBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#ef4444',
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 8,
  },
  cancelBtnText: { color: '#fff', fontWeight: '700', fontSize: 12 },
  cancellingText: { fontSize: 12, fontWeight: '600', fontStyle: 'italic', marginRight: 8 },
  errorBox: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: 8,
    padding: 10,
    backgroundColor: '#fef2f2',
    borderRadius: 8,
    marginBottom: 16,
  },
  errorText: { flex: 1, color: '#ef4444', fontSize: 12 },
  sectionTitle: { fontSize: 14, fontWeight: '700', marginBottom: 10 },
  nodeResult: { borderWidth: 1, borderRadius: 10, padding: 10, marginBottom: 8 },
  nodeResultHeader: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  nodeResultId: { flex: 1, fontSize: 12, fontWeight: '600', fontFamily: 'monospace' },
  duration: { fontSize: 11 },
  nodeError: { color: '#ef4444', fontSize: 11, marginTop: 4 },
  outputPreview: { marginTop: 6, padding: 8, borderRadius: 6 },
  outputText: { fontSize: 11, fontFamily: 'monospace' },
  emptyText: { fontSize: 13, fontStyle: 'italic', textAlign: 'center', paddingVertical: 20 },
});
