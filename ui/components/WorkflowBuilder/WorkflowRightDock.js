/**
 * WorkflowRightDock.js — unified, resizable right-hand dock for the workflow
 * canvas. Hosts the Node Settings panel and the AI Assistant together so the
 * user never has to close one to use the other.
 *
 *  • Width is draggable (vertical splitter on the dock's left edge).
 *  • A 3-way segmented control acts as tabs + maximize/minimize:
 *      [Node Settings] [Split] [AI Chat]
 *      - Node Settings / AI Chat → that panel fills the dock (other minimized)
 *      - Split → both panels stacked, with a draggable height divider.
 *  • Sizes persist to localStorage (web, guarded).
 *
 * Layout-only: it does not touch execution, save, or LLM logic — it just
 * arranges the existing NodeConfigPanel and WorkflowAIChat.
 */
import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Platform } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import Splitter from './Splitter';
import NodeConfigPanel from './NodeConfigPanel';
import WorkflowAIChat from './WorkflowAIChat';

const isWeb = Platform.OS === 'web';

const MIN_WIDTH = 320;
const MAX_WIDTH = 760;
const MIN_RATIO = 0.2;
const MAX_RATIO = 0.8;
const WIDTH_KEY = 'wf_dock_width';
const RATIO_KEY = 'wf_dock_ratio';

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

const readStored = (key, fallback) => {
  if (!isWeb || typeof window === 'undefined') return fallback;
  try {
    const raw = window.localStorage?.getItem(key);
    const n = raw == null ? NaN : parseFloat(raw);
    return Number.isFinite(n) ? n : fallback;
  } catch { return fallback; }
};

const writeStored = (key, val) => {
  if (!isWeb || typeof window === 'undefined') return;
  try { window.localStorage?.setItem(key, String(val)); } catch { /* ignore */ }
};

export default function WorkflowRightDock({
  theme,
  node,                 // selected node (or null)
  schemas,
  onUpdateConfig,
  onUpdateLabel,
  onCloseNode,
  showAI,
  onCloseAI,
  aiProps = {},
}) {
  const hasNode = !!node;
  const hasAI = !!showAI;
  const both = hasNode && hasAI;

  const [dockWidth, setDockWidth] = useState(() => clamp(readStored(WIDTH_KEY, 400), MIN_WIDTH, MAX_WIDTH));
  const [ratio, setRatio] = useState(() => clamp(readStored(RATIO_KEY, 0.5), MIN_RATIO, MAX_RATIO));
  const [mode, setMode] = useState('split'); // 'node' | 'ai' | 'split'

  // Selecting a (new) node while the AI is open should always reveal the node
  // config — never leave it hidden behind a maximized AI panel.
  useEffect(() => {
    if (hasNode && hasAI && mode === 'ai') setMode('split');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.id]);

  const onWidthDrag = useCallback((dx) => {
    setDockWidth((w) => {
      const next = clamp(w - dx, MIN_WIDTH, MAX_WIDTH); // dock is on the right
      writeStored(WIDTH_KEY, Math.round(next));
      return next;
    });
  }, []);

  const onHeightDrag = useCallback((_dx, dy) => {
    setRatio((r) => {
      // Approximate: shift ratio by the dragged fraction of a typical dock height.
      const next = clamp(r + dy / 600, MIN_RATIO, MAX_RATIO);
      writeStored(RATIO_KEY, next.toFixed(3));
      return next;
    });
  }, []);

  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#ffffff';
  const border = isDark ? '#334155' : '#e2e8f0';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const activeBg = isDark ? '#1e293b' : '#eef2ff';
  const activeText = '#6366f1';

  // Effective mode: only meaningful when both panels are present.
  const effMode = !hasNode ? 'ai' : !hasAI ? 'node' : mode;

  const renderNode = () => (
    <NodeConfigPanel
      node={node}
      schemas={schemas}
      theme={theme}
      onUpdateConfig={onUpdateConfig}
      onUpdateLabel={onUpdateLabel}
      onClose={onCloseNode}
    />
  );

  const renderAI = () => (
    <WorkflowAIChat
      theme={theme}
      {...aiProps}
      onClose={onCloseAI}
    />
  );

  const Segment = ({ value, icon, label }) => {
    const active = effMode === value;
    return (
      <TouchableOpacity
        onPress={() => setMode(value)}
        style={[styles.segment, active && { backgroundColor: activeBg }]}
        accessibilityRole="button"
        accessibilityState={{ selected: active }}
        accessibilityLabel={label}
      >
        <Ionicons name={icon} size={13} color={active ? activeText : muted} />
        <Text style={[styles.segmentText, { color: active ? activeText : muted }]} numberOfLines={1}>
          {label}
        </Text>
      </TouchableOpacity>
    );
  };

  return (
    <View style={[styles.dock, { width: dockWidth, backgroundColor: bg }]}>
      <Splitter orientation="vertical" onDelta={onWidthDrag} theme={theme} />

      <View style={[styles.content, { borderLeftColor: border }]}>
        {/* Segmented control — only when both panels can be shown */}
        {both && (
          <View style={[styles.tabBar, { borderBottomColor: border, backgroundColor: isDark ? '#0b1220' : '#f8fafc' }]}>
            <Segment value="node" icon="options-outline" label="Node Settings" />
            <Segment value="split" icon="git-compare-outline" label="Split" />
            <Segment value="ai" icon="sparkles-outline" label="AI Chat" />
          </View>
        )}

        {/* Body */}
        {effMode === 'split' ? (
          <View style={styles.splitBody}>
            <View style={{ flexGrow: ratio, flexShrink: 1, flexBasis: 0, minHeight: 0 }}>
              {renderNode()}
            </View>
            <Splitter orientation="horizontal" onDelta={onHeightDrag} theme={theme} />
            <View style={{ flexGrow: 1 - ratio, flexShrink: 1, flexBasis: 0, minHeight: 0 }}>
              {renderAI()}
            </View>
          </View>
        ) : effMode === 'node' ? (
          <View style={styles.fullBody}>{renderNode()}</View>
        ) : (
          <View style={styles.fullBody}>{renderAI()}</View>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  dock: {
    flexDirection: 'row',
    height: '100%',
    flexShrink: 0,
    ...(isWeb ? { boxShadow: '-4px 0 24px rgba(0,0,0,0.08)' } : {}),
    zIndex: 40,
  },
  content: { flex: 1, borderLeftWidth: 1, minWidth: 0 },
  tabBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 6,
    paddingVertical: 6,
    borderBottomWidth: 1,
  },
  segment: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 5,
    paddingVertical: 7,
    borderRadius: 8,
  },
  segmentText: { fontSize: 11, fontWeight: '700' },
  splitBody: { flex: 1, flexDirection: 'column', minHeight: 0 },
  fullBody: { flex: 1, minHeight: 0 },
});
