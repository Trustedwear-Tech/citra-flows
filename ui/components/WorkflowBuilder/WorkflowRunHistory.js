// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * WorkflowRunHistory.js — Per-workflow execution history (Runs) list.
 *
 * Shows every run for a single workflow: status, stored environment (with a
 * deploy/test mismatch warning), trigger, timing, duration and error summary.
 * Tapping a row opens the execution detail page.
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  View, Text, TouchableOpacity, FlatList, StyleSheet, ActivityIndicator,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import {
  statusMeta, envMeta, isEnvMismatch, formatDuration, formatRelative,
  formatTimestamp, triggerLabel,
} from './executionUi';

const PAGE_SIZE = 20;

// Filter chips → backend status value (null = no filter).
const FILTERS = [
  { key: 'all', label: 'All', status: null },
  { key: 'running', label: 'Running', status: 'running' },
  { key: 'completed', label: 'Completed', status: 'completed' },
  { key: 'failed', label: 'Failed', status: 'failed' },
  { key: 'paused', label: 'Paused', status: 'paused' },
];

export default function WorkflowRunHistory({ workflowId, theme, onBack, onOpenExecution }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  const [workflow, setWorkflow] = useState(null);
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('all');
  const [hasMore, setHasMore] = useState(false);

  const activeStatus = FILTERS.find((f) => f.key === filter)?.status || null;

  // Monotonic request id. Every fetch tags itself; only the latest tag may
  // commit results. Guards against a slow earlier response (old filter, or a
  // load-more issued before a filter switch) overwriting a newer one.
  const reqIdRef = useRef(0);

  const load = useCallback(async (statusFilter, { append = false, skip = 0 } = {}) => {
    const myReq = ++reqIdRef.current;
    if (append) setLoadingMore(true);
    else { setLoading(true); setError(''); }
    try {
      const data = await WorkflowService.listExecutions(workflowId, {
        skip, limit: PAGE_SIZE, status: statusFilter || undefined,
      });
      if (myReq !== reqIdRef.current) return; // a newer request superseded us
      const items = data?.executions || [];
      if (data?.workflow) setWorkflow(data.workflow);
      setRuns((prev) => (append ? [...prev, ...items] : items));
      setHasMore(items.length === PAGE_SIZE);
    } catch (err) {
      if (myReq !== reqIdRef.current) return; // filter changed mid-flight
      if (!append) setRuns([]);
      setError(err?.message || 'Failed to load runs');
    } finally {
      if (myReq === reqIdRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [workflowId]);

  useEffect(() => {
    load(activeStatus, { append: false, skip: 0 });
  }, [load, activeStatus]);

  const handleLoadMore = () => {
    if (loadingMore || !hasMore) return;
    load(activeStatus, { append: true, skip: runs.length });
  };

  const isDeployed = Boolean(workflow?.is_deployed);

  const renderRow = ({ item }) => {
    const sm = statusMeta(item.status);
    const em = envMeta(item.environment);
    const mismatch = isEnvMismatch(item.environment, isDeployed);
    return (
      <TouchableOpacity
        style={[styles.card, { backgroundColor: cardBg, borderColor: border }]}
        onPress={() => onOpenExecution?.(item.execution_id)}
        activeOpacity={0.7}
        accessibilityRole="button"
        accessibilityLabel={`Open execution ${item.execution_id}`}
        testID={`run-row-${item.execution_id}`}
      >
        <View style={styles.cardHeader}>
          <Ionicons name={sm.name} size={20} color={sm.color} />
          <View style={[styles.statusBadge, { backgroundColor: sm.color + '1a' }]}>
            <Text style={[styles.statusBadgeText, { color: sm.color }]}>{sm.label}</Text>
          </View>

          {/* Environment badge (stored value) + mismatch marker */}
          <View style={[styles.envBadge, { backgroundColor: em.bg }]}>
            <Text style={[styles.envBadgeText, { color: em.color }]}>{em.label}</Text>
          </View>
          {mismatch && (
            <View style={styles.mismatchChip}>
              <Ionicons name="warning-outline" size={12} color="#b45309" />
              <Text style={styles.mismatchText}>env mismatch</Text>
            </View>
          )}

          <View style={{ flex: 1 }} />
          <Text style={[styles.relTime, { color: muted }]}>{formatRelative(item.started_at)}</Text>
          <Ionicons name="chevron-forward" size={16} color={muted} />
        </View>

        <View style={styles.metaRow}>
          <Text style={[styles.execId, { color: muted }]} numberOfLines={1}>
            {String(item.execution_id || '').substring(0, 8)}…
          </Text>
          <View style={styles.metaDot} />
          <Ionicons name="flash-outline" size={12} color={muted} />
          <Text style={[styles.metaText, { color: muted }]}>{triggerLabel(item)}</Text>
          <View style={styles.metaDot} />
          <Ionicons name="time-outline" size={12} color={muted} />
          <Text style={[styles.metaText, { color: muted }]}>
            {formatDuration(item.duration_ms, item.started_at, item.completed_at)}
          </Text>
        </View>

        <View style={styles.timeRow}>
          <Text style={[styles.timeLabel, { color: muted }]}>
            Started {formatTimestamp(item.started_at)}
          </Text>
          {item.completed_at && (
            <Text style={[styles.timeLabel, { color: muted }]}>
              · Completed {formatTimestamp(item.completed_at)}
            </Text>
          )}
        </View>

        {item.error && (
          <View style={styles.errorBox}>
            <Ionicons name="alert-circle" size={13} color="#ef4444" />
            <Text style={styles.errorText} numberOfLines={2}>{item.error}</Text>
          </View>
        )}

        <View style={styles.viewRow}>
          <Text style={styles.viewText}>View details</Text>
          <Ionicons name="arrow-forward" size={13} color="#6366f1" />
        </View>
      </TouchableOpacity>
    );
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
            Runs{workflow?.name ? ` · ${workflow.name}` : ''}
          </Text>
          <View style={styles.subRow}>
            <Text style={[styles.subhead, { color: muted }]}>Execution history</Text>
            <View style={[styles.deployBadge, { backgroundColor: isDeployed ? '#dcfce7' : (isDark ? '#334155' : '#e2e8f0') }]}>
              <View style={[styles.deployDot, { backgroundColor: isDeployed ? '#22c55e' : '#94a3b8' }]} />
              <Text style={[styles.deployText, { color: isDeployed ? '#16a34a' : muted }]}>
                {isDeployed ? 'Live (Production)' : 'Draft (Test)'}
              </Text>
            </View>
          </View>
        </View>
        <TouchableOpacity onPress={() => load(activeStatus, { append: false, skip: 0 })} accessibilityLabel="Refresh">
          <Ionicons name="refresh-outline" size={22} color={muted} />
        </TouchableOpacity>
      </View>

      {/* Filter chips */}
      <View style={styles.filterRow}>
        {FILTERS.map((f) => (
          <TouchableOpacity
            key={f.key}
            style={[
              styles.chip,
              { borderColor: border },
              filter === f.key && { backgroundColor: '#6366f1', borderColor: '#6366f1' },
            ]}
            onPress={() => setFilter(f.key)}
          >
            <Text style={[styles.chipText, { color: filter === f.key ? '#fff' : muted }]}>
              {f.label}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* Content */}
      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color="#6366f1" />
        </View>
      ) : error ? (
        <View style={styles.emptyState}>
          <Ionicons name="cloud-offline-outline" size={48} color={muted} />
          <Text style={[styles.emptyTitle, { color: text }]}>Couldn’t load runs</Text>
          <Text style={[styles.emptyDesc, { color: muted }]}>{error}</Text>
          <TouchableOpacity
            style={styles.retryBtn}
            onPress={() => load(activeStatus, { append: false, skip: 0 })}
          >
            <Text style={styles.retryText}>Retry</Text>
          </TouchableOpacity>
        </View>
      ) : runs.length === 0 ? (
        <View style={styles.emptyState}>
          <Ionicons name="albums-outline" size={48} color={muted} />
          <Text style={[styles.emptyTitle, { color: text }]}>No runs yet</Text>
          <Text style={[styles.emptyDesc, { color: muted }]}>
            {filter === 'all'
              ? 'This workflow has no execution history.'
              : `No ${filter} runs for this workflow.`}
          </Text>
        </View>
      ) : (
        <FlatList
          data={runs}
          renderItem={renderRow}
          keyExtractor={(item) => item.execution_id}
          contentContainerStyle={styles.listContent}
          showsVerticalScrollIndicator={false}
          onEndReachedThreshold={0.4}
          onEndReached={handleLoadMore}
          ListFooterComponent={
            hasMore ? (
              <TouchableOpacity
                style={[styles.loadMore, { borderColor: border }]}
                onPress={handleLoadMore}
                disabled={loadingMore}
              >
                {loadingMore ? (
                  <ActivityIndicator size="small" color={muted} />
                ) : (
                  <Text style={[styles.loadMoreText, { color: muted }]}>Load more</Text>
                )}
              </TouchableOpacity>
            ) : null
          }
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 24 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  header: { flexDirection: 'row', alignItems: 'center', marginBottom: 16 },
  heading: { fontSize: 24, fontWeight: '700' },
  subRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 3 },
  subhead: { fontSize: 13 },
  deployBadge: {
    flexDirection: 'row', alignItems: 'center', gap: 5,
    paddingHorizontal: 8, paddingVertical: 2, borderRadius: 10,
  },
  deployDot: { width: 7, height: 7, borderRadius: 4 },
  deployText: { fontSize: 11, fontWeight: '600' },
  filterRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 16 },
  chip: {
    paddingHorizontal: 14, paddingVertical: 6, borderRadius: 16, borderWidth: 1,
  },
  chipText: { fontSize: 13, fontWeight: '600' },
  listContent: { paddingBottom: 40, gap: 10 },
  card: { borderWidth: 1, borderRadius: 14, padding: 14 },
  cardHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  statusBadge: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 8 },
  statusBadgeText: { fontSize: 11, fontWeight: '700' },
  envBadge: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 8 },
  envBadgeText: { fontSize: 11, fontWeight: '700' },
  mismatchChip: {
    flexDirection: 'row', alignItems: 'center', gap: 3,
    backgroundColor: '#fef3c7', paddingHorizontal: 6, paddingVertical: 2, borderRadius: 8,
  },
  mismatchText: { fontSize: 10, fontWeight: '700', color: '#b45309' },
  relTime: { fontSize: 11 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 5, marginTop: 10 },
  execId: { fontSize: 11, fontFamily: 'monospace' },
  metaDot: { width: 3, height: 3, borderRadius: 2, backgroundColor: '#94a3b8' },
  metaText: { fontSize: 11 },
  timeRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 4, marginTop: 6 },
  timeLabel: { fontSize: 11 },
  errorBox: {
    flexDirection: 'row', alignItems: 'flex-start', gap: 6,
    marginTop: 10, padding: 8, backgroundColor: '#fef2f2', borderRadius: 8,
  },
  errorText: { flex: 1, color: '#ef4444', fontSize: 11, lineHeight: 15 },
  viewRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'flex-end', gap: 4, marginTop: 10 },
  viewText: { color: '#6366f1', fontSize: 12, fontWeight: '700' },
  loadMore: {
    marginTop: 12, paddingVertical: 12, borderRadius: 10, borderWidth: 1,
    alignItems: 'center',
  },
  loadMoreText: { fontSize: 13, fontWeight: '600' },
  emptyState: { flex: 1, justifyContent: 'center', alignItems: 'center', gap: 12, padding: 24 },
  emptyTitle: { fontSize: 18, fontWeight: '700' },
  emptyDesc: { fontSize: 14, textAlign: 'center', maxWidth: 340 },
  retryBtn: {
    marginTop: 4, paddingHorizontal: 18, paddingVertical: 9,
    backgroundColor: '#6366f1', borderRadius: 8,
  },
  retryText: { color: '#fff', fontWeight: '600', fontSize: 13 },
});
