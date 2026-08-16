/**
 * ApprovalQueue.js — Dashboard for pending workflow approvals
 *
 * Shows all approvals waiting on the current user, with Approve / Reject actions
 * and an optional audit log of resolved approvals.
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, TouchableOpacity, FlatList, StyleSheet, ActivityIndicator, ScrollView,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import { parseApiTime } from './executionUi';

const STATUS_COLORS = {
  pending: { bg: '#fef3c7', text: '#d97706', icon: 'time-outline' },
  approved: { bg: '#dcfce7', text: '#16a34a', icon: 'checkmark-circle' },
  rejected: { bg: '#fef2f2', text: '#ef4444', icon: 'close-circle' },
  timed_out: { bg: '#f1f5f9', text: '#64748b', icon: 'alarm-outline' },
};

export default function ApprovalQueue({ theme, onBack, deepLinkExecutionId }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  const [tab, setTab] = useState('pending');          // 'pending' | 'all'
  const [pending, setPending] = useState([]);
  const [all, setAll] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actioning, setActioning] = useState(null);   // approval_id being acted on

  const loadApprovals = useCallback(async () => {
    try {
      const [p, a] = await Promise.all([
        WorkflowService.listPendingApprovals(),
        WorkflowService.listAllApprovals(0, 100),
      ]);
      setPending(p || []);
      setAll(a || []);
    } catch (err) {
      console.error('Failed to load approvals:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadApprovals();
  }, [loadApprovals]);

  // If deep-linked to a specific execution, highlight it
  useEffect(() => {
    if (deepLinkExecutionId && pending.length > 0) {
      const match = pending.find((a) => a.execution_id === deepLinkExecutionId);
      if (match) setTab('pending');
    }
  }, [deepLinkExecutionId, pending]);

  const handleApprove = async (approval) => {
    setActioning(approval.approval_id);
    try {
      await WorkflowService.approveExecution(approval.execution_id);
      loadApprovals();
    } catch (err) {
      alert('Approve failed: ' + err.message);
    } finally {
      setActioning(null);
    }
  };

  const handleReject = async (approval) => {
    setActioning(approval.approval_id);
    try {
      await WorkflowService.rejectExecution(approval.execution_id);
      loadApprovals();
    } catch (err) {
      alert('Reject failed: ' + err.message);
    } finally {
      setActioning(null);
    }
  };

  const formatDate = (iso) => {
    if (!iso) return '';
    const t = parseApiTime(iso);
    if (Number.isNaN(t)) return '';
    return new Date(t).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  };

  const timeRemaining = (created, timeoutHours) => {
    if (!timeoutHours) return null;
    // `new Date(created)` read the API's offset-less UTC as local time, so the
    // deadline was wrong by the viewer's UTC offset — far enough east and a
    // freshly-created approval already reads "Expired".
    const createdMs = parseApiTime(created);
    if (Number.isNaN(createdMs)) return null;
    const remaining = createdMs + timeoutHours * 3600000 - Date.now();
    if (remaining <= 0) return 'Expired';
    const hours = Math.floor(remaining / 3600000);
    const mins = Math.floor((remaining % 3600000) / 60000);
    return `${hours}h ${mins}m remaining`;
  };

  const renderPendingItem = ({ item }) => {
    const isHighlighted = deepLinkExecutionId === item.execution_id;
    const remaining = timeRemaining(item.created_at, item.timeout_hours);
    return (
      <View style={[
        styles.card,
        { backgroundColor: cardBg, borderColor: isHighlighted ? '#f59e0b' : border },
        isHighlighted && { borderWidth: 2 },
      ]}>
        <View style={styles.cardHeader}>
          <Ionicons name="shield-checkmark-outline" size={22} color="#f59e0b" />
          <View style={{ flex: 1 }}>
            <Text style={[styles.cardTitle, { color: text }]} numberOfLines={1}>
              {item.workflow_name || 'Untitled Workflow'}
            </Text>
            <Text style={[styles.cardSub, { color: muted }]}>
              Node: {item.node_label || item.node_id} · {formatDate(item.created_at)}
            </Text>
          </View>
          {remaining && (
            <View style={[styles.timeChip, { backgroundColor: remaining === 'Expired' ? '#fef2f2' : '#fef3c7' }]}>
              <Ionicons name="alarm-outline" size={12} color={remaining === 'Expired' ? '#ef4444' : '#d97706'} />
              <Text style={[styles.timeText, { color: remaining === 'Expired' ? '#ef4444' : '#d97706' }]}>
                {remaining}
              </Text>
            </View>
          )}
        </View>

        {item.message && (
          <Text style={[styles.message, { color: text }]}>{item.message}</Text>
        )}

        {item.data_preview && (
          <View style={[styles.dataPreview, { backgroundColor: isDark ? '#0f172a' : '#f8fafc' }]}>
            <Text style={[styles.dataPreviewText, { color: muted }]} numberOfLines={4}>
              {typeof item.data_preview === 'object'
                ? JSON.stringify(item.data_preview, null, 2)
                : String(item.data_preview)}
            </Text>
          </View>
        )}

        <View style={styles.actionRow}>
          <TouchableOpacity
            style={[styles.approveBtn, actioning === item.approval_id && { opacity: 0.5 }]}
            onPress={() => handleApprove(item)}
            disabled={actioning === item.approval_id}
          >
            {actioning === item.approval_id ? (
              <ActivityIndicator size={14} color="#fff" />
            ) : (
              <>
                <Ionicons name="checkmark" size={16} color="#fff" />
                <Text style={styles.actionBtnText}>Approve</Text>
              </>
            )}
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.rejectBtn, actioning === item.approval_id && { opacity: 0.5 }]}
            onPress={() => handleReject(item)}
            disabled={actioning === item.approval_id}
          >
            <Ionicons name="close" size={16} color="#fff" />
            <Text style={styles.actionBtnText}>Reject</Text>
          </TouchableOpacity>
        </View>
      </View>
    );
  };

  const renderAllItem = ({ item }) => {
    const sc = STATUS_COLORS[item.resolution] || STATUS_COLORS.pending;
    return (
      <View style={[styles.card, { backgroundColor: cardBg, borderColor: border }]}>
        <View style={styles.cardHeader}>
          <Ionicons name={sc.icon} size={18} color={sc.text} />
          <View style={{ flex: 1 }}>
            <Text style={[styles.cardTitle, { color: text }]} numberOfLines={1}>
              {item.workflow_name || 'Untitled Workflow'}
            </Text>
            <Text style={[styles.cardSub, { color: muted }]}>
              {item.node_label || item.node_id} · {formatDate(item.created_at)}
            </Text>
          </View>
          <View style={[styles.resolutionBadge, { backgroundColor: sc.bg }]}>
            <Text style={[styles.resolutionText, { color: sc.text }]}>
              {(item.resolution || 'pending').toUpperCase()}
            </Text>
          </View>
        </View>
        {item.resolved_at && (
          <Text style={[styles.cardSub, { color: muted, marginTop: 4 }]}>
            Resolved {formatDate(item.resolved_at)} by {item.resolved_by || 'system'}
          </Text>
        )}
      </View>
    );
  };

  if (loading) {
    return (
      <View style={[styles.container, styles.center, { backgroundColor: bg }]}>
        <ActivityIndicator size="large" color="#f59e0b" />
      </View>
    );
  }

  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {/* Header */}
      <View style={styles.header}>
        <TouchableOpacity onPress={onBack} style={{ marginRight: 12 }}>
          <Ionicons name="arrow-back" size={22} color={text} />
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={[styles.heading, { color: text }]}>Approval Queue</Text>
          <Text style={[styles.subhead, { color: muted }]}>
            {pending.length} pending approval{pending.length !== 1 ? 's' : ''}
          </Text>
        </View>
        <TouchableOpacity onPress={loadApprovals}>
          <Ionicons name="refresh-outline" size={22} color={muted} />
        </TouchableOpacity>
      </View>

      {/* Tabs */}
      <View style={[styles.tabs, { borderBottomColor: border }]}>
        <TouchableOpacity
          style={[styles.tab, tab === 'pending' && styles.tabActive]}
          onPress={() => setTab('pending')}
        >
          <Text style={[styles.tabText, tab === 'pending' ? styles.tabTextActive : { color: muted }]}>
            Pending ({pending.length})
          </Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[styles.tab, tab === 'all' && styles.tabActive]}
          onPress={() => setTab('all')}
        >
          <Text style={[styles.tabText, tab === 'all' ? styles.tabTextActive : { color: muted }]}>
            All ({all.length})
          </Text>
        </TouchableOpacity>
      </View>

      {/* Content */}
      {tab === 'pending' ? (
        pending.length === 0 ? (
          <View style={styles.emptyState}>
            <Ionicons name="checkmark-done-circle-outline" size={48} color={muted} />
            <Text style={[styles.emptyTitle, { color: text }]}>All clear</Text>
            <Text style={[styles.emptyDesc, { color: muted }]}>
              No workflows are waiting for your approval.
            </Text>
          </View>
        ) : (
          <FlatList
            data={pending}
            renderItem={renderPendingItem}
            keyExtractor={(item) => item.approval_id}
            contentContainerStyle={styles.listContent}
            showsVerticalScrollIndicator={false}
          />
        )
      ) : (
        all.length === 0 ? (
          <View style={styles.emptyState}>
            <Ionicons name="document-text-outline" size={48} color={muted} />
            <Text style={[styles.emptyTitle, { color: text }]}>No approvals yet</Text>
          </View>
        ) : (
          <FlatList
            data={all}
            renderItem={renderAllItem}
            keyExtractor={(item) => item.approval_id}
            contentContainerStyle={styles.listContent}
            showsVerticalScrollIndicator={false}
          />
        )
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 24 },
  center: { justifyContent: 'center', alignItems: 'center' },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 16,
  },
  heading: { fontSize: 24, fontWeight: '700' },
  subhead: { fontSize: 13, marginTop: 2 },
  tabs: {
    flexDirection: 'row',
    borderBottomWidth: 1,
    marginBottom: 16,
  },
  tab: { paddingVertical: 10, paddingHorizontal: 16, marginBottom: -1 },
  tabActive: { borderBottomWidth: 2, borderBottomColor: '#f59e0b' },
  tabText: { fontSize: 14, fontWeight: '600' },
  tabTextActive: { color: '#f59e0b' },
  listContent: { paddingBottom: 40, gap: 12 },
  card: {
    borderWidth: 1,
    borderRadius: 14,
    padding: 16,
  },
  cardHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
  },
  cardTitle: { fontSize: 15, fontWeight: '700' },
  cardSub: { fontSize: 11, marginTop: 1 },
  message: { fontSize: 13, marginTop: 10, lineHeight: 19 },
  dataPreview: {
    marginTop: 10,
    padding: 10,
    borderRadius: 8,
  },
  dataPreviewText: { fontSize: 11, fontFamily: 'monospace', lineHeight: 16 },
  timeChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
  },
  timeText: { fontSize: 10, fontWeight: '600' },
  actionRow: {
    flexDirection: 'row',
    gap: 10,
    marginTop: 14,
  },
  approveBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#22c55e',
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
  },
  rejectBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#ef4444',
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
  },
  actionBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  resolutionBadge: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
  },
  resolutionText: { fontSize: 10, fontWeight: '700' },
  emptyState: { flex: 1, justifyContent: 'center', alignItems: 'center', gap: 12 },
  emptyTitle: { fontSize: 18, fontWeight: '700' },
  emptyDesc: { fontSize: 14, textAlign: 'center', maxWidth: 340 },
});
