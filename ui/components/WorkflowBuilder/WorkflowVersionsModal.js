/**
 * WorkflowVersionsModal.js — Deploy history + rollback.
 *
 * Each deploy (and each rollback of a LIVE workflow) appends an immutable
 * snapshot of the exact graph that went live, captured server-side in the
 * WorkflowVersions collection. This modal lists that lineage newest-first,
 * marks which version is running now, and lets an operator roll back to any
 * earlier version.
 *
 * Rollback is append-only on the server: it restores the chosen version's
 * graph onto the live workflow AND records a new snapshot, so history is
 * never rewritten. Because it re-deploys live logic, we require an explicit
 * typed confirmation step (note optional) before firing.
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, TouchableOpacity, Modal, ScrollView,
  StyleSheet, TextInput, ActivityIndicator,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import WorkflowDiffReview from './WorkflowDiffReview';
import { parseApiTime } from './executionUi';

function fmtTime(iso) {
  if (!iso) return '';
  try {
    // parseApiTime, not `new Date(iso)`: the API sends UTC with no offset, which
    // Date parses as local time and shifts every deploy timestamp.
    const d = new Date(parseApiTime(iso));
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleString();
  } catch (e) {
    return '';
  }
}

export default function WorkflowVersionsModal({
  visible, theme, workflowId, onClose, onRolledBack,
}) {
  const isDark = theme?.isDark;
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const rowBg = isDark ? '#0f172a' : '#f8fafc';

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [versions, setVersions] = useState([]);
  const [deployedVersion, setDeployedVersion] = useState(null);
  const [status, setStatus] = useState(null);
  const [maxVersions, setMaxVersions] = useState(3);

  // Inline rollback confirmation: which version_number is pending confirm.
  const [confirmFor, setConfirmFor] = useState(null);
  const [rollbackNote, setRollbackNote] = useState('');
  const [rollingBack, setRollingBack] = useState(false);
  // Inline delete confirmation: which version_number is pending delete.
  const [deleteFor, setDeleteFor] = useState(null);
  const [deleting, setDeleting] = useState(false);
  // Diffs loaded on demand so the operator reviews WHAT changes before
  // acting. Two baselines: 'current' (what a rollback to vN applies) and a
  // previous version number (what the vN deploy itself changed). Keyed
  // "vN@baseline".
  const [diffs, setDiffs] = useState({});          // "vN@against" -> diff | 'loading' | {error}
  const [compareFor, setCompareFor] = useState(null); // row with an open standalone compare
  const [compareAgainst, setCompareAgainst] = useState('current');

  const loadDiff = useCallback(async (versionNumber, against = 'current') => {
    const key = `${versionNumber}@${against}`;
    setDiffs((d) => ({ ...d, [key]: 'loading' }));
    try {
      const data = await WorkflowService.getWorkflowVersionDiff(workflowId, versionNumber, against);
      setDiffs((d) => ({ ...d, [key]: data.diff || {} }));
    } catch (err) {
      setDiffs((d) => ({ ...d, [key]: { error: err.message || 'Failed to load changes' } }));
    }
  }, [workflowId]);

  const load = useCallback(async () => {
    if (!workflowId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await WorkflowService.listWorkflowVersions(workflowId);
      setVersions(data.versions || []);
      setDeployedVersion(data.deployed_version ?? null);
      setStatus(data.status || null);
      if (data.max_versions) setMaxVersions(data.max_versions);
    } catch (err) {
      setError(err.message || 'Failed to load version history');
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    if (!visible) return;
    setConfirmFor(null);
    setRollbackNote('');
    setDeleteFor(null);
    setDiffs({});
    setCompareFor(null);
    load();
  }, [visible, load]);

  const doRollback = useCallback(async (versionNumber) => {
    setRollingBack(true);
    try {
      const result = await WorkflowService.rollbackWorkflow(workflowId, versionNumber, rollbackNote.trim());
      setConfirmFor(null);
      setRollbackNote('');
      // The rollback changed the CURRENT graph — every cached "vs current"
      // diff is now stale. Drop the cache so compares refetch.
      setDiffs({});
      setCompareFor(null);
      if (onRolledBack) onRolledBack(result);
      await load();
    } catch (err) {
      setError(err.message || 'Rollback failed');
    } finally {
      setRollingBack(false);
    }
  }, [workflowId, rollbackNote, onRolledBack, load]);

  const doDelete = useCallback(async (versionNumber) => {
    setDeleting(true);
    try {
      await WorkflowService.deleteWorkflowVersion(workflowId, versionNumber);
      setDeleteFor(null);
      await load();
    } catch (err) {
      setError(err.message || 'Delete failed');
    } finally {
      setDeleting(false);
    }
  }, [workflowId, load]);

  if (!visible) return null;

  const isLive = status === 'deployed';

  // Loading / error / review states for one version's diff against a baseline.
  const renderDiffPanel = (versionNumber, against = 'current') => {
    const d = diffs[`${versionNumber}@${against}`];
    if (d === 'loading' || d === undefined) {
      return (
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, paddingVertical: 6 }}>
          <ActivityIndicator size="small" color="#6366f1" />
          <Text style={{ color: muted, fontSize: 11 }}>Loading changes…</Text>
        </View>
      );
    }
    if (d && d.error) {
      return <Text style={{ color: '#ef4444', fontSize: 11, paddingVertical: 4 }}>{d.error}</Text>;
    }
    const total = (d.nodes_added || []).length + (d.nodes_removed || []).length
      + (d.nodes_updated || []).length + (d.edges_added || []).length
      + (d.edges_removed || []).length;
    if (!total) {
      return (
        <Text style={{ color: muted, fontSize: 11, fontStyle: 'italic', paddingVertical: 4 }}>
          {against === 'current'
            ? 'Identical to the current graph — rolling back changes nothing.'
            : `Identical to v${against} — this deploy changed nothing in the graph.`}
        </Text>
      );
    }
    return <WorkflowDiffReview diff={d} theme={theme} initiallyExpanded />;
  };

  // The deploy immediately before vN in the (pruned) lineage, if any.
  const prevVersionOf = (versionNumber) => {
    const nums = versions.map((v) => v.version_number).sort((a, b) => b - a);
    const idx = nums.indexOf(versionNumber);
    return idx >= 0 && idx + 1 < nums.length ? nums[idx + 1] : null;
  };

  return (
    <Modal visible transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={[styles.card, { backgroundColor: cardBg }]}>
          {/* Header */}
          <View style={styles.header}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
              <Ionicons name="git-branch-outline" size={20} color="#6366f1" />
              <Text style={[styles.title, { color: text }]}>Deploy History</Text>
            </View>
            <TouchableOpacity onPress={onClose} accessibilityRole="button" accessibilityLabel="Close deploy history">
              <Ionicons name="close" size={22} color={text} />
            </TouchableOpacity>
          </View>

          <Text style={[styles.subtitle, { color: muted }]}>
            Every deploy snapshots the exact graph that went live. Roll back to
            re-deploy an earlier version{isLive ? '' : ' (restores it into the draft — deploy to go live)'}.
            The last {maxVersions} are kept; the live version is never removed.
          </Text>

          {loading ? (
            <View style={styles.centered}>
              <ActivityIndicator size="small" color="#6366f1" />
              <Text style={[styles.mutedSmall, { color: muted, marginTop: 8 }]}>Loading history…</Text>
            </View>
          ) : error ? (
            <View style={styles.centered}>
              <Ionicons name="alert-circle-outline" size={28} color="#ef4444" />
              <Text style={{ color: '#ef4444', marginTop: 8, fontSize: 13, textAlign: 'center' }}>{error}</Text>
              <TouchableOpacity onPress={load} style={[styles.retryBtn, { borderColor: border }]}>
                <Text style={{ color: text, fontSize: 13, fontWeight: '600' }}>Retry</Text>
              </TouchableOpacity>
            </View>
          ) : versions.length === 0 ? (
            <View style={styles.centered}>
              <Ionicons name="time-outline" size={28} color={muted} />
              <Text style={[styles.mutedSmall, { color: muted, marginTop: 8, textAlign: 'center' }]}>
                No deploys yet. The first deploy creates v1.
              </Text>
            </View>
          ) : (
            <ScrollView style={{ maxHeight: 380 }} contentContainerStyle={{ paddingBottom: 4, gap: 8 }}>
              {versions.map((v) => {
                const live = v.version_number === deployedVersion && isLive;
                const confirming = confirmFor === v.version_number;
                const confirmingDelete = deleteFor === v.version_number;
                return (
                  <View
                    key={v.version_number}
                    style={[styles.row, { backgroundColor: rowBg, borderColor: live ? '#22c55e' : border }]}
                  >
                    <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, flexShrink: 1 }}>
                        <Text style={[styles.versionNum, { color: text }]}>v{v.version_number}</Text>
                        {live && (
                          <View style={styles.liveBadge}>
                            <View style={styles.liveDot} />
                            <Text style={styles.liveText}>LIVE</Text>
                          </View>
                        )}
                        {v.source === 'rollback' && (
                          <View style={[styles.tagBadge, { backgroundColor: isDark ? '#3b2f0b' : '#fef3c7' }]}>
                            <Text style={[styles.tagText, { color: isDark ? '#fbbf24' : '#92400e' }]}>
                              ↺ from v{v.restored_from_version}
                            </Text>
                          </View>
                        )}
                        <View style={[styles.tagBadge, { backgroundColor: isDark ? '#0b2f3b' : '#e0f2fe' }]}>
                          <Text style={[styles.tagText, { color: isDark ? '#38bdf8' : '#0369a1' }]}>
                            {v.run_environment}
                          </Text>
                        </View>
                      </View>
                      {!confirming && !confirmingDelete && (
                        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                          {live && (
                            <Text style={[styles.lockedNote, { color: muted }]}>kept while live</Text>
                          )}
                          <TouchableOpacity
                            onPress={() => {
                              const opening = compareFor !== v.version_number;
                              setCompareFor(opening ? v.version_number : null);
                              if (!opening) return;
                              // Live row: "what did this deploy change" (vs the
                              // previous deploy) is the useful default. Older
                              // rows default to "what would rolling back change".
                              const prev = prevVersionOf(v.version_number);
                              const against = live && prev != null ? String(prev) : 'current';
                              setCompareAgainst(against);
                              if (diffs[`${v.version_number}@${against}`] === undefined) {
                                loadDiff(v.version_number, against);
                              }
                            }}
                            style={[styles.iconBtn, { borderColor: border }]}
                            accessibilityRole="button"
                            accessibilityLabel={`Show what version ${v.version_number} changes`}
                          >
                            <Ionicons name="git-compare-outline" size={14} color="#6366f1" />
                          </TouchableOpacity>
                          {!live && (
                            <>
                              <TouchableOpacity
                                onPress={() => {
                                  setConfirmFor(v.version_number); setDeleteFor(null); setRollbackNote('');
                                  setCompareFor(null);
                                  if (diffs[`${v.version_number}@current`] === undefined) {
                                    loadDiff(v.version_number, 'current');
                                  }
                                }}
                                style={[styles.rollbackBtn, { borderColor: border }]}
                                accessibilityRole="button"
                                accessibilityLabel={`Roll back to version ${v.version_number}`}
                              >
                                <Ionicons name="arrow-undo-outline" size={14} color="#6366f1" />
                                <Text style={styles.rollbackBtnText}>Roll back</Text>
                              </TouchableOpacity>
                              <TouchableOpacity
                                onPress={() => { setDeleteFor(v.version_number); setConfirmFor(null); setCompareFor(null); }}
                                style={[styles.iconBtn, { borderColor: border }]}
                                accessibilityRole="button"
                                accessibilityLabel={`Delete version ${v.version_number}`}
                              >
                                <Ionicons name="trash-outline" size={14} color="#ef4444" />
                              </TouchableOpacity>
                            </>
                          )}
                        </View>
                      )}
                    </View>

                    <Text style={[styles.meta, { color: muted }]}>
                      {v.node_count} node{v.node_count === 1 ? '' : 's'} · {fmtTime(v.created_at)}
                      {v.deployed_by_email ? ` · ${v.deployed_by_email}` : ''}
                    </Text>
                    {!!v.note && (
                      <Text style={[styles.note, { color: text }]} numberOfLines={2}>“{v.note}”</Text>
                    )}

                    {compareFor === v.version_number && !confirming && (() => {
                      const prev = prevVersionOf(v.version_number);
                      const modes = [
                        { key: 'current', label: 'vs current graph' },
                        ...(prev != null ? [{ key: String(prev), label: `vs v${prev} (previous deploy)` }] : []),
                      ];
                      return (
                        <View style={[styles.confirmBox, { borderTopColor: border }]}>
                          {modes.length > 1 && (
                            <View style={{ flexDirection: 'row', gap: 6, marginBottom: 6 }}>
                              {modes.map((m) => {
                                const active = compareAgainst === m.key;
                                return (
                                  <TouchableOpacity
                                    key={m.key}
                                    onPress={() => {
                                      setCompareAgainst(m.key);
                                      if (diffs[`${v.version_number}@${m.key}`] === undefined) {
                                        loadDiff(v.version_number, m.key);
                                      }
                                    }}
                                    style={[styles.compareTab, {
                                      borderColor: active ? '#6366f1' : border,
                                      backgroundColor: active ? (isDark ? '#312e81' : '#eef2ff') : 'transparent',
                                    }]}
                                    accessibilityRole="button"
                                    accessibilityLabel={`Compare ${m.label}`}
                                  >
                                    <Text style={{ fontSize: 11, fontWeight: '600', color: active ? '#6366f1' : muted }}>
                                      {m.label}
                                    </Text>
                                  </TouchableOpacity>
                                );
                              })}
                            </View>
                          )}
                          <Text style={{ color: muted, fontSize: 11, marginBottom: 4 }}>
                            {compareAgainst === 'current'
                              ? `What rolling back to v${v.version_number} would change on the current graph:`
                              : `What the v${v.version_number} deploy changed compared to v${compareAgainst}:`}
                          </Text>
                          {renderDiffPanel(v.version_number, compareAgainst)}
                        </View>
                      );
                    })()}

                    {confirming && (
                      <View style={[styles.confirmBox, { borderTopColor: border }]}>
                        <Text style={{ color: text, fontSize: 12, fontWeight: '600', marginBottom: 6 }}>
                          {isLive
                            ? `Re-deploy v${v.version_number} to PRODUCTION now?`
                            : `Restore v${v.version_number} into the draft?`}
                        </Text>
                        <View style={{ marginBottom: 8 }}>
                          {renderDiffPanel(v.version_number)}
                        </View>
                        <TextInput
                          value={rollbackNote}
                          onChangeText={setRollbackNote}
                          placeholder="Reason (optional)"
                          placeholderTextColor={muted}
                          style={{
                            borderWidth: 1, borderColor: border, borderRadius: 8,
                            paddingHorizontal: 10, paddingVertical: 8, fontSize: 13,
                            color: text, backgroundColor: cardBg, marginBottom: 8,
                          }}
                        />
                        <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 8 }}>
                          <TouchableOpacity
                            onPress={() => { setConfirmFor(null); setRollbackNote(''); }}
                            style={styles.cancelBtn}
                            disabled={rollingBack}
                          >
                            <Text style={[styles.cancelBtnText, { color: muted }]}>Cancel</Text>
                          </TouchableOpacity>
                          <TouchableOpacity
                            onPress={() => doRollback(v.version_number)}
                            disabled={rollingBack}
                            style={[styles.confirmBtn, rollingBack && { opacity: 0.6 }]}
                            accessibilityRole="button"
                            accessibilityLabel={`Confirm rollback to version ${v.version_number}`}
                          >
                            {rollingBack ? (
                              <ActivityIndicator size="small" color="#fff" />
                            ) : (
                              <>
                                <Ionicons name="arrow-undo" size={14} color="#fff" />
                                <Text style={styles.confirmBtnText}>
                                  {isLive ? 'Re-deploy this version' : 'Restore'}
                                </Text>
                              </>
                            )}
                          </TouchableOpacity>
                        </View>
                      </View>
                    )}

                    {confirmingDelete && (
                      <View style={[styles.confirmBox, { borderTopColor: border }]}>
                        <Text style={{ color: text, fontSize: 12, fontWeight: '600', marginBottom: 8 }}>
                          Delete v{v.version_number} from history? This can't be undone.
                        </Text>
                        <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 8 }}>
                          <TouchableOpacity
                            onPress={() => setDeleteFor(null)}
                            style={styles.cancelBtn}
                            disabled={deleting}
                          >
                            <Text style={[styles.cancelBtnText, { color: muted }]}>Cancel</Text>
                          </TouchableOpacity>
                          <TouchableOpacity
                            onPress={() => doDelete(v.version_number)}
                            disabled={deleting}
                            style={[styles.deleteBtn, deleting && { opacity: 0.6 }]}
                            accessibilityRole="button"
                            accessibilityLabel={`Confirm delete version ${v.version_number}`}
                          >
                            {deleting ? (
                              <ActivityIndicator size="small" color="#fff" />
                            ) : (
                              <>
                                <Ionicons name="trash" size={14} color="#fff" />
                                <Text style={styles.confirmBtnText}>Delete</Text>
                              </>
                            )}
                          </TouchableOpacity>
                        </View>
                      </View>
                    )}
                  </View>
                );
              })}
            </ScrollView>
          )}
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.5)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  card: { width: 540, maxWidth: '92%', borderRadius: 14, padding: 20 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 6,
  },
  title: { fontSize: 17, fontWeight: '700' },
  subtitle: { fontSize: 12, lineHeight: 17, marginBottom: 14 },
  centered: { alignItems: 'center', justifyContent: 'center', paddingVertical: 36 },
  mutedSmall: { fontSize: 13 },
  retryBtn: { marginTop: 12, borderWidth: 1, borderRadius: 8, paddingHorizontal: 16, paddingVertical: 7 },
  row: { borderWidth: 1, borderRadius: 10, padding: 12 },
  versionNum: { fontSize: 15, fontWeight: '700' },
  liveBadge: {
    flexDirection: 'row', alignItems: 'center', gap: 4,
    backgroundColor: 'rgba(34,197,94,0.15)', borderRadius: 6,
    paddingHorizontal: 7, paddingVertical: 2,
  },
  liveDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: '#22c55e' },
  liveText: { color: '#16a34a', fontSize: 10, fontWeight: '800', letterSpacing: 0.5 },
  tagBadge: { borderRadius: 6, paddingHorizontal: 7, paddingVertical: 2 },
  tagText: { fontSize: 10, fontWeight: '700' },
  meta: { fontSize: 11, marginTop: 6 },
  note: { fontSize: 12, marginTop: 4, fontStyle: 'italic' },
  rollbackBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 4,
    borderWidth: 1, borderRadius: 8, paddingHorizontal: 10, paddingVertical: 6,
  },
  rollbackBtnText: { color: '#6366f1', fontSize: 12, fontWeight: '600' },
  iconBtn: {
    borderWidth: 1, borderRadius: 8, paddingHorizontal: 8, paddingVertical: 6,
    alignItems: 'center', justifyContent: 'center',
  },
  lockedNote: { fontSize: 11, fontStyle: 'italic' },
  compareTab: { borderWidth: 1, borderRadius: 6, paddingHorizontal: 8, paddingVertical: 4 },
  confirmBox: { marginTop: 10, borderTopWidth: 1, paddingTop: 10 },
  cancelBtn: { paddingHorizontal: 14, paddingVertical: 8 },
  cancelBtnText: { fontSize: 13, fontWeight: '600' },
  confirmBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    backgroundColor: '#6366f1', paddingHorizontal: 14, paddingVertical: 8, borderRadius: 8,
  },
  confirmBtnText: { color: '#fff', fontWeight: '700', fontSize: 13 },
  deleteBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    backgroundColor: '#dc2626', paddingHorizontal: 14, paddingVertical: 8, borderRadius: 8,
  },
});
