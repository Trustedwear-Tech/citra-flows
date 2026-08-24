// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * ConnectionManager.js — CRUD screen for workflow connections with test/prod configs
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, ScrollView,
  StyleSheet, ActivityIndicator, Modal, Platform,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';

const CONNECTION_TYPES = [
  { value: 'sql', label: 'SQL Database', icon: '🗄️', color: '#3b82f6' },
  { value: 'mongo', label: 'NoSQL Database', icon: '🗃️', color: '#22c55e' },
  { value: 'api', label: 'REST API', icon: '🌐', color: '#8b5cf6' },
  { value: 'sftp', label: 'SFTP / FTP', icon: '📁', color: '#06b6d4' },
  { value: 'bucket', label: 'S3 / Object Storage', icon: '☁️', color: '#f59e0b' },
  { value: 'smtp', label: 'Email (SMTP)', icon: '✉️', color: '#ec4899' },
];

export default function ConnectionManager({ theme, onBack }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const inputBg = isDark ? '#0f172a' : '#f1f5f9';

  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState(null); // null = create, object = edit
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [formError, setFormError] = useState(''); // app-level save/validation error
  const [listError, setListError] = useState(''); // app-level list/delete error

  // Form state
  const [formName, setFormName] = useState('');
  const [formType, setFormType] = useState('sql');
  const [formDesc, setFormDesc] = useState('');
  const [formTab, setFormTab] = useState('test'); // 'test' | 'prod'
  const _emptyEnv = () => ({ connection_string: '', url: '', headers: '{}', database: '', host: '', port: '22', username: '', password: '', private_key: '', protocol: 'sftp', bucket: '', region: 'us-east-1', access_key_id: '', secret_access_key: '', endpoint_url: '', from_address: '', smtp_port: '587' });
  const [formTest, setFormTest] = useState(_emptyEnv());
  const [formProd, setFormProd] = useState(_emptyEnv());

  useEffect(() => {
    if (formError === 'Please enter a connection name.' && formName.trim()) {
      setFormError('');
    }
  }, [formError, formName]);

  const loadConnections = useCallback(async () => {
    setLoading(true);
    try {
      const data = await WorkflowService.listConnections();
      setConnections(data?.connections || []);
    } catch {
      setConnections([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadConnections(); }, [loadConnections]);

  const openCreate = () => {
    setEditing(null);
    setFormName('');
    setFormType('sql');
    setFormDesc('');
    setFormTab('test');
    setFormTest(_emptyEnv());
    setFormProd(_emptyEnv());
    setTestResult(null);
    setFormError('');
    setShowForm(true);
  };

  const openEdit = (conn) => {
    setEditing(conn);
    setFormName(conn.name);
    setFormType(conn.type);
    setFormDesc(conn.description || '');
    setFormTab('test');
    // Secrets come back from the API MASKED (••••••••). Never prefill them into
    // the editable fields — re-saving a mask would overwrite the real credential
    // with the mask string. Leave every secret field BLANK; the backend keeps
    // the stored value for any secret left blank on update. Non-secret fields
    // (host, port, database, region, bucket, protocol, …) are real and prefilled.
    const _loadEnv = (e) => ({
      connection_string: '',
      url: '',
      headers: '{}',
      database: e?.database || '',
      host: e?.host || '',
      port: String(e?.port || '22'),
      username: '',
      password: '',
      private_key: '',
      protocol: e?.protocol || 'sftp',
      bucket: e?.bucket || '',
      region: e?.region || 'us-east-1',
      access_key_id: '',
      secret_access_key: '',
      endpoint_url: e?.endpoint_url || '',
      from_address: e?.from_address || '',
      smtp_port: String(e?.smtp_port || e?.port || '587'),
    });
    setFormTest(_loadEnv(conn.test));
    setFormProd(_loadEnv(conn.prod));
    setTestResult(null);
    setFormError('');
    setShowForm(true);
  };

  const buildEnvConfig = (form, type) => {
    if (!form.connection_string && !form.host && !form.url && !form.bucket) return null;
    const cfg = {};
    // SQL / Mongo
    if (['sql', 'mongo'].includes(type)) {
      if (form.connection_string) cfg.connection_string = form.connection_string;
      if (form.database) cfg.database = form.database;
      if (form.host) cfg.host = form.host;
      if (form.port) cfg.port = String(parseInt(form.port, 10) || (type === 'mongo' ? 27017 : 5432));
      if (form.username) cfg.username = form.username;
      if (form.password) cfg.password = form.password;
    }
    // REST API
    if (type === 'api') {
      if (form.url) cfg.url = form.url;
      try {
        const h = JSON.parse(form.headers || '{}');
        if (Object.keys(h).length > 0) cfg.headers = h;
      } catch { /* ignore bad JSON */ }
    }
    // SFTP / FTP
    if (type === 'sftp') {
      if (form.host) cfg.host = form.host;
      if (form.port) cfg.port = String(parseInt(form.port, 10) || 22);
      if (form.username) cfg.username = form.username;
      if (form.password) cfg.password = form.password;
      if (form.private_key) cfg.private_key = form.private_key;
      if (form.protocol) cfg.protocol = form.protocol;
    }
    // S3 / bucket
    if (type === 'bucket') {
      if (form.bucket) cfg.bucket = form.bucket;
      if (form.region) cfg.region = form.region;
      if (form.access_key_id) cfg.access_key_id = form.access_key_id;
      if (form.secret_access_key) cfg.secret_access_key = form.secret_access_key;
      if (form.endpoint_url) cfg.endpoint_url = form.endpoint_url;
    }
    // SMTP
    if (type === 'smtp') {
      if (form.host) cfg.host = form.host;
      if (form.smtp_port) cfg.port = String(parseInt(form.smtp_port, 10) || 587);
      if (form.username) cfg.username = form.username;
      if (form.password) cfg.password = form.password;
      if (form.from_address) cfg.from_address = form.from_address;
    }
    return cfg;
  };

  const handleSave = async () => {
    setFormError('');
    if (!formName.trim()) {
      setFormError('Please enter a connection name.');
      return;
    }

    const testCfg = buildEnvConfig(formTest, formType);
    const prodCfg = buildEnvConfig(formProd, formType);

    // On CREATE, at least one environment must be configured. On EDIT, secrets
    // are left blank by design (they're preserved server-side), so an empty
    // form is valid — e.g. renaming without re-entering credentials. Only
    // environments the user actually filled are sent; the rest stay untouched.
    if (!editing && !testCfg && !prodCfg) {
      setFormError('Please configure at least one environment: Test or Production.');
      return;
    }

    setSaving(true);
    try {
      // Only include an environment when it is actually configured. Sending an
      // explicit `null` would fail backend validation (it expects an object).
      const payload = { name: formName, type: formType, description: formDesc };
      if (testCfg) payload.test = testCfg;
      if (prodCfg) payload.prod = prodCfg;

      if (editing) {
        await WorkflowService.updateConnection(editing.connection_id, payload);
      } else {
        await WorkflowService.createConnection(payload);
      }
      setShowForm(false);
      await loadConnections();
    } catch (err) {
      setFormError(err?.message || 'Failed to save connection. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (conn) => {
    if (!confirm(`Delete connection "${conn.name}"?`)) return;
    setListError('');
    try {
      await WorkflowService.deleteConnection(conn.connection_id);
      await loadConnections();
    } catch (err) {
      setListError(err?.message || `Failed to delete connection "${conn.name}".`);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    setFormError('');
    try {
      let res;
      if (editing) {
        res = await WorkflowService.testConnection(editing.connection_id, formTab);
      } else {
        const config = buildEnvConfig(formTab === 'test' ? formTest : formProd, formType);
        res = await WorkflowService.testDraftConnection(formType, config);
      }
      setTestResult(res);
    } catch (err) {
      setTestResult({ success: false, message: err.message });
    } finally {
      setTesting(false);
    }
  };

  const activeEnv = formTab === 'test' ? formTest : formProd;
  const setActiveEnv = formTab === 'test' ? setFormTest : setFormProd;
  const typeInfo = CONNECTION_TYPES.find((t) => t.value === formType);

  // ── Render ─────────────────────────────────────────────
  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {/* Header */}
      <View style={[styles.header, { backgroundColor: cardBg, borderBottomColor: border }]}>
        <TouchableOpacity onPress={onBack} style={styles.backBtn}>
          <Ionicons name="arrow-back" size={20} color={text} />
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={[styles.heading, { color: text }]}>Connections</Text>
          <Text style={{ color: muted, fontSize: 12 }}>Shared across your IT team · database & API connections with test/prod configs</Text>
        </View>
        <TouchableOpacity style={styles.createBtn} onPress={openCreate}>
          <Ionicons name="add" size={18} color="#fff" />
          <Text style={styles.createBtnText}>New Connection</Text>
        </TouchableOpacity>
      </View>

      {/* Connection list */}
      {loading ? (
        <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center' }}>
          <ActivityIndicator size="large" color="#3b82f6" />
        </View>
      ) : (
        <ScrollView contentContainerStyle={styles.list}>
          {listError ? (
            <View style={styles.errorBox}>
              <Ionicons name="alert-circle" size={16} color="#ef4444" />
              <Text style={{ color: '#ef4444', fontSize: 12, flex: 1 }}>{listError}</Text>
            </View>
          ) : null}
          {connections.length === 0 && (
            <View style={{ alignItems: 'center', paddingTop: 60 }}>
              <Text style={{ fontSize: 40, marginBottom: 12 }}>🔌</Text>
              <Text style={{ color: text, fontSize: 16, fontWeight: '600' }}>No connections yet</Text>
              <Text style={{ color: muted, marginTop: 4, textAlign: 'center' }}>
                Create a connection to reuse database & API credentials across workflows.
                {'\n'}Connections are shared with your whole IT team.
              </Text>
            </View>
          )}
          {connections.map((conn) => {
            const ct = CONNECTION_TYPES.find((t) => t.value === conn.type);
            return (
              <TouchableOpacity
                key={conn.connection_id}
                style={[styles.card, { backgroundColor: cardBg, borderColor: border }]}
                onPress={() => openEdit(conn)}
              >
                <View style={styles.cardRow}>
                  <Text style={{ fontSize: 24 }}>{ct?.icon || '🔗'}</Text>
                  <View style={{ flex: 1 }}>
                    <Text style={[styles.cardName, { color: text }]}>{conn.name}</Text>
                    <Text style={{ color: muted, fontSize: 12 }}>{ct?.label || conn.type}</Text>
                    {conn.description ? (
                      <Text style={{ color: muted, fontSize: 11, marginTop: 2 }} numberOfLines={1}>{conn.description}</Text>
                    ) : null}
                    {conn.user_id ? (
                      <Text style={{ color: muted, fontSize: 10, marginTop: 2 }} numberOfLines={1}>
                        Created by {conn.user_id}
                      </Text>
                    ) : null}
                  </View>
                  <View style={{ flexDirection: 'row', gap: 4 }}>
                    {conn.test && (conn.test.connection_string || conn.test.host || conn.test.url || conn.test.bucket) ? (
                      <View style={[styles.envBadge, { backgroundColor: '#3b82f620' }]}>
                        <Text style={{ fontSize: 10, color: '#3b82f6', fontWeight: '600' }}>TEST</Text>
                      </View>
                    ) : null}
                    {conn.prod && (conn.prod.connection_string || conn.prod.host || conn.prod.url || conn.prod.bucket) ? (
                      <View style={[styles.envBadge, { backgroundColor: '#22c55e20' }]}>
                        <Text style={{ fontSize: 10, color: '#22c55e', fontWeight: '600' }}>PROD</Text>
                      </View>
                    ) : null}
                  </View>
                  <TouchableOpacity onPress={() => handleDelete(conn)} style={{ padding: 4 }}>
                    <Ionicons name="trash-outline" size={18} color="#ef4444" />
                  </TouchableOpacity>
                </View>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
      )}

      {/* Create / Edit Form Modal */}
      {showForm && (
        <Modal visible transparent animationType="fade" onRequestClose={() => setShowForm(false)}>
          <View style={styles.overlay}>
            <View style={[styles.formCard, { backgroundColor: cardBg }]}>
              <ScrollView showsVerticalScrollIndicator={false}>
                {/* Title */}
                <View style={{ flexDirection: 'row', justifyContent: 'space-between', marginBottom: 16 }}>
                  <Text style={[styles.formTitle, { color: text }]}>
                    {editing ? 'Edit Connection' : 'New Connection'}
                  </Text>
                  <TouchableOpacity onPress={() => setShowForm(false)}>
                    <Ionicons name="close" size={22} color={muted} />
                  </TouchableOpacity>
                </View>

                {/* Name */}
                <Text style={[styles.label, { color: text }]}>Name *</Text>
                <TextInput
                  value={formName}
                  onFocus={() => setFormError('')}
                  onChangeText={(t) => {
                    setFormName(t);
                    setFormError('');
                  }}
                  placeholder="e.g. Sales Database"
                  placeholderTextColor={muted}
                  style={[styles.input, { color: text, backgroundColor: inputBg, borderColor: border }]}
                />

                {/* Type */}
                <Text style={[styles.label, { color: text, marginTop: 12 }]}>Type</Text>
                <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
                  {CONNECTION_TYPES.map((ct) => (
                    <TouchableOpacity
                      key={ct.value}
                      onPress={() => setFormType(ct.value)}
                      style={[
                        styles.typeChip,
                        {
                          borderColor: formType === ct.value ? ct.color : border,
                          backgroundColor: formType === ct.value ? `${ct.color}15` : 'transparent',
                        },
                      ]}
                    >
                      <Text style={{ fontSize: 16 }}>{ct.icon}</Text>
                      <Text style={{ fontSize: 12, color: formType === ct.value ? ct.color : muted, fontWeight: '600' }}>
                        {ct.label}
                      </Text>
                    </TouchableOpacity>
                  ))}
                </View>

                {/* Description */}
                <Text style={[styles.label, { color: text }]}>Description</Text>
                <TextInput
                  value={formDesc}
                  onChangeText={setFormDesc}
                  placeholder="Optional description"
                  placeholderTextColor={muted}
                  style={[styles.input, { color: text, backgroundColor: inputBg, borderColor: border }]}
                />

                {/* Environment Tabs */}
                <View style={[styles.envTabs, { marginTop: 16 }]}>
                  <TouchableOpacity
                    style={[styles.envTab, formTab === 'test' && styles.envTabActive]}
                    onPress={() => { setFormTab('test'); setTestResult(null); }}
                  >
                    <Ionicons name="flask-outline" size={14} color={formTab === 'test' ? '#3b82f6' : muted} />
                    <Text style={{ color: formTab === 'test' ? '#3b82f6' : muted, fontWeight: '600', fontSize: 13 }}>
                      Test
                    </Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.envTab, formTab === 'prod' && styles.envTabActiveProd]}
                    onPress={() => { setFormTab('prod'); setTestResult(null); }}
                  >
                    <Ionicons name="rocket-outline" size={14} color={formTab === 'prod' ? '#22c55e' : muted} />
                    <Text style={{ color: formTab === 'prod' ? '#22c55e' : muted, fontWeight: '600', fontSize: 13 }}>
                      Production
                    </Text>
                  </TouchableOpacity>
                </View>

                {/* Environment-specific fields */}
                <View style={[styles.envBox, { borderColor: formTab === 'test' ? '#3b82f640' : '#22c55e40', backgroundColor: inputBg }]}>
                  {editing && (
                    <View style={styles.keepSecretNote}>
                      <Ionicons name="lock-closed-outline" size={12} color={muted} />
                      <Text style={{ color: muted, fontSize: 11, flex: 1 }}>
                        Saved credentials are hidden. Leave a secret field blank to keep its current value; type a new value to replace it.
                      </Text>
                    </View>
                  )}
                  {(formType === 'sql' || formType === 'mongo') && (
                    <>
                      <Text style={[styles.label, { color: text }]}>Connection String</Text>
                      <TextInput
                        value={activeEnv.connection_string}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, connection_string: t }))}
                        placeholder={formType === 'sql'
                          ? 'mssql+pyodbc://user:pass@host/db?driver=...'
                          : 'mongodb+srv://user:pass@cluster/db  or  cosmosdb://...'}
                        placeholderTextColor={muted}
                        secureTextEntry
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                    </>
                  )}

                  {formType === 'mongo' && (
                    <>
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Database Name</Text>
                      <TextInput
                        value={activeEnv.database}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, database: t }))}
                        placeholder="mydb"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                    </>
                  )}

                  {formType === 'api' && (
                    <>
                      <Text style={[styles.label, { color: text }]}>Base URL</Text>
                      <TextInput
                        value={activeEnv.url}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, url: t }))}
                        placeholder="https://api.example.com"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Headers (JSON)</Text>
                      <TextInput
                        value={activeEnv.headers}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, headers: t }))}
                        placeholder='{"Authorization": "Bearer ..."}'
                        placeholderTextColor={muted}
                        multiline
                        numberOfLines={3}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border, minHeight: 60, textAlignVertical: 'top' }]}
                      />
                    </>
                  )}

                  {formType === 'sftp' && (
                    <>
                      <Text style={[styles.label, { color: text }]}>Protocol</Text>
                      <View style={{ flexDirection: 'row', gap: 8, marginBottom: 8 }}>
                        {[{ l: 'SFTP', v: 'sftp' }, { l: 'FTP', v: 'ftp' }, { l: 'FTPS', v: 'ftps' }].map(opt => (
                          <TouchableOpacity
                            key={opt.v}
                            style={[styles.protocolBtn, activeEnv.protocol === opt.v && styles.protocolBtnActive, { borderColor: border }]}
                            onPress={() => setActiveEnv(prev => ({ ...prev, protocol: opt.v, port: opt.v === 'sftp' ? '22' : '21' }))}
                          >
                            <Text style={{ color: activeEnv.protocol === opt.v ? '#3b82f6' : muted, fontWeight: '600', fontSize: 12 }}>{opt.l}</Text>
                          </TouchableOpacity>
                        ))}
                      </View>
                      <View style={{ flexDirection: 'row', gap: 8 }}>
                        <View style={{ flex: 3 }}>
                          <Text style={[styles.label, { color: text }]}>Host</Text>
                          <TextInput
                            value={activeEnv.host}
                            onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, host: t }))}
                            placeholder="sftp.example.com"
                            placeholderTextColor={muted}
                            style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                          />
                        </View>
                        <View style={{ flex: 1 }}>
                          <Text style={[styles.label, { color: text }]}>Port</Text>
                          <TextInput
                            value={activeEnv.port}
                            onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, port: t }))}
                            placeholder="22"
                            placeholderTextColor={muted}
                            keyboardType="numeric"
                            style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                          />
                        </View>
                      </View>
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Username</Text>
                      <TextInput
                        value={activeEnv.username}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, username: t }))}
                        placeholder="myuser"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Password</Text>
                      <TextInput
                        value={activeEnv.password}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, password: t }))}
                        placeholder="(leave blank for key-based auth)"
                        placeholderTextColor={muted}
                        secureTextEntry
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      {activeEnv.protocol === 'sftp' && (
                        <>
                          <Text style={[styles.label, { color: text, marginTop: 8 }]}>Private Key (PEM)</Text>
                          <TextInput
                            value={activeEnv.private_key}
                            onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, private_key: t }))}
                            placeholder="-----BEGIN RSA PRIVATE KEY-----"
                            placeholderTextColor={muted}
                            multiline
                            numberOfLines={4}
                            style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border, minHeight: 80, textAlignVertical: 'top', fontFamily: Platform.OS === 'web' ? 'monospace' : 'Courier' }]}
                          />
                        </>
                      )}
                    </>
                  )}

                  {formType === 'bucket' && (
                    <>
                      <Text style={[styles.label, { color: text }]}>Bucket Name</Text>
                      <TextInput
                        value={activeEnv.bucket}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, bucket: t }))}
                        placeholder="my-bucket"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>AWS Region</Text>
                      <TextInput
                        value={activeEnv.region}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, region: t }))}
                        placeholder="us-east-1"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Access Key ID</Text>
                      <TextInput
                        value={activeEnv.access_key_id}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, access_key_id: t }))}
                        placeholder="AKIA..."
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Secret Access Key</Text>
                      <TextInput
                        value={activeEnv.secret_access_key}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, secret_access_key: t }))}
                        placeholder="wJalrXUtnFEMI/K7MDENG/..."
                        placeholderTextColor={muted}
                        secureTextEntry
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Endpoint URL (optional, for S3-compatible)</Text>
                      <TextInput
                        value={activeEnv.endpoint_url}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, endpoint_url: t }))}
                        placeholder="https://s3.example.com (leave blank for AWS)"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                    </>
                  )}

                  {formType === 'smtp' && (
                    <>
                      <Text style={[styles.label, { color: text }]}>SMTP Host</Text>
                      <TextInput
                        value={activeEnv.host}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, host: t }))}
                        placeholder="smtp.gmail.com"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>SMTP Port</Text>
                      <TextInput
                        value={activeEnv.smtp_port}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, smtp_port: t }))}
                        placeholder="587"
                        placeholderTextColor={muted}
                        keyboardType="numeric"
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Username</Text>
                      <TextInput
                        value={activeEnv.username}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, username: t }))}
                        placeholder="you@gmail.com"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>Password / App Password</Text>
                      <TextInput
                        value={activeEnv.password}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, password: t }))}
                        placeholder="Gmail app password or SMTP password"
                        placeholderTextColor={muted}
                        secureTextEntry
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                      <Text style={[styles.label, { color: text, marginTop: 8 }]}>From Address (optional)</Text>
                      <TextInput
                        value={activeEnv.from_address}
                        onChangeText={(t) => setActiveEnv((prev) => ({ ...prev, from_address: t }))}
                        placeholder="you@gmail.com (defaults to username)"
                        placeholderTextColor={muted}
                        style={[styles.input, { color: text, backgroundColor: cardBg, borderColor: border }]}
                      />
                    </>
                  )}

                  {/* Test Connection Button */}
                  <TouchableOpacity
                    style={[styles.testBtn, testing && { opacity: 0.5 }]}
                    onPress={handleTest}
                    disabled={testing}
                  >
                    {testing ? (
                      <ActivityIndicator size="small" color="#3b82f6" />
                    ) : (
                      <Ionicons name="flash-outline" size={14} color="#3b82f6" />
                    )}
                    <Text style={{ color: '#3b82f6', fontWeight: '600', fontSize: 12 }}>
                      Verify {formTab === 'test' ? 'Test' : 'Prod'} Connection
                    </Text>
                  </TouchableOpacity>

                  {/* Test Result */}
                  {testResult && (
                    <View style={[
                      styles.testResultBox,
                      { backgroundColor: testResult.success ? '#22c55e15' : '#ef444415' },
                    ]}>
                      <Ionicons
                        name={testResult.success ? 'checkmark-circle' : 'close-circle'}
                        size={16}
                        color={testResult.success ? '#22c55e' : '#ef4444'}
                      />
                      <Text style={{ color: testResult.success ? '#22c55e' : '#ef4444', fontSize: 12, flex: 1 }}>
                        {testResult.success ? 'Connected successfully' : (testResult.message || 'Connection failed')}
                      </Text>
                    </View>
                  )}
                </View>

                {/* App-level error banner (replaces window.alert) */}
                {formError ? (
                  <View style={styles.errorBox}>
                    <Ionicons name="alert-circle" size={16} color="#ef4444" />
                    <Text style={{ color: '#ef4444', fontSize: 12, flex: 1 }}>{formError}</Text>
                  </View>
                ) : null}

                {/* Action buttons */}
                <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 10, marginTop: 16 }}>
                  <TouchableOpacity
                    onPress={() => setShowForm(false)}
                    style={[styles.cancelBtn, { borderColor: border }]}
                  >
                    <Text style={{ color: text }}>Cancel</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.saveBtn, saving && { opacity: 0.5 }]}
                    onPress={handleSave}
                    disabled={saving}
                  >
                    {saving ? (
                      <ActivityIndicator size="small" color="#fff" />
                    ) : (
                      <Text style={{ color: '#fff', fontWeight: '600' }}>
                        {editing ? 'Update' : 'Create'}
                      </Text>
                    )}
                  </TouchableOpacity>
                </View>
              </ScrollView>
            </View>
          </View>
        </Modal>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 1,
    gap: 12,
  },
  backBtn: { padding: 6 },
  heading: { fontSize: 18, fontWeight: '700' },
  createBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    backgroundColor: '#3b82f6',
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 8,
  },
  createBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  list: { padding: 16, gap: 10 },
  card: {
    borderWidth: 1,
    borderRadius: 12,
    padding: 14,
  },
  cardRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
  },
  cardName: { fontSize: 14, fontWeight: '600' },
  envBadge: {
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
  },
  overlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.5)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  formCard: {
    width: '95%',
    maxWidth: 500,
    maxHeight: '85%',
    borderRadius: 16,
    padding: 24,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.15,
    shadowRadius: 24,
    elevation: 10,
  },
  formTitle: { fontSize: 18, fontWeight: '700' },
  label: { fontSize: 12, fontWeight: '600', marginBottom: 4 },
  input: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
    marginBottom: 4,
  },
  typeChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    borderWidth: 1.5,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  envTabs: {
    flexDirection: 'row',
    gap: 0,
    borderRadius: 8,
    overflow: 'hidden',
    marginBottom: 0,
  },
  envTab: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    paddingVertical: 8,
    borderBottomWidth: 2,
    borderBottomColor: 'transparent',
  },
  envTabActive: { borderBottomColor: '#3b82f6' },
  envTabActiveProd: { borderBottomColor: '#22c55e' },
  envBox: {
    borderWidth: 1,
    borderRadius: 10,
    padding: 14,
    marginTop: 8,
  },
  keepSecretNote: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    marginBottom: 10,
  },
  testBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    marginTop: 10,
    paddingVertical: 6,
  },
  protocolBtn: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 6,
    borderWidth: 1,
  },
  protocolBtnActive: {
    backgroundColor: '#3b82f620',
    borderColor: '#3b82f6',
  },
  testResultBox: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    marginTop: 8,
    padding: 8,
    borderRadius: 6,
  },
  errorBox: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    marginTop: 16,
    padding: 10,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#ef444440',
    backgroundColor: '#ef444415',
  },
  cancelBtn: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    borderWidth: 1,
  },
  saveBtn: {
    paddingHorizontal: 20,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: '#3b82f6',
  },
});
