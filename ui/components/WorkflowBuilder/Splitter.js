// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * Splitter.js — a thin, draggable divider for resizing adjacent panes.
 *
 * Web-only (the Workflow Builder itself is web-only). On native it renders a
 * static divider so layouts don't crash. Dragging reports a pixel delta via
 * onDelta(dx, dy); the parent decides how to apply it (panel width / split
 * ratio). All browser APIs (window, document) are guarded behind
 * Platform.OS === 'web'.
 */
import React, { useCallback, useRef } from 'react';
import { View, StyleSheet, Platform } from 'react-native';

const isWeb = Platform.OS === 'web';

export default function Splitter({
  orientation = 'vertical', // 'vertical' = a vertical bar you drag left/right
  onDelta,                  // (dx, dy) => void — pixel delta since last move
  theme,
  thickness = 6,
}) {
  const lastPos = useRef(null);

  const handleMove = useCallback((e) => {
    if (lastPos.current == null) return;
    const x = e.clientX;
    const y = e.clientY;
    const dx = x - lastPos.current.x;
    const dy = y - lastPos.current.y;
    lastPos.current = { x, y };
    if (dx !== 0 || dy !== 0) onDelta?.(dx, dy);
  }, [onDelta]);

  const handleUp = useCallback(() => {
    lastPos.current = null;
    if (!isWeb || typeof window === 'undefined') return;
    window.removeEventListener('mousemove', handleMove);
    window.removeEventListener('mouseup', handleUp);
    if (typeof document !== 'undefined' && document.body) {
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    }
  }, [handleMove]);

  const handleDown = useCallback((e) => {
    if (!isWeb || typeof window === 'undefined') return;
    e.preventDefault?.();
    e.stopPropagation?.();
    lastPos.current = { x: e.clientX, y: e.clientY };
    window.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleUp);
    if (typeof document !== 'undefined' && document.body) {
      document.body.style.userSelect = 'none';
      document.body.style.cursor = orientation === 'vertical' ? 'col-resize' : 'row-resize';
    }
  }, [handleMove, handleUp, orientation]);

  const isDark = theme?.isDark;
  const base = isDark ? '#334155' : '#e2e8f0';
  const hover = isDark ? '#475569' : '#cbd5e1';

  const isVertical = orientation === 'vertical';
  const webProps = isWeb
    ? {
        onMouseDown: handleDown,
        onMouseEnter: (e) => { if (e?.currentTarget?.style) e.currentTarget.style.background = hover; },
        onMouseLeave: (e) => { if (lastPos.current == null && e?.currentTarget?.style) e.currentTarget.style.background = base; },
      }
    : {};

  return (
    <View
      {...webProps}
      style={[
        styles.base,
        { backgroundColor: base },
        isVertical
          ? { width: thickness, cursor: 'col-resize', height: '100%' }
          : { height: thickness, cursor: 'row-resize', width: '100%' },
      ]}
      accessibilityRole="adjustable"
      accessibilityLabel={isVertical ? 'Resize panel width' : 'Resize panel height'}
    >
      <View
        style={[
          styles.grip,
          isVertical
            ? { width: 2, height: 26, backgroundColor: hover }
            : { height: 2, width: 26, backgroundColor: hover },
        ]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  base: {
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
    zIndex: 60,
    ...(isWeb ? { transition: 'background 0.15s' } : {}),
  },
  grip: { borderRadius: 2, opacity: 0.8 },
});
