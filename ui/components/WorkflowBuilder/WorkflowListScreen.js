// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * WorkflowListScreen.js — Grid view of user's saved workflows with CRUD actions
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  View, Text, TouchableOpacity, FlatList, StyleSheet, ActivityIndicator, Alert, Platform, TextInput,

} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import { parseApiTime } from './executionUi';

export default function WorkflowListScreen({ theme, onClose, onOpenWorkflow, onCreateNew, onShowTemplates, onShowApprovals, onShowConnections, onShowMaintenance, onShowControl, onShowRuns, onShowGuide }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  // How many workflows to fetch per page. 30 divides evenly into the
  // 3-column grid so pages don't leave a ragged last row.
  const PAGE_SIZE = 30;

  const [workflows, setWorkflows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [deploying, setDeploying] = useState(null); // workflow_id currently deploying
  const [forbidden, setForbidden] = useState(false);

  // Total workflows matching the current filters (from the API). null when
  // the API didn't report it — we then fall back to a page-size heuristic.
  const [total, setTotal] = useState(null);
  const [hasMore, setHasMore] = useState(false);

  // searchInput is the controlled text; search is the debounced value that
  // actually drives fetches. Search runs server-side so it spans ALL
  // workflows, not just the ones already loaded.
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  // Monotonic request id. Every fetch tags itself; only the latest tag may
  // commit results. Guards against a slow earlier response (old search term
  // or toggle state) overwriting a newer one.
  const reqIdRef = useRef(0);

  // Debounce the search box → `search`. Resetting `search` re-runs the
  // first-page effect below (pagination resets to page 1 on every change).
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadFirstPage = useCallback(async () => {
    const myReq = ++reqIdRef.current;
    try {
      setLoading(true);
      setError(null);
      setForbidden(false);
      const data = await WorkflowService.listWorkflows({
        skip: 0, limit: PAGE_SIZE, search,
      });
      if (myReq !== reqIdRef.current) return; // a newer request superseded us
      const incoming = data.workflows || [];
      setWorkflows(incoming);
      const t = typeof data.total === 'number' ? data.total : null;
      setTotal(t);
      setHasMore(t != null ? incoming.length < t : (data.has_more ?? incoming.length === PAGE_SIZE));
    } catch (err) {
      if (myReq !== reqIdRef.current) return;
      const msg = String(err && err.message || err);
      if (/403/.test(msg) || /workflow access/i.test(msg)) {
        setForbidden(true);
      } else {
        setError(msg);
      }
      setWorkflows([]);
      setTotal(null);
      setHasMore(false);
    } finally {
      if (myReq === reqIdRef.current) setLoading(false);
    }
  }, [search]);

  // Append the next page. Dedupes by workflow_id so a row can never appear
  // twice (e.g. if the underlying list shifted between pages).
  const handleLoadMore = useCallback(async () => {
    if (loadingMore || loading || !hasMore) return;
    const myReq = reqIdRef.current; // tied to the current first-page context
    const skip = workflows.length;
    setLoadingMore(true);
    setError(null);
    try {
      const data = await WorkflowService.listWorkflows({
        skip, limit: PAGE_SIZE, search,
      });
      if (myReq !== reqIdRef.current) return; // filters changed mid-flight
      const incoming = data.workflows || [];
      setWorkflows((prev) => {
        const seen = new Set(prev.map((w) => w.workflow_id));
        const merged = [...prev, ...incoming.filter((w) => !seen.has(w.workflow_id))];
        const t = typeof data.total === 'number' ? data.total : total;
        setHasMore(t != null ? merged.length < t : (data.has_more ?? incoming.length === PAGE_SIZE));
        return merged;
      });
      if (typeof data.total === 'number') setTotal(data.total);
    } catch (err) {
      if (myReq !== reqIdRef.current) return;
      // Non-destructive: keep what's already loaded, just surface the error.
      setError(String(err && err.message || err));
    } finally {
      if (myReq === reqIdRef.current) setLoadingMore(false);
    }
  }, [loadingMore, loading, hasMore, workflows.length, search, total]);

  useEffect(() => {
    loadFirstPage();
  }, [loadFirstPage]);

  const handleDelete = async (wfId, name) => {
    const confirmed = Platform.OS === 'web'
      ? window.confirm(`Delete "${name}"?`)
      : await new Promise((res) => Alert.alert('Delete', `Delete "${name}"?`, [
          { text: 'Cancel', onPress: () => res(false) },
          { text: 'Delete', style: 'destructive', onPress: () => res(true) },
        ]));
    if (!confirmed) return;
    try {
      await WorkflowService.deleteWorkflow(wfId);
      setWorkflows((prev) => prev.filter((w) => w.workflow_id !== wfId));
      // Keep the count label honest after an optimistic local removal.
      setTotal((t) => (typeof t === 'number' ? Math.max(0, t - 1) : t));
    } catch (err) {
      alert('Delete failed: ' + err.message);
    }
  };

  const handleDuplicate = async (wfId) => {
    try {
      await WorkflowService.duplicateWorkflow(wfId);
      loadFirstPage();
    } catch (err) {
      alert('Duplicate failed: ' + err.message);
    }
  };

  const handleToggleDeploy = async (wf) => {
    const isDeployed = wf.status === 'deployed';
    setDeploying(wf.workflow_id);
    try {
      if (isDeployed) {
        await WorkflowService.undeployWorkflow(wf.workflow_id);
      } else {
        await WorkflowService.deployWorkflow(wf.workflow_id);
      }
      loadFirstPage();
    } catch (err) {
      alert(`${isDeployed ? 'Undeploy' : 'Deploy'} failed: ${err.message}`);
    } finally {
      setDeploying(null);
    }
  };

  const formatDate = (iso) => {
    if (!iso) return '';
    // Offset-less UTC read as local time shifts the instant by the viewer's
    // offset, which for a date-only display lands on the wrong DAY near midnight.
    const t = parseApiTime(iso);
    if (Number.isNaN(t)) return '';
    return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const renderItem = ({ item }) => {
    const isDeployed = item.status === 'deployed';
    return (
    <TouchableOpacity
      style={[styles.card, { backgroundColor: cardBg, borderColor: isDeployed ? '#22c55e' : border }]}
      onPress={() => onOpenWorkflow(item.workflow_id)}
      activeOpacity={0.7}
    >
      <View style={styles.cardHeader}>
        <Ionicons name="git-network-outline" size={22} color="#6366f1" />
        <View style={{ flex: 1 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <Text style={[styles.cardTitle, { color: text }]} numberOfLines={1}>
              {item.name || 'Untitled'}
            </Text>
            <View style={[styles.statusBadge, { backgroundColor: isDeployed ? '#dcfce7' : (isDark ? '#334155' : '#f1f5f9') }]}>
              <View style={[styles.statusDot, { backgroundColor: isDeployed ? '#22c55e' : '#94a3b8' }]} />
              <Text style={[styles.statusBadgeText, { color: isDeployed ? '#16a34a' : muted }]}>
                {isDeployed ? 'Live' : 'Draft'}
              </Text>
            </View>
          </View>
          <Text style={[styles.cardDate, { color: muted }]}>
            Updated {formatDate(item.updated_at)} · v{item.version || 1}
          </Text>
        </View>
      </View>

      {item.description ? (
        <Text style={[styles.cardDesc, { color: muted }]} numberOfLines={2}>
          {item.description}
        </Text>
      ) : null}

      <View style={styles.cardMeta}>
        <View style={styles.metaChip}>
          <Ionicons name="cube-outline" size={12} color={muted} />
          <Text style={[styles.metaText, { color: muted }]}>{item.node_count || 0} nodes</Text>
        </View>
        <View style={styles.metaChip}>
          <Ionicons name="arrow-forward-outline" size={12} color={muted} />
          <Text style={[styles.metaText, { color: muted }]}>{item.edge_count || 0} edges</Text>
        </View>
      </View>

      <View style={[styles.cardActions, { borderTopColor: border }]}>
        <TouchableOpacity
          style={[styles.deployBtn, { backgroundColor: isDeployed ? '#fef2f2' : '#f0fdf4' }]}
          onPress={(e) => { e.stopPropagation(); handleToggleDeploy(item); }}
          disabled={deploying === item.workflow_id}
        >
          {deploying === item.workflow_id ? (
            <ActivityIndicator size={14} color={muted} />
          ) : (
            <>
              <Ionicons
                name={isDeployed ? 'stop-circle-outline' : 'rocket-outline'}
                size={14}
                color={isDeployed ? '#ef4444' : '#22c55e'}
              />
              <Text style={[styles.deployBtnText, { color: isDeployed ? '#ef4444' : '#22c55e' }]}>
                {isDeployed ? 'Undeploy' : 'Deploy'}
              </Text>
            </>
          )}
        </TouchableOpacity>
        <View style={{ flex: 1 }} />
        {/* Per-workflow run history entry point (labeled for discoverability). */}
        {onShowRuns && (
          <TouchableOpacity
            style={styles.historyBtn}
            onPress={(e) => { e.stopPropagation(); onShowRuns(item.workflow_id); }}
            accessibilityRole="button"
            accessibilityLabel={`View run history for ${item.name || 'workflow'}`}
            testID={`wf-history-${item.workflow_id}`}
            // @ts-ignore web-only tooltip
            title="View run history"
          >
            <Ionicons name="time-outline" size={15} color={muted} />
            <Text style={[styles.historyBtnText, { color: muted }]}>History</Text>
          </TouchableOpacity>
        )}
        <TouchableOpacity style={styles.actionBtn} onPress={() => handleDuplicate(item.workflow_id)}>
          <Ionicons name="copy-outline" size={16} color={muted} />
        </TouchableOpacity>
        <TouchableOpacity style={styles.actionBtn} onPress={() => handleDelete(item.workflow_id, item.name)}>
          <Ionicons name="trash-outline" size={16} color="#ef4444" />
        </TouchableOpacity>
      </View>
    </TouchableOpacity>
    );
  };

  // Full-screen spinner only on the very first load. Once anything is on
  // screen (including a prior result set), refetches from search/toggle keep
  // the header + search box mounted so focus isn't lost mid-typing.
  if (loading && workflows.length === 0 && !searchInput && !error && !forbidden) {
    return (
      <View style={[styles.container, styles.center, { backgroundColor: bg }]}>
        <ActivityIndicator size="large" color="#6366f1" />
      </View>
    );
  }

  const isSearching = search.length > 0;

  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {/* Header */}
      <View style={styles.header}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
          {onClose && (
            <TouchableOpacity onPress={onClose} style={{ padding: 6 }}>
              <Ionicons name="close" size={22} color={text} />
            </TouchableOpacity>
          )}
          <View>
            <Text style={[styles.heading, { color: text }]}>Workflows</Text>
            <Text style={[styles.subhead, { color: muted }]}>
              {total != null
                ? `Showing ${workflows.length} of ${total} workflow${total !== 1 ? 's' : ''}`
                : `Showing ${workflows.length} workflow${workflows.length !== 1 ? 's' : ''}`}
            </Text>
          </View>
        </View>
        <View style={{ flexDirection: 'row', gap: 10 }}>
          {onShowGuide && (
            <TouchableOpacity style={[styles.createBtn, { backgroundColor: '#6366f1' }]} onPress={onShowGuide}>
              <Ionicons name="book-outline" size={18} color="#fff" />
              <Text style={styles.createBtnText}>Guide</Text>
            </TouchableOpacity>
          )}
          {onShowConnections && (
            <TouchableOpacity style={[styles.createBtn, { backgroundColor: '#06b6d4' }]} onPress={onShowConnections}>
              <Ionicons name="key-outline" size={18} color="#fff" />
              <Text style={styles.createBtnText}>Connections</Text>
            </TouchableOpacity>
          )}
          {onShowApprovals && (
            <TouchableOpacity style={[styles.createBtn, { backgroundColor: '#f59e0b' }]} onPress={onShowApprovals}>
              <Ionicons name="shield-checkmark-outline" size={18} color="#fff" />
              <Text style={styles.createBtnText}>Approvals</Text>
            </TouchableOpacity>
          )}
          {onShowTemplates && (
            <TouchableOpacity style={[styles.createBtn, { backgroundColor: '#8b5cf6' }]} onPress={onShowTemplates}>
              <Ionicons name="albums-outline" size={18} color="#fff" />
              <Text style={styles.createBtnText}>Templates</Text>
            </TouchableOpacity>
          )}
          {onShowMaintenance && (
            <TouchableOpacity style={[styles.createBtn, { backgroundColor: '#64748b' }]} onPress={onShowMaintenance}>
              <Ionicons name="construct-outline" size={18} color="#fff" />
              <Text style={styles.createBtnText}>Maintenance</Text>
            </TouchableOpacity>
          )}
          {/* Red on purpose: halting every workflow is the highest-stakes
              control in the product, so it stays visible rather than buried. */}
          {onShowControl && (
            <TouchableOpacity style={[styles.createBtn, { backgroundColor: '#dc2626' }]} onPress={onShowControl}>
              <Ionicons name="stop-circle-outline" size={18} color="#fff" />
              <Text style={styles.createBtnText}>Automation Control</Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity style={styles.createBtn} onPress={onCreateNew}>
            <Ionicons name="add" size={18} color="#fff" />
            <Text style={styles.createBtnText}>New Workflow</Text>
          </TouchableOpacity>
        </View>
      </View>

      {/* Search runs server-side (name/description) so it reaches every
          workflow, not just the loaded page. */}
      <View style={[styles.filterBar, { borderColor: border }]}>
        <View style={[styles.searchBox, { borderColor: border, backgroundColor: cardBg }]}>
          <Ionicons name="search-outline" size={16} color={muted} />
          <TextInput
            style={[styles.searchInput, { color: text }]}
            value={searchInput}
            onChangeText={setSearchInput}
            placeholder="Search workflows by name or description"
            placeholderTextColor={muted}
            testID="wf-search-input"
            returnKeyType="search"
          />
          {searchInput.length > 0 && (
            <TouchableOpacity onPress={() => setSearchInput('')} testID="wf-search-clear">
              <Ionicons name="close-circle" size={16} color={muted} />
            </TouchableOpacity>
          )}
        </View>
      </View>

      {forbidden && (
        <View style={styles.emptyState}>
          <Ionicons name="lock-closed-outline" size={48} color={muted} />
          <Text style={[styles.emptyTitle, { color: text }]}>
            Workflows are IT-only
          </Text>
          <Text style={[styles.emptyDesc, { color: muted }]}>
            Workflow automation is restricted to users with an IT role
            (IT-workflow, IT department admin, or org admin). Ask your
            org admin to grant access.
          </Text>
        </View>
      )}

      {!forbidden && error && (
        <View style={styles.errorBox}>
          <Ionicons name="alert-circle" size={16} color="#ef4444" />
          <Text style={styles.errorText}>{error}</Text>
          <TouchableOpacity onPress={loadFirstPage}>
            <Text style={styles.retryText}>Retry</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Body spinner while a search/toggle reload is in flight and there's
          nothing yet to show under it. */}
      {!forbidden && loading && workflows.length === 0 && !error && (
        <View style={[styles.center, { flex: 1 }]}>
          <ActivityIndicator size="large" color="#6366f1" />
        </View>
      )}

      {/* Search returned nothing — distinct from the first-run empty state. */}
      {!forbidden && !loading && workflows.length === 0 && !error && isSearching && (
        <View style={styles.emptyState}>
          <Ionicons name="search-outline" size={48} color={muted} />
          <Text style={[styles.emptyTitle, { color: text }]}>No matching workflows</Text>
          <Text style={[styles.emptyDesc, { color: muted }]}>
            Nothing matches "{search}". Try a different name or description.
          </Text>
          <TouchableOpacity style={styles.createBtn} onPress={() => setSearchInput('')}>
            <Ionicons name="close" size={18} color="#fff" />
            <Text style={styles.createBtnText}>Clear search</Text>
          </TouchableOpacity>
        </View>
      )}

      {!forbidden && !loading && workflows.length === 0 && !error && !isSearching && (
        <View style={styles.emptyState}>
          <Ionicons name="git-network-outline" size={48} color={muted} />
          <Text style={[styles.emptyTitle, { color: text }]}>
            No workflows yet
          </Text>
          <Text style={[styles.emptyDesc, { color: muted }]}>
            Create your first workflow to automate processes for your org.
          </Text>
          <TouchableOpacity style={styles.createBtn} onPress={onCreateNew}>
            <Ionicons name="add" size={18} color="#fff" />
            <Text style={styles.createBtnText}>Create Workflow</Text>
          </TouchableOpacity>
        </View>
      )}

      {!forbidden && workflows.length > 0 && (
        <FlatList
          data={workflows}
          renderItem={renderItem}
          keyExtractor={(item) => item.workflow_id}
          numColumns={3}
          columnWrapperStyle={styles.row}
          contentContainerStyle={styles.list}
          showsVerticalScrollIndicator={false}
          ListFooterComponent={
            loadingMore ? (
              <View style={styles.footer}>
                <ActivityIndicator size="small" color="#6366f1" />
              </View>
            ) : hasMore ? (
              <View style={styles.footer}>
                <TouchableOpacity
                  style={[styles.loadMoreBtn, { borderColor: border }]}
                  onPress={handleLoadMore}
                  testID="wf-load-more"
                >
                  <Ionicons name="chevron-down" size={16} color="#6366f1" />
                  <Text style={styles.loadMoreText}>Load More</Text>
                </TouchableOpacity>
              </View>
            ) : null
          }
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 24 },
  center: { justifyContent: 'center', alignItems: 'center' },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 12,
  },
  filterBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 16,
    borderBottomWidth: 1,
    paddingBottom: 12,
    marginBottom: 16,
  },
  searchBox: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    maxWidth: 420,
    borderWidth: 1,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: Platform.OS === 'web' ? 8 : 6,
  },
  searchInput: {
    flex: 1,
    fontSize: 14,
    // RN-web focus ring is noisy; drop it for a cleaner inline field.
    ...(Platform.OS === 'web' ? { outlineStyle: 'none' } : {}),
  },
  footer: { paddingVertical: 20, alignItems: 'center' },
  loadMoreBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    borderWidth: 1,
    borderRadius: 10,
    paddingHorizontal: 20,
    paddingVertical: 10,
  },
  loadMoreText: { color: '#6366f1', fontWeight: '600', fontSize: 14 },
  heading: { fontSize: 24, fontWeight: '700' },
  subhead: { fontSize: 13, marginTop: 2 },
  createBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    backgroundColor: '#6366f1',
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderRadius: 10,
  },
  createBtnText: { color: '#fff', fontWeight: '600', fontSize: 14 },
  list: { paddingBottom: 40 },
  row: { gap: 16, marginBottom: 16 },
  card: {
    flex: 1,
    maxWidth: '32%',
    borderWidth: 1,
    borderRadius: 14,
    padding: 16,
  },
  cardHeader: { flexDirection: 'row', alignItems: 'center', gap: 10, marginBottom: 8 },
  cardTitle: { fontSize: 15, fontWeight: '700' },
  cardDate: { fontSize: 11, marginTop: 1 },
  cardDesc: { fontSize: 12, marginBottom: 10, lineHeight: 17 },
  cardMeta: { flexDirection: 'row', gap: 12, marginBottom: 12 },
  metaChip: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  metaText: { fontSize: 11 },
  cardActions: { flexDirection: 'row', alignItems: 'center', gap: 8, borderTopWidth: 1, paddingTop: 10 },
  actionBtn: { padding: 4 },
  historyBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 8,
  },
  historyBtnText: { fontSize: 12, fontWeight: '600' },
  statusBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 10,
  },
  statusDot: { width: 6, height: 6, borderRadius: 3 },
  statusBadgeText: { fontSize: 10, fontWeight: '600' },
  deployBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 8,
  },
  deployBtnText: { fontSize: 12, fontWeight: '600' },
  errorBox: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    padding: 12,
    backgroundColor: '#fef2f2',
    borderRadius: 10,
    marginBottom: 16,
  },
  errorText: { flex: 1, color: '#ef4444', fontSize: 13 },
  retryText: { color: '#3b82f6', fontWeight: '600', fontSize: 13 },
  emptyState: { flex: 1, justifyContent: 'center', alignItems: 'center', gap: 12 },
  emptyTitle: { fontSize: 18, fontWeight: '700' },
  emptyDesc: { fontSize: 14, textAlign: 'center', maxWidth: 340, lineHeight: 20 },
});
