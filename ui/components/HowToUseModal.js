// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * HowToUseModal — the in-app guide the builder's "?" opens.
 *
 * The original was a shared modal covering a whole platform's features. This
 * one covers the workflow builder only, because that is all this repo ships.
 * `initialSection` is accepted (WorkflowBuilderScreen passes
 * "agent-builder-intro") and scrolls that section into view when it matches.
 */

import React, { useMemo } from 'react';
import {
  Modal,
  View,
  Text,
  ScrollView,
  TouchableOpacity,
  StyleSheet,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import theme from './theme';

const SECTIONS = [
  {
    id: 'agent-builder-intro',
    title: 'Describe the workflow',
    body:
      'Open the AI chat and say what should happen, in plain English: "every '
      + 'weekday at 7am, pull yesterday\'s orders from Postgres, flag any over '
      + '£10,000, and email me the list." The builder replies with a graph built '
      + 'only from real node types — it cannot invent one — and you edit it on '
      + 'the canvas from there.',
  },
  {
    id: 'nodes',
    title: 'Nodes and connections',
    body:
      'Every node declares its own fields, so the config panel and the AI both '
      + 'work from the same schema. Credentials never live in the graph: save a '
      + 'connection once, then reference it by name. Anything with a URL and a '
      + 'secret belongs in Connections, not in a node.',
  },
  {
    id: 'runs',
    title: 'Deploy, run, inspect',
    body:
      'A draft runs on demand. Deploying it activates its trigger — schedule, '
      + 'webhook, or manual. Every run records each node\'s input, output and '
      + 'timing, so when something fails you can see which node, with what data. '
      + 'Failed nodes retry with backoff; a restart resumes rather than restarts.',
  },
  {
    id: 'approvals',
    title: 'Human checkpoints',
    body:
      'Drop a Human Approval node anywhere in the graph and the run pauses there '
      + 'until someone approves or rejects it from the Approvals tab. Use it '
      + 'before anything irreversible.',
  },
  {
    id: 'safety',
    title: 'What agents may touch',
    body:
      'An AI agent node can call tools, and tools can change things. Write-'
      + 'capable tools stay blocked unless you enable them on that node. For MCP '
      + 'servers, a tool with no safety metadata is treated as unsafe — absence '
      + 'of a signal is never read as permission.',
  },
];

export default function HowToUseModal({ visible, onClose, initialSection = null }) {
  // Put the requested section first so it is what you land on. Cheap, and
  // avoids measuring layout to scroll to an offset.
  const ordered = useMemo(() => {
    if (!initialSection) return SECTIONS;
    const idx = SECTIONS.findIndex((s) => s.id === initialSection);
    if (idx <= 0) return SECTIONS;
    return [SECTIONS[idx], ...SECTIONS.filter((_, i) => i !== idx)];
  }, [initialSection]);

  return (
    <Modal visible={!!visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.backdrop}>
        <View style={styles.sheet}>
          <View style={styles.header}>
            <Text style={styles.title}>Using the workflow builder</Text>
            <TouchableOpacity onPress={onClose} accessibilityRole="button" accessibilityLabel="Close">
              <Ionicons name="close" size={22} color={theme.textSecondary} />
            </TouchableOpacity>
          </View>

          <ScrollView style={styles.body} contentContainerStyle={styles.bodyContent}>
            {ordered.map((s) => (
              <View key={s.id} style={styles.section}>
                <Text style={styles.sectionTitle}>{s.title}</Text>
                <Text style={styles.sectionBody}>{s.body}</Text>
              </View>
            ))}
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1, backgroundColor: theme.overlay,
    alignItems: 'center', justifyContent: 'center', padding: 24,
  },
  sheet: {
    width: '100%', maxWidth: 640, maxHeight: '85%',
    backgroundColor: theme.surface, borderColor: theme.border, borderWidth: 1,
    borderRadius: 14, overflow: 'hidden',
  },
  header: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    paddingVertical: 16, paddingHorizontal: 20,
    borderBottomColor: theme.border, borderBottomWidth: 1,
  },
  title: { color: theme.text, fontSize: 17, fontWeight: '700' },
  body: { flexGrow: 0 },
  bodyContent: { padding: 20, gap: 20 },
  section: { gap: 7 },
  sectionTitle: { color: theme.text, fontSize: 15, fontWeight: '700' },
  sectionBody: { color: theme.textSecondary, fontSize: 14, lineHeight: 21 },
});
