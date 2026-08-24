// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * WorkflowSettingsModal.js — Workflow-level settings.
 *
 * Scoped to the failure-alert master switch. On failure the workflow's owner
 * (the BA who authored it — an address auto-derived from their login, never
 * free-text) is emailed, and the failure is logged at ERROR for IT. There is
 * intentionally NO way to add arbitrary recipient addresses here, so an alert
 * can never be routed to an outside-org email.
 * See docs/workflow-visibility-ownership.md.
 *
 * The modal edits a local copy and hands it back via onSave; the actual
 * persistence happens on the workflow Save, same as name/description.
 */
import React, { useState, useEffect } from 'react';
import {
  View, Text, TouchableOpacity, Modal, ScrollView,
  StyleSheet, Switch,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';

export default function WorkflowSettingsModal({ visible, theme, notifications, onClose, onSave }) {
  const isDark = theme?.isDark;
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  const [notifyOnFailure, setNotifyOnFailure] = useState(true);

  // Reset local state from props each time the modal opens.
  useEffect(() => {
    if (!visible) return;
    setNotifyOnFailure(notifications?.notify_on_failure !== false);
  }, [visible, notifications]);

  const handleDone = () => {
    onSave({ notify_on_failure: notifyOnFailure });
    onClose();
  };

  if (!visible) return null;

  return (
    <Modal visible transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={[styles.card, { backgroundColor: cardBg }]}>
          {/* Header */}
          <View style={styles.header}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
              <Ionicons name="settings-outline" size={20} color="#6366f1" />
              <Text style={[styles.title, { color: text }]}>Workflow Settings</Text>
            </View>
            <TouchableOpacity onPress={onClose}>
              <Ionicons name="close" size={22} color={text} />
            </TouchableOpacity>
          </View>

          <ScrollView style={{ maxHeight: 420 }} contentContainerStyle={{ paddingBottom: 4 }}>
            {/* Failure alerts section */}
            <Text style={[styles.sectionTitle, { color: text }]}>Failure Alerts</Text>
            <Text style={[styles.sectionDesc, { color: muted }]}>
              When a deployed run fails, the workflow owner is emailed and the
              failure is logged for IT. Recipients can't be added here — alerts
              only ever go to the owner.
            </Text>

            <View style={[styles.toggleRow, { borderColor: border }]}>
              <View style={{ flex: 1 }}>
                <Text style={[styles.toggleLabel, { color: text }]}>Email owner on failure</Text>
                <Text style={[styles.toggleHint, { color: muted }]}>
                  Turn off to silence the owner email for this workflow. Failures
                  are still logged for IT either way.
                </Text>
              </View>
              <Switch
                value={notifyOnFailure}
                onValueChange={setNotifyOnFailure}
                trackColor={{ true: '#6366f1', false: isDark ? '#475569' : '#cbd5e1' }}
                thumbColor="#ffffff"
              />
            </View>
          </ScrollView>

          {/* Footer */}
          <View style={[styles.footer, { borderTopColor: border }]}>
            <TouchableOpacity style={styles.cancelBtn} onPress={onClose}>
              <Text style={[styles.cancelBtnText, { color: muted }]}>Cancel</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.doneBtn} onPress={handleDone}>
              <Ionicons name="checkmark" size={16} color="#fff" />
              <Text style={styles.doneBtnText}>Done</Text>
            </TouchableOpacity>
          </View>
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
  card: { width: 460, borderRadius: 14, padding: 20 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 14,
  },
  title: { fontSize: 17, fontWeight: '700' },
  sectionTitle: { fontSize: 14, fontWeight: '700', marginBottom: 4 },
  sectionDesc: { fontSize: 12, lineHeight: 17, marginBottom: 12 },
  toggleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
    marginBottom: 4,
  },
  toggleLabel: { fontSize: 13, fontWeight: '600' },
  toggleHint: { fontSize: 11, marginTop: 2 },
  footer: {
    flexDirection: 'row',
    justifyContent: 'flex-end',
    gap: 10,
    borderTopWidth: 1,
    paddingTop: 14,
    marginTop: 14,
  },
  cancelBtn: { paddingHorizontal: 16, paddingVertical: 9 },
  cancelBtnText: { fontSize: 14, fontWeight: '600' },
  doneBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    backgroundColor: '#6366f1',
    paddingHorizontal: 18,
    paddingVertical: 9,
    borderRadius: 8,
  },
  doneBtnText: { color: '#fff', fontWeight: '600', fontSize: 14 },
});
