// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * MaintenancePanel.js — Admin maintenance actions for the workflow engine.
 *
 * Currently hosts one action: reclaiming orphaned workflow media. Workflow
 * nodes stash binary media (uploaded images, fetched PDFs, recorded audio) in
 * a GridFS bucket and pass it between nodes by reference. A run normally
 * deletes its own media the moment it finishes, fails, or is cancelled. But a
 * worker that's hard-killed (out-of-memory, container kill, redeploy) before
 * any handler runs leaves its media behind — and that storage adds up.
 *
 * This screen scans for that orphaned media and lets an admin reclaim it on
 * demand. The scan is read-only; reclaiming is gated behind an explicit
 * two-step confirm. Media for a still-running or paused-for-approval run is
 * never counted or deleted — it may still be needed.
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, TouchableOpacity, ScrollView, StyleSheet, ActivityIndicator,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';

function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export default function MaintenancePanel({ theme, onBack }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const infoBg = isDark ? '#0c2a3a' : '#ecfeff';
  const infoBorder = isDark ? '#155e75' : '#a5f3fc';

  const [usage, setUsage] = useState(null);     // last scan result
  const [scanning, setScanning] = useState(true);
  const [sweeping, setSweeping] = useState(false);
  const [confirming, setConfirming] = useState(false); // armed "Confirm reclaim?" state
  const [error, setError] = useState('');
  const [lastSweep, setLastSweep] = useState(null);    // result of the most recent reclaim

  const scan = useCallback(async () => {
    setScanning(true);
    setError('');
    setConfirming(false);
    try {
      const data = await WorkflowService.getBlobMaintenanceUsage();
      setUsage(data);
    } catch (err) {
      setError(err?.message || 'Failed to scan workflow media.');
      setUsage(null);
    } finally {
      setScanning(false);
    }
  }, []);

  useEffect(() => { scan(); }, [scan]);

  const reclaim = useCallback(async () => {
    setSweeping(true);
    setError('');
    setConfirming(false);
    try {
      const result = await WorkflowService.sweepOrphanBlobs();
      setLastSweep(result);
      // Re-scan so the numbers reflect what's left after the reclaim.
      const data = await WorkflowService.getBlobMaintenanceUsage();
      setUsage(data);
    } catch (err) {
      setError(err?.message || 'Failed to reclaim workflow media.');
    } finally {
      setSweeping(false);
    }
  }, []);

  const orphans = usage?.orphans || 0;
  const orphanBytes = usage?.orphan_bytes || 0;
  const hasOrphans = orphans > 0;

  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {/* Header */}
      <View style={[styles.header, { borderBottomColor: border }]}>
        <TouchableOpacity onPress={onBack} style={{ marginRight: 12 }}>
          <Ionicons name="arrow-back" size={22} color={text} />
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={[styles.heading, { color: text }]}>Maintenance</Text>
          <Text style={[styles.subhead, { color: muted }]}>
            Housekeeping for the workflow engine
          </Text>
        </View>
        <TouchableOpacity onPress={scan} disabled={scanning || sweeping}>
          <Ionicons name="refresh-outline" size={22} color={muted} />
        </TouchableOpacity>
      </View>

      <ScrollView contentContainerStyle={{ padding: 16, gap: 16 }}>
        {/* Action card: orphaned media */}
        <View style={[styles.card, { backgroundColor: cardBg, borderColor: border }]}>
          <View style={styles.cardHeaderRow}>
            <Ionicons name="server-outline" size={20} color="#06b6d4" />
            <Text style={[styles.cardTitle, { color: text }]}>Workflow media storage</Text>
          </View>

          {/* What this is / what it does */}
          <View style={[styles.infoBox, { backgroundColor: infoBg, borderColor: infoBorder }]}>
            <Text style={[styles.infoText, { color: text }]}>
              Workflow steps store images, PDFs, and audio they upload or fetch as
              binary “media” in the database, passing it between steps by reference.
              When a run finishes, fails, or is cancelled it deletes its own media
              automatically.
            </Text>
            <Text style={[styles.infoText, { color: text, marginTop: 8 }]}>
              <Text style={{ fontWeight: '700' }}>Orphaned media</Text> is left behind
              only when a worker is killed mid-run (e.g. a restart or out-of-memory)
              before that cleanup can run. Reclaiming it frees database storage.
              Media belonging to a run that is still in progress or paused for approval
              is never touched.
            </Text>
          </View>

          {/* Stats */}
          {scanning ? (
            <View style={styles.statsLoading}>
              <ActivityIndicator color="#06b6d4" />
              <Text style={[styles.muted, { color: muted }]}>Scanning…</Text>
            </View>
          ) : usage ? (
            <View style={styles.statsRow}>
              <Stat label="Media files" value={String(usage.scanned)} sub="total stored" color={text} muted={muted} border={border} />
              <Stat label="In use" value={String(usage.live_kept)} sub="active runs — kept" color="#16a34a" muted={muted} border={border} />
              <Stat label="Orphaned" value={String(orphans)} sub={formatBytes(orphanBytes)} color={hasOrphans ? '#f59e0b' : muted} muted={muted} border={border} />
            </View>
          ) : null}

          {/* Unattributable note (non-super admins) */}
          {!scanning && usage?.skipped_unattributable > 0 && (
            <Text style={[styles.note, { color: muted }]}>
              {usage.skipped_unattributable} file{usage.skipped_unattributable !== 1 ? 's' : ''} could
              not be matched to a workflow in your organization and can only be reclaimed by a super admin.
            </Text>
          )}

          {/* Reclaim action */}
          {!scanning && (
            hasOrphans ? (
              confirming ? (
                <View style={styles.confirmRow}>
                  <Text style={[styles.confirmText, { color: text }]}>
                    Permanently delete {orphans} orphaned file{orphans !== 1 ? 's' : ''} ({formatBytes(orphanBytes)})?
                  </Text>
                  <View style={styles.confirmBtns}>
                    <TouchableOpacity
                      style={[styles.btn, styles.btnGhost, { borderColor: border }]}
                      onPress={() => setConfirming(false)}
                      disabled={sweeping}
                    >
                      <Text style={[styles.btnGhostText, { color: muted }]}>Cancel</Text>
                    </TouchableOpacity>
                    <TouchableOpacity
                      style={[styles.btn, { backgroundColor: '#ef4444' }]}
                      onPress={reclaim}
                      disabled={sweeping}
                    >
                      {sweeping ? (
                        <ActivityIndicator size="small" color="#fff" />
                      ) : (
                        <>
                          <Ionicons name="trash-outline" size={16} color="#fff" />
                          <Text style={styles.btnText}>Confirm reclaim</Text>
                        </>
                      )}
                    </TouchableOpacity>
                  </View>
                </View>
              ) : (
                <TouchableOpacity
                  style={[styles.btn, { backgroundColor: '#f59e0b', alignSelf: 'flex-start' }]}
                  onPress={() => setConfirming(true)}
                  disabled={sweeping}
                >
                  <Ionicons name="sparkles-outline" size={16} color="#fff" />
                  <Text style={styles.btnText}>
                    Reclaim {orphans} file{orphans !== 1 ? 's' : ''} · {formatBytes(orphanBytes)}
                  </Text>
                </TouchableOpacity>
              )
            ) : (
              <View style={styles.cleanRow}>
                <Ionicons name="checkmark-circle" size={18} color="#16a34a" />
                <Text style={[styles.cleanText, { color: muted }]}>
                  No orphaned media — nothing to reclaim.
                </Text>
              </View>
            )
          )}

          {/* Last reclaim result */}
          {lastSweep && (
            <Text style={[styles.note, { color: '#16a34a' }]}>
              Reclaimed {lastSweep.deleted} file{lastSweep.deleted !== 1 ? 's' : ''}
              {' '}({formatBytes(lastSweep.deleted_bytes)}) on the last run.
            </Text>
          )}

          {/* Error */}
          {error ? (
            <View style={styles.errorRow}>
              <Ionicons name="alert-circle-outline" size={16} color="#ef4444" />
              <Text style={styles.errorText}>{error}</Text>
            </View>
          ) : null}
        </View>
      </ScrollView>
    </View>
  );
}

function Stat({ label, value, sub, color, muted, border }) {
  return (
    <View style={[styles.stat, { borderColor: border }]}>
      <Text style={[styles.statValue, { color }]}>{value}</Text>
      <Text style={[styles.statLabel, { color: muted }]}>{label}</Text>
      <Text style={[styles.statSub, { color: muted }]}>{sub}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: 'row', alignItems: 'center',
    paddingHorizontal: 16, paddingVertical: 14, borderBottomWidth: 1,
  },
  heading: { fontSize: 20, fontWeight: '700' },
  subhead: { fontSize: 13, marginTop: 2 },
  card: { borderWidth: 1, borderRadius: 12, padding: 16, gap: 14 },
  cardHeaderRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  cardTitle: { fontSize: 16, fontWeight: '700' },
  infoBox: { borderWidth: 1, borderRadius: 8, padding: 12 },
  infoText: { fontSize: 13, lineHeight: 19 },
  statsLoading: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 8 },
  muted: { fontSize: 13 },
  statsRow: { flexDirection: 'row', gap: 10 },
  stat: { flex: 1, borderWidth: 1, borderRadius: 8, padding: 12, alignItems: 'center' },
  statValue: { fontSize: 24, fontWeight: '800' },
  statLabel: { fontSize: 12, fontWeight: '600', marginTop: 2 },
  statSub: { fontSize: 11, marginTop: 1 },
  note: { fontSize: 12, lineHeight: 17 },
  confirmRow: { gap: 10 },
  confirmText: { fontSize: 14, fontWeight: '600' },
  confirmBtns: { flexDirection: 'row', gap: 10 },
  btn: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 8,
  },
  btnText: { color: '#fff', fontSize: 14, fontWeight: '700' },
  btnGhost: { borderWidth: 1, backgroundColor: 'transparent' },
  btnGhostText: { fontSize: 14, fontWeight: '600' },
  cleanRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  cleanText: { fontSize: 14 },
  errorRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  errorText: { color: '#ef4444', fontSize: 13, flex: 1 },
});
