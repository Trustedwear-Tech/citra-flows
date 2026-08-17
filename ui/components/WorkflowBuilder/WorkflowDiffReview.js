// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * WorkflowDiffReview.js — human-readable review of a workflow diff.
 *
 * One component, two entry points: the AI chat renders it inside a proposal
 * bubble (review before "Apply"), and the versions modal renders it inside
 * the rollback confirmation (review before "Re-deploy"). Both feed it the
 * same server-computed diff shape from _diff_workflows:
 *
 *   nodes_added / nodes_removed / nodes_updated  — the applier patch
 *   nodes_changed_fields — {nodeId: [{path, before, after}]} field detail
 *   edges_added / edges_removed
 *   variables_added / variables_removed / variables_changed
 *
 * Purely presentational: no fetching, no apply logic. The caller owns the
 * action buttons so "the only path to applying is through seeing" holds at
 * the call site.
 */
import React, { useState } from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

const GREEN = '#16a34a';
const AMBER = '#d97706';
const RED = '#dc2626';

function fmtValue(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v.length ? v : '""';
  try {
    return JSON.stringify(v);
  } catch (e) {
    return String(v);
  }
}

function CountChip({ count, label, color, bg }) {
  if (!count) return null;
  return (
    <View style={[styles.chip, { backgroundColor: bg }]}>
      <Text style={[styles.chipText, { color }]}>{count} {label}</Text>
    </View>
  );
}

export default function WorkflowDiffReview({ diff, theme, initiallyExpanded = false }) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const isDark = theme?.isDark;
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const rowBg = isDark ? '#0f172a' : '#ffffff';
  const greenBg = isDark ? 'rgba(34,197,94,0.12)' : '#f0fdf4';
  const amberBg = isDark ? 'rgba(245,158,11,0.12)' : '#fffbeb';
  const redBg = isDark ? 'rgba(239,68,68,0.12)' : '#fef2f2';

  if (!diff) return null;

  const added = diff.nodes_added || [];
  const removed = diff.nodes_removed || [];
  const updated = diff.nodes_updated || [];
  const changedFields = diff.nodes_changed_fields || {};
  const edgesAdded = diff.edges_added || [];
  const edgesRemoved = diff.edges_removed || [];
  const varsAdded = Object.entries(diff.variables_added || {});
  const varsRemoved = Object.entries(diff.variables_removed || {});
  const varsChanged = diff.variables_changed || [];

  const total = added.length + removed.length + updated.length
    + edgesAdded.length + edgesRemoved.length
    + varsAdded.length + varsRemoved.length + varsChanged.length;
  if (!total) return null;

  return (
    <View style={[styles.container, { borderColor: border }]}>
      <TouchableOpacity
        style={styles.header}
        onPress={() => setExpanded(!expanded)}
        accessibilityRole="button"
        accessibilityLabel={expanded ? 'Hide change details' : 'Review changes'}
      >
        <Ionicons name="git-compare-outline" size={14} color={muted} />
        <Text style={[styles.headerText, { color: text }]}>
          Review {total} change{total === 1 ? '' : 's'}
        </Text>
        <View style={styles.chipRow}>
          <CountChip count={added.length} label="added" color={GREEN} bg={greenBg} />
          <CountChip count={updated.length} label="modified" color={AMBER} bg={amberBg} />
          <CountChip count={removed.length} label="removed" color={RED} bg={redBg} />
        </View>
        <Ionicons name={expanded ? 'chevron-up' : 'chevron-down'} size={14} color={muted} />
      </TouchableOpacity>

      {expanded && (
        <View style={styles.body}>
          {updated.map((n) => (
            <View key={`u-${n.id}`} style={[styles.entry, { backgroundColor: rowBg, borderColor: border }]}>
              <View style={[styles.entryHeader, { backgroundColor: amberBg }]}>
                <Ionicons name="pencil-outline" size={12} color={AMBER} />
                <Text style={[styles.entryTitle, { color: AMBER }]} numberOfLines={1}>
                  Modified · {n.label || n.id}
                </Text>
              </View>
              {(changedFields[n.id] || []).map((c, i) => (
                <View key={i} style={[styles.fieldRow, { borderTopColor: border }]}>
                  <Text style={[styles.fieldPath, { color: muted }]} numberOfLines={1}>{c.path}</Text>
                  <Text style={[styles.fieldValue, { color: RED }]} numberOfLines={3}>{fmtValue(c.before)}</Text>
                  <Ionicons name="arrow-forward" size={11} color={muted} style={{ marginHorizontal: 4 }} />
                  <Text style={[styles.fieldValue, { color: GREEN }]} numberOfLines={3}>{fmtValue(c.after)}</Text>
                </View>
              ))}
              {!(changedFields[n.id] || []).length && (
                <Text style={[styles.fallbackNote, { color: muted }]}>
                  Configuration replaced — apply to see the node's new settings.
                </Text>
              )}
            </View>
          ))}

          {added.map((n) => (
            <View key={`a-${n.id}`} style={[styles.lineEntry, { backgroundColor: greenBg }]}>
              <Ionicons name="add-circle-outline" size={12} color={GREEN} />
              <Text style={[styles.lineTitle, { color: GREEN }]} numberOfLines={1}>
                Added · {n.label || n.id}
              </Text>
              <Text style={[styles.lineMeta, { color: muted }]} numberOfLines={1}>{n.type}</Text>
            </View>
          ))}

          {removed.map((id) => (
            <View key={`r-${id}`} style={[styles.lineEntry, { backgroundColor: redBg }]}>
              <Ionicons name="remove-circle-outline" size={12} color={RED} />
              <Text style={[styles.lineTitle, { color: RED, textDecorationLine: 'line-through' }]} numberOfLines={1}>
                Removed · {id}
              </Text>
            </View>
          ))}

          {edgesAdded.map((e, i) => (
            <View key={`ea-${i}`} style={[styles.lineEntry, { backgroundColor: greenBg }]}>
              <Ionicons name="git-merge-outline" size={12} color={GREEN} />
              <Text style={[styles.lineMeta, { color: GREEN }]} numberOfLines={1}>
                Edge added · {e.source} → {e.target}
              </Text>
            </View>
          ))}

          {edgesRemoved.map((k, i) => (
            <View key={`er-${i}`} style={[styles.lineEntry, { backgroundColor: redBg }]}>
              <Ionicons name="git-merge-outline" size={12} color={RED} />
              <Text style={[styles.lineMeta, { color: RED }]} numberOfLines={1}>Edge removed · {k}</Text>
            </View>
          ))}

          {(varsAdded.length > 0 || varsRemoved.length > 0 || varsChanged.length > 0) && (
            <View style={[styles.entry, { backgroundColor: rowBg, borderColor: border }]}>
              <View style={[styles.entryHeader, { backgroundColor: isDark ? '#1e293b' : '#f1f5f9' }]}>
                <Ionicons name="code-outline" size={12} color={muted} />
                <Text style={[styles.entryTitle, { color: text }]}>Variables</Text>
              </View>
              {varsAdded.map(([k, v]) => (
                <View key={`va-${k}`} style={[styles.fieldRow, { borderTopColor: border }]}>
                  <Text style={[styles.fieldPath, { color: muted }]} numberOfLines={1}>{k}</Text>
                  <Text style={[styles.fieldValue, { color: GREEN }]} numberOfLines={2}>+ {fmtValue(v)}</Text>
                </View>
              ))}
              {varsRemoved.map(([k, v]) => (
                <View key={`vr-${k}`} style={[styles.fieldRow, { borderTopColor: border }]}>
                  <Text style={[styles.fieldPath, { color: muted }]} numberOfLines={1}>{k}</Text>
                  <Text style={[styles.fieldValue, { color: RED, textDecorationLine: 'line-through' }]} numberOfLines={2}>
                    {fmtValue(v)}
                  </Text>
                </View>
              ))}
              {varsChanged.map((c) => (
                <View key={`vc-${c.name}`} style={[styles.fieldRow, { borderTopColor: border }]}>
                  <Text style={[styles.fieldPath, { color: muted }]} numberOfLines={1}>{c.name}</Text>
                  <Text style={[styles.fieldValue, { color: RED }]} numberOfLines={2}>{fmtValue(c.before)}</Text>
                  <Ionicons name="arrow-forward" size={11} color={muted} style={{ marginHorizontal: 4 }} />
                  <Text style={[styles.fieldValue, { color: GREEN }]} numberOfLines={2}>{fmtValue(c.after)}</Text>
                </View>
              ))}
            </View>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { borderWidth: 1, borderRadius: 8, marginTop: 8, overflow: 'hidden' },
  header: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 10, paddingVertical: 8,
  },
  headerText: { fontSize: 12, fontWeight: '600' },
  chipRow: { flexDirection: 'row', gap: 4, flex: 1, justifyContent: 'flex-end' },
  chip: { borderRadius: 8, paddingHorizontal: 6, paddingVertical: 1 },
  chipText: { fontSize: 10, fontWeight: '700' },
  body: { paddingHorizontal: 8, paddingBottom: 8, gap: 6 },
  entry: { borderWidth: 1, borderRadius: 6, overflow: 'hidden' },
  entryHeader: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 8, paddingVertical: 6,
  },
  entryTitle: { fontSize: 11, fontWeight: '700', flexShrink: 1 },
  fieldRow: {
    flexDirection: 'row', alignItems: 'center',
    borderTopWidth: 1, paddingHorizontal: 8, paddingVertical: 5,
  },
  fieldPath: { fontSize: 10, fontFamily: 'monospace', width: '38%' },
  fieldValue: { fontSize: 10, fontFamily: 'monospace', flexShrink: 1, flex: 1 },
  fallbackNote: { fontSize: 10, fontStyle: 'italic', padding: 8 },
  lineEntry: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    borderRadius: 6, paddingHorizontal: 8, paddingVertical: 6,
  },
  lineTitle: { fontSize: 11, fontWeight: '700', flexShrink: 1 },
  lineMeta: { fontSize: 10, flexShrink: 1 },
});
