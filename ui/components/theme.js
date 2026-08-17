// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * One theme object, passed down as a prop.
 *
 * The WorkflowBuilder components already expect a `theme` with these keys —
 * this is the palette they were written against, extracted so the shell has a
 * single source for it rather than each screen inventing colours.
 */

export const theme = {
  // surfaces
  background: '#0b0f19',
  surface: '#131a2a',
  surfaceAlt: '#1a2337',
  card: '#131a2a',
  border: '#243049',

  // text
  text: '#e8edf7',
  textSecondary: '#9aa8c4',
  textMuted: '#6b7a99',

  // accents
  primary: '#3b82f6',
  primaryText: '#ffffff',
  success: '#22c55e',
  warning: '#f59e0b',
  danger: '#ef4444',
  info: '#38bdf8',

  // misc
  inputBackground: '#0f1626',
  overlay: 'rgba(3, 7, 18, 0.72)',
};

export default theme;
