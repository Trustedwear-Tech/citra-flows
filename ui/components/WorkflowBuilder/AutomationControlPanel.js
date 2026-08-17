// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * AutomationControlPanel.js — "Workflow Automation Control" for the workflow
 * engine. Two tabs:
 *
 *   1. Kill switches — stop/resume every workflow at global, org or dept scope.
 *   2. Schedules     — every workflow with its cron, next/last run and deploy
 *                      status, plus inline start/stop and edit-cron.
 *
 * Backend (all three already exist and are enforced):
 *   GET   /api/admin/workflow-halt
 *   GET   /api/admin/workflow-automation
 *   PATCH /api/workflows/{id}/schedule
 *
 * Ported from the pre-split Citra-UI console. Three things changed, and each is
 * a deliberate difference rather than a shortcut:
 *
 *   • It was a full-screen <Modal>. Every other panel here is an in-place view
 *     with an onBack header, so it is one too.
 *
 *   • The original filled its org/dept pickers from an admin user-service
 *     (listOrgs / listDepts). This product has no org registry — identity comes
 *     from the token. So the scopes are derived from the caller's own `org_id`
 *     and `dept_ids`. Nothing is lost: the server already refuses any scope
 *     wider than the caller's own unless they are super_admin
 *     (router.py:3126-3143), so the old picker was offering options the API
 *     would have rejected.
 *
 *   • Colours were hardcoded for a light theme. They are theme tokens now, with
 *     translucent tints so the halted/active states read on a dark surface.
 *
 * Halting is high-stakes and irreversible-feeling, so every switch is two-step:
 * press, then confirm. No browser dialogs — they are unstyleable and blocked in
 * some embedded contexts.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  View, Text, TouchableOpacity, ActivityIndicator, ScrollView, TextInput, StyleSheet,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import authService from '../../services/authService';

/** Roles that may reach the workflow API at all — mirrors WORKFLOW_ACCESS_ROLES
 *  in router.py:282. The IT-department carve-out below mirrors
 *  _has_workflow_access (router.py:286-294). */
const WORKFLOW_ACCESS_ROLES = ['super_admin', 'org_admin', 'IT-workflow'];
const IT_DEPT_ID = 'it';

function fmtTime(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch { return '—'; }
}

/** Translucent tint of a hex colour — lets one accent read as both a badge
 *  background and a card wash without inventing a second palette. */
function tint(hex, alpha) {
  const h = String(hex || '').replace('#', '');
  if (h.length !== 6) return 'transparent';
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export default function AutomationControlPanel({ theme, onBack }) {
  const colors = theme || {};
  const bg = colors.background || '#0b0f19';
  const surface = colors.surface || '#131a2a';
  const text = colors.text || '#e8edf7';
  const sub = colors.textSecondary || '#9aa8c4';
  const muted = colors.textMuted || '#6b7a99';
  const border = colors.border || '#243049';
  const primary = colors.primary || '#3b82f6';
  const danger = colors.danger || '#ef4444';
  const success = colors.success || '#22c55e';
  const warning = colors.warning || '#f59e0b';

  const me = useMemo(() => authService.getUser?.() || {}, []);
  const roles = useMemo(() => {
    if (Array.isArray(me.roles)) return me.roles;
    return me.roles ? [me.roles] : [];
  }, [me]);
  const myDeptIds = useMemo(() => (Array.isArray(me.dept_ids) ? me.dept_ids : []), [me]);

  const isSuper = roles.includes('super_admin');
  const isOrg = roles.includes('org_admin') || isSuper;
  // The same rule the API applies, so the UI stops offering what the server
  // would refuse. Everything else in this app leaves that to a 403.
  const hasAccess = roles.some((r) => WORKFLOW_ACCESS_ROLES.includes(r))
    || (roles.includes('dept_admin') && myDeptIds.map((d) => String(d).toLowerCase()).includes(IT_DEPT_ID));

  const [tab, setTab] = useState('kill');

  // ── kill switches ────────────────────────────────────────────────────────
  const [controls, setControls] = useState([]);
  const [haltNote, setHaltNote] = useState('');
  const [haltErr, setHaltErr] = useState('');
  const [haltBusy, setHaltBusy] = useState(null);
  const [pending, setPending] = useState(null);

  const loadHalt = useCallback(async () => {
    setHaltErr(''); setHaltNote(''); setPending(null);
    try {
      const r = await WorkflowService.listWorkflowHalt();
      setControls(Array.isArray(r?.controls) ? r.controls : []);
    } catch (e) {
      setControls([]);
      setHaltNote(e?.message || 'Could not load halt status. You can still act below.');
    }
  }, []);

  const isHalted = useCallback((scopeType, scopeId) => controls.some(
    (c) => c.scope_type === scopeType && (scopeType === 'global' || c.scope_id === scopeId),
  ), [controls]);

  const applyHalt = useCallback(async (row) => {
    setHaltBusy(row.key); setHaltErr('');
    try {
      await WorkflowService.setWorkflowHalt({
        scopeType: row.scopeType,
        scopeId: row.scopeId,
        enabled: row.enabled,
        reason: row.enabled ? `Halted from Automation Control (${row.label})` : '',
      });
      await loadHalt();
    } catch (e) {
      setHaltErr(e?.message || 'Action failed.');
    } finally {
      setHaltBusy(null); setPending(null);
    }
  }, [loadHalt]);

  /** Scopes come from the token, not a registry. super_admin additionally gets
   *  the deployment-wide switch; everyone else sees their own org and/or the
   *  departments they actually belong to. */
  const haltRows = useMemo(() => {
    const rows = [];
    const org = me.org_id;
    if (isSuper) {
      rows.push({
        key: 'global', scopeType: 'global', scopeId: null,
        label: 'Stop ALL workflows (entire deployment)',
        icon: 'earth-outline',
        hint: 'Freezes EVERY workflow in every organization — no scheduled or manual runs until resumed.',
      });
    }
    if (isOrg && org) {
      rows.push({
        key: `org:${org}`, scopeType: 'org', scopeId: org,
        label: `Stop ALL workflows in ${org}`,
        icon: 'business-outline',
        hint: 'Freezes every workflow in your organization.',
      });
    }
    for (const d of myDeptIds) {
      rows.push({
        key: `dept:${org}:${d}`, scopeType: 'dept', scopeId: `${org}:${d}`,
        label: `Department · ${d}`,
        icon: 'people-outline',
        hint: `Freezes workflows tagged with ${d}.`,
      });
    }
    return rows;
  }, [isSuper, isOrg, myDeptIds, me.org_id]);

  // ── schedules ────────────────────────────────────────────────────────────
  const [workflows, setWorkflows] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [wfLoading, setWfLoading] = useState(false);
  const [wfErr, setWfErr] = useState('');
  const [rowBusy, setRowBusy] = useState(null);
  const [editing, setEditing] = useState(null);   // {id, value}
  const [query, setQuery] = useState('');

  const loadWorkflows = useCallback(async () => {
    setWfLoading(true); setWfErr('');
    try {
      const r = await WorkflowService.listWorkflowAutomation();
      setWorkflows(Array.isArray(r?.workflows) ? r.workflows : []);
      setIncidents(Array.isArray(r?.incidents) ? r.incidents : []);
    } catch (e) {
      setWorkflows([]); setIncidents([]);
      setWfErr(e?.message || 'Could not load workflows.');
    } finally {
      setWfLoading(false);
    }
  }, []);

  const toggleSchedule = useCallback(async (w) => {
    setRowBusy(w.workflow_id); setWfErr('');
    try {
      await WorkflowService.setWorkflowSchedule(w.workflow_id, { enabled: !w.schedule_enabled });
      await loadWorkflows();
    } catch (e) {
      setWfErr(e?.message || 'Could not change schedule.');
    } finally { setRowBusy(null); }
  }, [loadWorkflows]);

  const saveCron = useCallback(async () => {
    if (!editing) return;
    setRowBusy(editing.id); setWfErr('');
    try {
      await WorkflowService.setWorkflowSchedule(editing.id, {
        cron_expression: String(editing.value || '').trim(),
      });
      setEditing(null);
      await loadWorkflows();
    } catch (e) {
      setWfErr(e?.message || 'Could not save cron.');
    } finally { setRowBusy(null); }
  }, [editing, loadWorkflows]);

  useEffect(() => {
    if (!hasAccess) return;
    loadHalt(); loadWorkflows();
  }, [hasAccess, loadHalt, loadWorkflows]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return workflows.filter((w) => !q || String(w.name || '').toLowerCase().includes(q));
  }, [workflows, query]);

  const Header = (
    <View style={[styles.header, { borderBottomColor: border }]}>
      <TouchableOpacity onPress={onBack} style={{ marginRight: 12 }} hitSlop={8}>
        <Ionicons name="arrow-back" size={22} color={text} />
      </TouchableOpacity>
      <View style={{ flex: 1 }}>
        <Text style={[styles.heading, { color: text }]}>Automation Control</Text>
        <Text style={[styles.subhead, { color: muted }]}>
          Stop or resume all workflows, and control each workflow&apos;s schedule
        </Text>
      </View>
      {hasAccess && (
        <TouchableOpacity onPress={() => { loadHalt(); loadWorkflows(); }} hitSlop={8}>
          <Ionicons name="refresh-outline" size={22} color={muted} />
        </TouchableOpacity>
      )}
    </View>
  );

  // Mirrors the API's own rule so this reads as "you don't have this" rather
  // than a failed request.
  if (!hasAccess) {
    return (
      <View style={[styles.container, { backgroundColor: bg }]}>
        {Header}
        <View style={styles.emptyWrap}>
          <Ionicons name="lock-closed-outline" size={40} color={muted} />
          <Text style={[styles.emptyTitle, { color: text }]}>Automation Control is IT-only</Text>
          <Text style={[styles.emptyBody, { color: muted }]}>
            Halting workflows and changing schedules is restricted to users with an
            IT role — IT-workflow, IT department admin, org admin, or super admin.
          </Text>
        </View>
      </View>
    );
  }

  const TabBtn = ({ id, label }) => (
    <TouchableOpacity
      onPress={() => setTab(id)}
      style={[styles.tabBtn, { borderBottomColor: tab === id ? primary : 'transparent' }]}
    >
      <Text style={{ color: tab === id ? primary : sub, fontWeight: tab === id ? '700' : '500', fontSize: 14 }}>
        {label}
      </Text>
    </TouchableOpacity>
  );

  const renderHaltCard = (row) => {
    const halted = isHalted(row.scopeType, row.scopeId);
    const busy = haltBusy === row.key;
    const confirming = pending && pending.key === row.key;
    return (
      <View
        key={row.key}
        style={[styles.card, {
          borderColor: halted ? danger : border,
          backgroundColor: halted ? tint(danger, 0.08) : surface,
        }]}
      >
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
          <Ionicons name={row.icon || 'business-outline'} size={16} color={halted ? danger : sub} />
          <Text style={{ flex: 1, fontSize: 14, fontWeight: '700', color: text }}>{row.label}</Text>
          <View style={[styles.badge, { backgroundColor: tint(halted ? danger : success, 0.16) }]}>
            <Text style={{ fontSize: 11, fontWeight: '700', color: halted ? danger : success }}>
              {halted ? 'HALTED' : 'Active'}
            </Text>
          </View>
        </View>
        <Text style={{ fontSize: 12, color: sub, marginTop: 4 }}>{row.hint}</Text>

        {confirming ? (
          <View style={{ flexDirection: 'row', gap: 8, marginTop: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ flex: 1, fontSize: 12, color: danger, fontWeight: '600', minWidth: 200 }}>
              {pending.enabled
                ? `Halt ${pending.label}? Scheduled and manual runs stop now.`
                : `Resume ${pending.label}?`}
            </Text>
            <TouchableOpacity onPress={() => setPending(null)} style={[styles.btnGhost, { borderColor: border }]}>
              <Text style={{ color: sub, fontSize: 12, fontWeight: '600' }}>Cancel</Text>
            </TouchableOpacity>
            <TouchableOpacity
              disabled={busy}
              onPress={() => applyHalt(pending)}
              style={[styles.btn, { backgroundColor: pending.enabled ? danger : success, opacity: busy ? 0.6 : 1 }]}
            >
              {busy
                ? <ActivityIndicator color="#fff" size="small" />
                : <Text style={styles.btnText}>{pending.enabled ? 'Confirm halt' : 'Confirm resume'}</Text>}
            </TouchableOpacity>
          </View>
        ) : (
          <TouchableOpacity
            onPress={() => setPending({ ...row, enabled: !halted })}
            style={[styles.btn, styles.btnInline, { backgroundColor: halted ? success : danger }]}
          >
            <Ionicons name={halted ? 'play' : 'stop'} size={14} color="#fff" />
            <Text style={styles.btnText}>{halted ? 'Resume' : 'Halt'}</Text>
          </TouchableOpacity>
        )}
      </View>
    );
  };

  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {Header}

      <View style={[styles.tabBar, { borderBottomColor: border }]}>
        <TabBtn id="kill" label="Kill switches" />
        <TabBtn id="sched" label="Schedules" />
      </View>

      {tab === 'kill' ? (
        <ScrollView style={{ flex: 1 }} contentContainerStyle={styles.scrollBody}>
          {!!haltNote && (
            <Text style={[styles.notice, { color: warning, backgroundColor: tint(warning, 0.12) }]}>{haltNote}</Text>
          )}
          {!!haltErr && (
            <Text style={[styles.notice, { color: danger, backgroundColor: tint(danger, 0.12) }]}>{haltErr}</Text>
          )}
          {haltRows.length === 0 ? (
            <Text style={{ color: sub, fontSize: 13, padding: 12 }}>
              You don&apos;t have a halt scope. Manage individual schedules in the Schedules tab.
            </Text>
          ) : haltRows.map(renderHaltCard)}
        </ScrollView>
      ) : (
        <View style={{ flex: 1 }}>
          <View style={[styles.searchBar, { borderBottomColor: border }]}>
            <View style={[styles.searchBox, { borderColor: border, backgroundColor: colors.inputBackground || surface }]}>
              <Ionicons name="search" size={14} color={sub} />
              <TextInput
                value={query}
                onChangeText={setQuery}
                placeholder="Search workflow…"
                placeholderTextColor={muted}
                style={{ flex: 1, paddingVertical: 8, color: text, fontSize: 13 }}
              />
            </View>
          </View>

          {!!wfErr && (
            <Text style={[styles.notice, { color: danger, backgroundColor: tint(danger, 0.12), margin: 12, marginBottom: 0 }]}>{wfErr}</Text>
          )}

          {incidents.length > 0 && (
            <View style={{ backgroundColor: tint(danger, 0.1), borderBottomWidth: 1, borderBottomColor: tint(danger, 0.3), paddingHorizontal: 12, paddingVertical: 8 }}>
              <Text style={{ color: danger, fontSize: 12, fontWeight: '700', marginBottom: 4 }}>
                Recent failures ({incidents.length})
              </Text>
              {incidents.slice(0, 6).map((inc, i) => (
                <Text key={`${inc.workflow_id}:${i}`} style={{ color: sub, fontSize: 11 }} numberOfLines={1}>
                  • {inc.name} · {inc.status} · {fmtTime(inc.at)}{inc.error ? ` — ${inc.error}` : ''}
                </Text>
              ))}
            </View>
          )}

          {wfLoading ? (
            <ActivityIndicator color={primary} style={{ marginTop: 32 }} />
          ) : (
            <ScrollView style={{ flex: 1 }} contentContainerStyle={{ padding: 12 }}>
              {filtered.length === 0 ? (
                <Text style={{ color: sub, fontSize: 13, padding: 12 }}>No workflows in your scope.</Text>
              ) : filtered.map((w) => {
                const busy = rowBusy === w.workflow_id;
                const isEditing = editing && editing.id === w.workflow_id;
                const deployed = w.deployed;
                return (
                  <View key={w.workflow_id} style={[styles.card, { borderColor: border, backgroundColor: surface }]}>
                    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <Ionicons name="git-network-outline" size={16} color={primary} />
                      <Text style={{ fontWeight: '700', color: text, fontSize: 14 }}>{w.name || w.workflow_id}</Text>
                      <View style={[styles.chip, { backgroundColor: tint(deployed ? success : muted, 0.16) }]}>
                        <Text style={{ fontSize: 10, fontWeight: '700', color: deployed ? success : muted }}>
                          {deployed ? 'Deployed' : (w.status || 'draft')}
                        </Text>
                      </View>
                      <View style={[styles.chip, { backgroundColor: tint(w.schedule_enabled ? success : muted, 0.16) }]}>
                        <Text style={{ fontSize: 10, fontWeight: '700', color: w.schedule_enabled ? success : muted }}>
                          {w.schedule_enabled ? 'Schedule ON' : 'Schedule OFF'}
                        </Text>
                      </View>
                    </View>

                    <Text style={{ fontSize: 12, color: sub, marginTop: 4 }}>
                      {w.cron_expression ? `cron · ${w.cron_expression}` : 'no cron set'}
                      {'  ·  next: '}{fmtTime(w.next_run)}
                      {'  ·  last: '}{fmtTime(w.last_run?.at)}{w.last_run?.status ? ` (${w.last_run.status})` : ''}
                    </Text>

                    {isEditing ? (
                      <View style={{ flexDirection: 'row', gap: 8, marginTop: 8, alignItems: 'center' }}>
                        <TextInput
                          value={String(editing.value ?? '')}
                          onChangeText={(v) => setEditing((e) => ({ ...e, value: v }))}
                          placeholder="0 9 * * 1"
                          placeholderTextColor={muted}
                          style={{
                            flex: 1, borderWidth: 1, borderColor: border, borderRadius: 8,
                            paddingHorizontal: 10, paddingVertical: 8, color: text, fontSize: 13,
                            backgroundColor: colors.inputBackground || bg,
                          }}
                        />
                        <TouchableOpacity onPress={() => setEditing(null)} style={[styles.btnGhost, { borderColor: border }]}>
                          <Text style={{ color: sub, fontSize: 12, fontWeight: '600' }}>Cancel</Text>
                        </TouchableOpacity>
                        <TouchableOpacity disabled={busy} onPress={saveCron} style={[styles.btn, { backgroundColor: primary, opacity: busy ? 0.6 : 1 }]}>
                          {busy ? <ActivityIndicator color="#fff" size="small" /> : <Text style={styles.btnText}>Save cron</Text>}
                        </TouchableOpacity>
                      </View>
                    ) : (
                      <View style={{ flexDirection: 'row', gap: 8, marginTop: 8 }}>
                        <TouchableOpacity
                          disabled={busy}
                          onPress={() => toggleSchedule(w)}
                          style={[styles.btn, styles.btnInline, { backgroundColor: w.schedule_enabled ? danger : success, opacity: busy ? 0.6 : 1 }]}
                        >
                          <Ionicons name={w.schedule_enabled ? 'stop' : 'play'} size={13} color="#fff" />
                          <Text style={styles.btnText}>{w.schedule_enabled ? 'Stop' : 'Start'}</Text>
                        </TouchableOpacity>
                        <TouchableOpacity
                          onPress={() => setEditing({ id: w.workflow_id, value: w.cron_expression || '' })}
                          style={[styles.btnGhost, styles.btnInline, { borderColor: border }]}
                        >
                          <Ionicons name="time-outline" size={13} color={text} />
                          <Text style={{ color: text, fontSize: 12, fontWeight: '600' }}>Edit cron</Text>
                        </TouchableOpacity>
                      </View>
                    )}
                  </View>
                );
              })}
            </ScrollView>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: 'row', alignItems: 'center',
    paddingHorizontal: 16, paddingVertical: 14, borderBottomWidth: 1,
  },
  heading: { fontSize: 18, fontWeight: '800' },
  subhead: { fontSize: 12, marginTop: 2 },
  tabBar: { flexDirection: 'row', paddingHorizontal: 12, borderBottomWidth: 1 },
  tabBtn: { paddingVertical: 12, paddingHorizontal: 16, borderBottomWidth: 2 },
  scrollBody: { padding: 16, maxWidth: 720, alignSelf: 'center', width: '100%' },
  card: { borderWidth: 1, borderRadius: 10, padding: 12, marginBottom: 10 },
  badge: { paddingHorizontal: 8, paddingVertical: 3, borderRadius: 12 },
  chip: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 4 },
  notice: { fontSize: 12, padding: 10, borderRadius: 8, marginBottom: 10, overflow: 'hidden' },
  btn: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8 },
  btnGhost: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8, borderWidth: 1 },
  btnInline: { flexDirection: 'row', alignItems: 'center', gap: 6, alignSelf: 'flex-start' },
  btnText: { color: '#fff', fontSize: 12, fontWeight: '700' },
  searchBar: { flexDirection: 'row', alignItems: 'center', gap: 8, padding: 12, borderBottomWidth: 1 },
  searchBox: {
    flex: 1, flexDirection: 'row', alignItems: 'center', gap: 6,
    borderWidth: 1, borderRadius: 8, paddingHorizontal: 10,
  },
  emptyWrap: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 32, gap: 10 },
  emptyTitle: { fontSize: 16, fontWeight: '700' },
  emptyBody: { fontSize: 13, textAlign: 'center', lineHeight: 19, maxWidth: 420 },
});
