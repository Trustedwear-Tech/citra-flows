// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * NodeConfigPanel.js — Right-side panel for editing a selected node's configuration
 */
import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TextInput, TouchableOpacity, ScrollView, StyleSheet, Switch, Platform } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';
import CodeEditorField, { pickFieldEditor } from './CodeEditorField';

export default function NodeConfigPanel({ node, schemas = [], theme, onUpdateConfig, onUpdateLabel, onClose }) {
  const isDark = theme?.isDark;
  const bg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const inputBg = isDark ? '#0f172a' : '#f1f5f9';

  const schema = schemas.find((s) => s.type === node?.data?.nodeType);
  const fields = schema?.fields || [];
  const [localConfig, setLocalConfig] = useState({});
  const [localLabel, setLocalLabel] = useState('');

  useEffect(() => {
    setLocalConfig(node?.data?.config || {});
    setLocalLabel(node?.data?.label || '');
  }, [node?.id, node?.data?.config]);

  const handleChange = useCallback(
    (fieldName, value) => {
      const next = { ...localConfig, [fieldName]: value };
      setLocalConfig(next);
      onUpdateConfig(next);
    },
    [localConfig, onUpdateConfig]
  );

  if (!node) return null;

  return (
    <View
      style={[styles.container, { backgroundColor: bg, borderLeftColor: border }]}
      onStartShouldSetResponder={() => true}
      {...(Platform.OS === 'web' ? {
        onKeyDown: (e) => e.stopPropagation(),
        onKeyUp: (e) => e.stopPropagation(),
      } : {})}
    >
      {/* Header — node label is editable */}
      <View style={[styles.header, { borderBottomColor: border }]}>
        <View style={{ flex: 1 }}>
          <TextInput
            value={localLabel}
            onChangeText={(t) => {
              setLocalLabel(t);
              onUpdateLabel?.(t);
            }}
            placeholder="Node label"
            placeholderTextColor={muted}
            style={[styles.titleInput, { color: text, borderColor: border, backgroundColor: inputBg }]}
          />
          <Text style={[styles.subtitle, { color: muted }]}>{schema?.description || node.data.nodeType}</Text>
        </View>
        <TouchableOpacity onPress={onClose}>
          <Ionicons name="close" size={20} color={muted} />
        </TouchableOpacity>
      </View>

      {/* Fields */}
      <ScrollView style={styles.fields} showsVerticalScrollIndicator={false}>
        {fields.map((field) => (
          <FieldRenderer
            key={field.name}
            field={field}
            value={localConfig[field.name]}
            allValues={localConfig}
            onChange={(val) => handleChange(field.name, val)}
            isDark={isDark}
            text={text}
            muted={muted}
            border={border}
            inputBg={inputBg}
            nodeType={node?.data?.nodeType}
          />
        ))}

        {fields.length === 0 && (
          <Text style={[styles.emptyText, { color: muted }]}>
            No configuration options for this node.
          </Text>
        )}
      </ScrollView>
    </View>
  );
}

function FieldRenderer({ field, value, allValues, onChange, isDark, text, muted, border, inputBg, nodeType }) {
  const { type, name, label, placeholder, help_text, options, required, visible_when } = field;

  // Conditional visibility — hide field when condition is not met
  if (visible_when) {
    const depField = visible_when.field;
    const depValue = allValues?.[depField];
    const operator = visible_when.operator;
    const expected = visible_when.value;
    const actualNumber = Number(depValue ?? 0);
    const expectedNumber = Number(expected);

    if (operator === '>') {
      if (!(actualNumber > expectedNumber)) return null;
    } else if (operator === '>=') {
      if (!(actualNumber >= expectedNumber)) return null;
    } else if (operator === '<') {
      if (!(actualNumber < expectedNumber)) return null;
    } else if (operator === '<=') {
      if (!(actualNumber <= expectedNumber)) return null;
    } else if (operator === '!=' || operator === '!==') {
      if (String(depValue) === String(expected)) return null;
    } else {
      if (visible_when.value !== undefined) {
        if (String(depValue) !== String(visible_when.value)) return null;
      }
      if (visible_when.values && Array.isArray(visible_when.values)) {
        if (!visible_when.values.map(String).includes(String(depValue))) return null;
      }
    }
  }

  const labelEl = (
    <View style={styles.fieldLabelRow}>
      <Text style={[styles.fieldLabel, { color: text }]}>
        {label || name}
        {required && <Text style={{ color: '#ef4444' }}> *</Text>}
      </Text>
    </View>
  );

  switch (type) {
    case 'boolean':
      return (
        <View style={styles.fieldWrap}>
          <View style={styles.boolRow}>
            <Text style={[styles.fieldLabel, { color: text, flex: 1 }]}>{label || name}</Text>
            <Switch value={!!value} onValueChange={onChange} />
          </View>
          {help_text && <Text style={[styles.helpText, { color: muted }]}>{help_text}</Text>}
        </View>
      );

    case 'select':
      return (
        <View style={styles.fieldWrap}>
          {labelEl}
          <View style={[styles.selectWrap, { borderColor: border, backgroundColor: inputBg }]}>
            {Platform.OS === 'web' ? (
              <select
                value={value || ''}
                onChange={(e) => onChange(e.target.value)}
                style={{
                  width: '100%',
                  padding: '6px 8px',
                  border: 'none',
                  background: 'transparent',
                  color: isDark ? '#e2e8f0' : '#1e293b',
                  fontSize: 13,
                  outline: 'none',
                }}
              >
                <option value="">Select…</option>
                {(options || []).map((opt) => {
                  const optValue = typeof opt === 'object' ? opt.value : opt;
                  const optLabel = typeof opt === 'object' ? opt.label : opt;
                  return (
                    <option key={optValue} value={optValue}>
                      {optLabel}
                    </option>
                  );
                })}
              </select>
            ) : (
              <TextInput
                value={value || ''}
                onChangeText={onChange}
                placeholder={`One of: ${(options || []).map(o => typeof o === 'object' ? o.label : o).join(', ')}`}
                placeholderTextColor={muted}
                style={[styles.input, { color: text }]}
              />
            )}
          </View>
          {help_text && <Text style={[styles.helpText, { color: muted }]}>{help_text}</Text>}
        </View>
      );

    case 'textarea':
    case 'json': {
      // Centralized editor selection (pickFieldEditor): chooses python / json /
      // html / sql / template / text per the field schema + value shape, and
      // the JSON save-mode. type === 'json' → object-mode (parse + save object);
      // textarea JSON-ish text → string-mode (highlight but save the string).
      // Single-line text inputs are handled by the default case, untouched.
      const editor = pickFieldEditor(field, value, nodeType) || { language: 'text' };
      return (
        <View style={styles.fieldWrap}>
          {labelEl}
          <CodeEditorField
            value={value}
            onChange={onChange}
            language={editor.language}
            jsonSave={editor.jsonSave || 'object'}
            fieldLabel={label || name}
            placeholder={placeholder || ''}
            isDark={isDark}
            text={text}
            muted={muted}
            border={border}
            inputBg={inputBg}
          />
          {nodeType === 'code_block' && name === 'code' && (
            <AICodeGenerator onChange={onChange} isDark={isDark} text={text} muted={muted} border={border} inputBg={inputBg} />
          )}
          {help_text && <Text style={[styles.helpText, { color: muted }]}>{help_text}</Text>}
        </View>
      );
    }

    case 'number':
      return (
        <View style={styles.fieldWrap}>
          {labelEl}
          <TextInput
            value={value != null ? String(value) : ''}
            onChangeText={(t) => onChange(t === '' ? null : Number(t))}
            placeholder={placeholder || '0'}
            placeholderTextColor={muted}
            keyboardType="numeric"
            style={[styles.input, { color: text, borderColor: border, backgroundColor: inputBg }]}
          />
          {help_text && <Text style={[styles.helpText, { color: muted }]}>{help_text}</Text>}
        </View>
      );

    case 'password':
      return (
        <View style={styles.fieldWrap}>
          {labelEl}
          <TextInput
            value={value || ''}
            onChangeText={onChange}
            placeholder={placeholder || '••••••••'}
            placeholderTextColor={muted}
            secureTextEntry
            style={[styles.input, { color: text, borderColor: border, backgroundColor: inputBg }]}
          />
          {help_text && <Text style={[styles.helpText, { color: muted }]}>{help_text}</Text>}
        </View>
      );

    case 'tool_picker':
      return (
        <ToolPickerField
          field={field}
          value={value}
          onChange={onChange}
          isDark={isDark}
          text={text}
          muted={muted}
          border={border}
          inputBg={inputBg}
          labelEl={labelEl}
        />
      );

    case 'schema_builder':
      return (
        <SchemaBuilderField
          field={field}
          value={value}
          onChange={onChange}
          isDark={isDark}
          text={text}
          muted={muted}
          border={border}
          inputBg={inputBg}
          labelEl={labelEl}
        />
      );

    case 'connection_picker':
      return (
        <ConnectionPickerField
          field={field}
          value={value}
          onChange={onChange}
          isDark={isDark}
          text={text}
          muted={muted}
          border={border}
          inputBg={inputBg}
          labelEl={labelEl}
        />
      );

    case 'variable_assignments':
      return (
        <VariableAssignmentsField
          field={field}
          value={value}
          onChange={onChange}
          isDark={isDark}
          text={text}
          muted={muted}
          border={border}
          inputBg={inputBg}
          labelEl={labelEl}
        />
      );

    default: // text, cron
      return (
        <View style={styles.fieldWrap}>
          {labelEl}
          <TextInput
            value={value || ''}
            onChangeText={onChange}
            placeholder={placeholder || ''}
            placeholderTextColor={muted}
            style={[styles.input, { color: text, borderColor: border, backgroundColor: inputBg }]}
          />
          {help_text && <Text style={[styles.helpText, { color: muted }]}>{help_text}</Text>}
        </View>
      );
  }
}

// ── AI Code Generator (for code_block textarea) ──────────────────
function AICodeGenerator({ onChange, isDark, text, muted, border, inputBg }) {
  const [expanded, setExpanded] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleGenerate = async () => {
    if (!prompt.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await WorkflowService.generateCode(prompt.trim());
      onChange(resp.code);
      setPrompt('');
      setExpanded(false);
    } catch (e) {
      setError(e.message || 'Generation failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <View style={{ marginTop: 8 }}>
      <TouchableOpacity
        onPress={() => setExpanded(!expanded)}
        style={{ flexDirection: 'row', alignItems: 'center', gap: 6, paddingVertical: 4 }}
      >
        <Text style={{ fontSize: 12, color: '#8b5cf6', fontWeight: '600' }}>
          ✨ Generate with AI
        </Text>
        <Ionicons name={expanded ? 'chevron-up' : 'chevron-down'} size={14} color="#8b5cf6" />
      </TouchableOpacity>
      {expanded && (
        <View style={{ marginTop: 6 }}>
          <TextInput
            value={prompt}
            onChangeText={setPrompt}
            placeholder="Describe what the code should do…"
            placeholderTextColor={muted}
            multiline
            numberOfLines={3}
            style={[
              styles.textarea,
              { color: text, borderColor: border, backgroundColor: inputBg, minHeight: 60 },
            ]}
          />
          <TouchableOpacity
            onPress={handleGenerate}
            disabled={loading || !prompt.trim()}
            style={{
              marginTop: 8,
              backgroundColor: loading || !prompt.trim() ? (isDark ? '#334155' : '#cbd5e1') : '#8b5cf6',
              paddingVertical: 8,
              borderRadius: 8,
              alignItems: 'center',
            }}
          >
            <Text style={{ color: '#fff', fontSize: 12, fontWeight: '700' }}>
              {loading ? 'Generating…' : 'Generate'}
            </Text>
          </TouchableOpacity>
          {error && (
            <Text style={{ color: '#ef4444', fontSize: 11, marginTop: 4 }}>{error}</Text>
          )}
        </View>
      )}
    </View>
  );
}

// ── Tool Picker (checkbox list of available agent tools) ──────────
function ToolPickerField({ field, value, onChange, isDark, text, muted, border, inputBg, labelEl }) {
  const [tools, setTools] = useState([]);
  const selected = Array.isArray(value) ? value : [];

  useEffect(() => {
    WorkflowService.getAgentTools()
      .then((res) => setTools(res.tools || []))
      .catch(() => setTools([]));
  }, []);

  const toggle = (toolName) => {
    const next = selected.includes(toolName)
      ? selected.filter((t) => t !== toolName)
      : [...selected, toolName];
    onChange(next);
  };

  return (
    <View style={styles.fieldWrap}>
      {labelEl}
      {tools.map((tool) => (
        <TouchableOpacity
          key={tool.name}
          onPress={() => toggle(tool.name)}
          style={[
            styles.toolPickerItem,
            {
              backgroundColor: selected.includes(tool.name) ? (isDark ? '#312e81' : '#ede9fe') : inputBg,
              borderColor: selected.includes(tool.name) ? '#8b5cf6' : border,
            },
          ]}
        >
          <Ionicons
            name={selected.includes(tool.name) ? 'checkbox' : 'square-outline'}
            size={18}
            color={selected.includes(tool.name) ? '#8b5cf6' : muted}
          />
          <Text style={{ fontSize: 15 }}>{tool.icon || '🔧'}</Text>
          <View style={{ flex: 1 }}>
            <Text style={[styles.toolPickerLabel, { color: text }]}>{tool.label}</Text>
            <Text style={{ fontSize: 10, color: muted }} numberOfLines={1}>{tool.description}</Text>
          </View>
        </TouchableOpacity>
      ))}
      {field.help_text && <Text style={[styles.helpText, { color: muted }]}>{field.help_text}</Text>}
    </View>
  );
}

// ── Connection Picker (dropdown of saved connections + "Manage" link) ──
function ConnectionPickerField({ field, value, onChange, isDark, text, muted, border, inputBg, labelEl }) {
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const connType = field.connection_type || '';

  useEffect(() => {
    setLoading(true);
    WorkflowService.listConnections(connType || null)
      .then((data) => setConnections(data?.connections || []))
      .catch(() => setConnections([]))
      .finally(() => setLoading(false));
  }, [connType]);

  if (loading) {
    return (
      <View style={styles.fieldWrap}>
        {labelEl}
        <Text style={{ fontSize: 11, color: muted }}>Loading connections…</Text>
      </View>
    );
  }

  return (
    <View style={styles.fieldWrap}>
      {labelEl}
      <View style={[styles.selectWrap, { borderColor: border, backgroundColor: inputBg }]}>
        {Platform.OS === 'web' ? (
          <select
            value={value || ''}
            onChange={(e) => onChange(e.target.value || null)}
            style={{
              width: '100%',
              padding: '6px 8px',
              border: 'none',
              background: 'transparent',
              color: isDark ? '#e2e8f0' : '#1e293b',
              fontSize: 13,
              outline: 'none',
            }}
          >
            <option value="">No saved connection (use inline)</option>
            {connections.map((c) => (
              <option key={c.connection_id} value={c.connection_id}>
                {c.name} ({c.type})
              </option>
            ))}
          </select>
        ) : (
          <TextInput
            value={connections.find(c => c.connection_id === value)?.name || value || ''}
            onChangeText={onChange}
            placeholder="Enter connection ID"
            placeholderTextColor={muted}
            style={[styles.input, { color: text }]}
          />
        )}
      </View>
      {field.help_text && <Text style={[styles.helpText, { color: muted }]}>{field.help_text}</Text>}
    </View>
  );
}

// ── Schema Builder (add/remove input fields with name, type, required) ──
function SchemaBuilderField({ field, value, onChange, isDark, text, muted, border, inputBg, labelEl }) {
  const fields = Array.isArray(value) ? value : [];
  const TYPES = ['text', 'number', 'boolean', 'json'];

  const addField = () => {
    onChange([...fields, { name: '', type: 'text', required: false, default: '' }]);
  };

  const updateField = (index, key, val) => {
    const next = fields.map((f, i) => (i === index ? { ...f, [key]: val } : f));
    onChange(next);
  };

  const removeField = (index) => {
    onChange(fields.filter((_, i) => i !== index));
  };

  return (
    <View style={styles.fieldWrap}>
      {labelEl}
      {fields.map((f, i) => (
        <View
          key={i}
          style={[styles.schemaRow, { backgroundColor: inputBg, borderColor: border }]}
        >
          <View style={{ flexDirection: 'row', gap: 6, marginBottom: 6 }}>
            <TextInput
              value={f.name}
              onChangeText={(t) => updateField(i, 'name', t)}
              placeholder="Field name"
              placeholderTextColor={muted}
              style={[styles.schemaInput, { color: text, borderColor: border, flex: 1 }]}
            />
            {Platform.OS === 'web' ? (
              <select
                value={f.type || 'text'}
                onChange={(e) => updateField(i, 'type', e.target.value)}
                style={{
                  padding: '4px 6px', borderRadius: 6, border: `1px solid ${border}`,
                  background: 'transparent', color: isDark ? '#e2e8f0' : '#1e293b',
                  fontSize: 12, outline: 'none',
                }}
              >
                {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            ) : (
              <TextInput
                value={f.type || 'text'}
                onChangeText={(t) => updateField(i, 'type', t)}
                style={[styles.schemaInput, { color: text, borderColor: border, width: 60 }]}
              />
            )}
          </View>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <TouchableOpacity
              onPress={() => updateField(i, 'required', !f.required)}
              style={{ flexDirection: 'row', alignItems: 'center', gap: 4, flex: 1 }}
            >
              <Ionicons
                name={f.required ? 'checkbox' : 'square-outline'}
                size={16}
                color={f.required ? '#3b82f6' : muted}
              />
              <Text style={{ fontSize: 11, color: muted }}>Required</Text>
            </TouchableOpacity>
            <TextInput
              value={f.default || ''}
              onChangeText={(t) => updateField(i, 'default', t)}
              placeholder="Default"
              placeholderTextColor={muted}
              style={[styles.schemaInput, { color: text, borderColor: border, flex: 1, fontSize: 11 }]}
            />
            <TouchableOpacity onPress={() => removeField(i)}>
              <Ionicons name="close-circle" size={18} color="#ef4444" />
            </TouchableOpacity>
          </View>
        </View>
      ))}
      <TouchableOpacity onPress={addField} style={styles.schemaAddBtn}>
        <Ionicons name="add-circle-outline" size={16} color="#3b82f6" />
        <Text style={{ fontSize: 12, color: '#3b82f6', fontWeight: '600' }}>Add Field</Text>
      </TouchableOpacity>
      {field.help_text && <Text style={[styles.helpText, { color: muted }]}>{field.help_text}</Text>}
    </View>
  );
}

// ── Variable Assignments Editor (name/value table for Set Variable node) ──
function VariableAssignmentsField({ field, value, onChange, isDark, text, muted, border, inputBg, labelEl }) {
  const rows = Array.isArray(value) ? value : [];

  const addRow = () => {
    onChange([...rows, { name: '', value: '' }]);
  };

  const updateRow = (index, key, val) => {
    const next = rows.map((r, i) => (i === index ? { ...r, [key]: val } : r));
    onChange(next);
  };

  const removeRow = (index) => {
    onChange(rows.filter((_, i) => i !== index));
  };

  return (
    <View style={styles.fieldWrap}>
      {labelEl}
      {rows.map((row, i) => (
        <View
          key={i}
          style={[styles.schemaRow, { backgroundColor: inputBg, borderColor: border }]}
        >
          <View style={{ flexDirection: 'row', gap: 6, alignItems: 'center' }}>
            <TextInput
              value={row.name || ''}
              onChangeText={(t) => updateRow(i, 'name', t)}
              placeholder="Variable name"
              placeholderTextColor={muted}
              style={[styles.schemaInput, { color: text, borderColor: border, flex: 1 }]}
            />
            <Text style={{ color: muted, fontSize: 13, fontWeight: '700' }}>=</Text>
            <TextInput
              value={row.value != null ? String(row.value) : ''}
              onChangeText={(t) => updateRow(i, 'value', t)}
              placeholder="Value or {{var}}"
              placeholderTextColor={muted}
              style={[styles.schemaInput, { color: text, borderColor: border, flex: 2 }]}
            />
            <TouchableOpacity onPress={() => removeRow(i)}>
              <Ionicons name="close-circle" size={18} color="#ef4444" />
            </TouchableOpacity>
          </View>
        </View>
      ))}
      <TouchableOpacity onPress={addRow} style={styles.schemaAddBtn}>
        <Ionicons name="add-circle-outline" size={16} color="#8b5cf6" />
        <Text style={{ fontSize: 12, color: '#8b5cf6', fontWeight: '600' }}>Add Variable</Text>
      </TouchableOpacity>
      {field.help_text && <Text style={[styles.helpText, { color: muted }]}>{field.help_text}</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    minWidth: 0,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    padding: 14,
    borderBottomWidth: 1,
    gap: 8,
  },
  title: { fontSize: 15, fontWeight: '700' },
  titleInput: {
    fontSize: 15,
    fontWeight: '700',
    borderWidth: 1,
    borderRadius: 6,
    paddingHorizontal: 8,
    paddingVertical: 5,
    ...(Platform.OS === 'web' ? { outline: 'none' } : {}),
  },
  subtitle: { fontSize: 11, marginTop: 4 },
  fields: { flex: 1, padding: 14 },
  fieldWrap: { marginBottom: 16 },
  fieldLabelRow: { marginBottom: 6 },
  fieldLabel: { fontSize: 12, fontWeight: '600' },
  input: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
  },
  textarea: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
    minHeight: 100,
    textAlignVertical: 'top',
  },
  selectWrap: { borderWidth: 1, borderRadius: 8, overflow: 'hidden' },
  boolRow: { flexDirection: 'row', alignItems: 'center' },
  helpText: { fontSize: 10, marginTop: 4 },
  emptyText: { fontSize: 13, fontStyle: 'italic', textAlign: 'center', marginTop: 24 },
  // Tool picker
  toolPickerItem: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    padding: 8,
    borderRadius: 8,
    borderWidth: 1,
    marginBottom: 6,
  },
  toolPickerLabel: { fontSize: 12, fontWeight: '600' },
  // Schema builder
  schemaRow: {
    padding: 8,
    borderRadius: 8,
    borderWidth: 1,
    marginBottom: 6,
  },
  schemaInput: {
    borderWidth: 1,
    borderRadius: 6,
    paddingHorizontal: 8,
    paddingVertical: 4,
    fontSize: 12,
  },
  schemaAddBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingVertical: 6,
  },
});
