// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * NodePalette.js — Draggable sidebar palette of available node types
 */
import React, { useMemo, useState } from 'react';
import { View, Text, TextInput, TouchableOpacity, ScrollView, StyleSheet, Platform } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

const CATEGORY_META = {
  trigger: { label: 'Triggers', icon: 'flash-outline', color: '#22c55e' },
  source: { label: 'Data Sources', icon: 'download-outline', color: '#3b82f6' },
  agent: { label: 'AI Agents', icon: 'sparkles', color: '#a855f7' },
  processor: { label: 'AI / Processing', icon: 'sparkles-outline', color: '#8b5cf6' },
  logic: { label: 'Logic & Flow', icon: 'git-branch-outline', color: '#ec4899' },
  output: { label: 'Outputs', icon: 'push-outline', color: '#f59e0b' },
};

// 'dept_flow' removed 2026-08-08: that category held the Citra dept-MCP /
// Milvus ingestion nodes, which are gone (PORTING.md §1).
const CATEGORY_ORDER = ['trigger', 'source', 'agent', 'processor', 'logic', 'output'];

export default function NodePalette({ schemas = [], theme, width = 240 }) {
  const [search, setSearch] = useState('');
  const [collapsed, setCollapsed] = useState({});
  const isDark = theme?.isDark;
  const bg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  const grouped = useMemo(() => {
    const map = {};
    const q = search.toLowerCase();
    for (const s of schemas) {
      if (q && !s.label.toLowerCase().includes(q) && !s.type.toLowerCase().includes(q)) continue;
      const cat = s.category || 'processor';
      if (!map[cat]) map[cat] = [];
      map[cat].push(s);
    }
    return map;
  }, [schemas, search]);

  const onDragStart = (event, schema) => {
    if (Platform.OS !== 'web') return;
    event.dataTransfer.setData('application/json', JSON.stringify(schema));
    event.dataTransfer.effectAllowed = 'move';
  };

  return (
    <View style={[styles.container, { backgroundColor: bg, borderRightColor: border, width }]}>
      <Text style={[styles.heading, { color: text }]}>Nodes</Text>

      <View style={[styles.searchWrap, { borderColor: border }]}>
        <Ionicons name="search-outline" size={14} color={muted} />
        <TextInput
          value={search}
          onChangeText={setSearch}
          placeholder="Search nodes…"
          placeholderTextColor={muted}
          style={[styles.searchInput, { color: text }]}
        />
      </View>

      <ScrollView showsVerticalScrollIndicator={false} style={styles.scrollArea}>
        {CATEGORY_ORDER.map((cat) => {
          const items = grouped[cat];
          if (!items?.length) return null;
          const meta = CATEGORY_META[cat];
          const isCollapsed = collapsed[cat];

          return (
            <View key={cat} style={styles.catGroup}>
              <TouchableOpacity
                style={styles.catHeader}
                onPress={() => setCollapsed((p) => ({ ...p, [cat]: !p[cat] }))}
              >
                <View style={[styles.catDot, { backgroundColor: meta.color }]} />
                <Text style={[styles.catLabel, { color: text }]}>{meta.label}</Text>
                <Ionicons
                  name={isCollapsed ? 'chevron-forward' : 'chevron-down'}
                  size={14}
                  color={muted}
                />
              </TouchableOpacity>

              {!isCollapsed &&
                items.map((s) => (
                  <div
                    key={s.type}
                    draggable
                    onDragStart={(e) => onDragStart(e, s)}
                    style={{ cursor: 'grab' }}
                  >
                    <View
                      style={[
                        styles.nodeItem,
                        { borderColor: border, borderLeftColor: s.color || meta.color },
                      ]}
                    >
                      <Text style={{ fontSize: 16 }}>{s.icon || '⚙️'}</Text>
                      <View style={{ flex: 1 }}>
                        <Text style={[styles.nodeLabel, { color: text }]}>{s.label}</Text>
                        {s.description ? (
                          <Text style={[styles.nodeDesc, { color: muted }]} numberOfLines={1}>
                            {s.description}
                          </Text>
                        ) : null}
                      </View>
                      {s.dept_scope_required ? (
                        <Ionicons
                          name="lock-closed"
                          size={11}
                          color={muted}
                          title="Dept-scoped: targets a specific department's collection"
                        />
                      ) : null}
                    </View>
                  </div>
                ))}
            </View>
          );
        })}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    borderRightWidth: 1,
    paddingTop: 12,
  },
  heading: { fontSize: 14, fontWeight: '700', paddingHorizontal: 14, marginBottom: 8 },
  searchWrap: {
    flexDirection: 'row',
    alignItems: 'center',
    marginHorizontal: 12,
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 8,
    paddingVertical: 4,
    marginBottom: 8,
    gap: 6,
  },
  searchInput: { flex: 1, fontSize: 12, paddingVertical: 2, outline: 'none' },
  scrollArea: { flex: 1, paddingHorizontal: 12 },
  catGroup: { marginBottom: 12 },
  catHeader: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 6, paddingVertical: 2 },
  catDot: { width: 8, height: 8, borderRadius: 4 },
  catLabel: { flex: 1, fontSize: 12, fontWeight: '600', textTransform: 'uppercase', letterSpacing: 0.5 },
  nodeItem: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingVertical: 8,
    paddingHorizontal: 10,
    borderWidth: 1,
    borderLeftWidth: 3,
    borderRadius: 8,
    marginBottom: 4,
  },
  nodeLabel: { fontSize: 12, fontWeight: '600' },
  nodeDesc: { fontSize: 10, marginTop: 1 },
});
