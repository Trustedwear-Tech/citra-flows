// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * CodeEditorField.js — rich editor for every multiline workflow config field.
 *
 *  • language 'python'/'json'/'html'/'sql' → CodeMirror 6 with the matching
 *    grammar (syntax highlight, line numbers, bracket matching).
 *  • language 'template' → CodeMirror plain editor that highlights {{var}} /
 *    {% %} placeholders.
 *  • language 'text' (or null) → CodeMirror plain editor (line numbers,
 *    readable monospace) for long text / lists / prose.
 *  • CodeMirror unavailable (native, or load failure) → enhanced fallback
 *    <TextInput> so nothing ever breaks.
 *  • Every variant offers a fullscreen "Maximize" modal.
 *
 * Readability: an explicit, self-contained light/dark theme is injected as
 * global CSS (CM_STYLE_ID) so code is legible WITHOUT selecting it — we do not
 * rely on inherited app text colors.
 *
 * Type-safety (must not corrupt node config):
 *   jsonSave='object'  (schema type === 'json')  → valid JSON saved as a parsed
 *                       object/array, invalid kept as raw string + inline error,
 *                       Format button available.
 *   jsonSave='string'  (textarea holding JSON-ish text) → ALWAYS saved as the
 *                       raw string. No type conversion, no Format, no errors.
 *   everything else    → saved as the raw string, unchanged.
 *   The text buffer re-syncs from `value` only on EXTERNAL changes (node switch,
 *   AI edit), never on our own round-trip, so content isn't reformatted while
 *   typing.
 */
import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, StyleSheet, Modal, Platform,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';

const isWeb = Platform.OS === 'web';

// ── CodeMirror (web only, guarded) ───────────────────────────────
let ReactCodeMirror = null;
let cmEditorView = null;
let cmView = null;
let cmPython = null;
let cmJson = null;
let cmHtml = null;
let cmSql = null;
if (isWeb) {
  try {
    const mod = require('@uiw/react-codemirror');
    ReactCodeMirror = mod.default || mod.CodeMirror || null;
    cmEditorView = mod.EditorView || null;
  } catch { /* fall back to textarea */ }
  try { cmView = require('@codemirror/view'); } catch { /* no decoration support */ }
  try { cmPython = require('@codemirror/lang-python').python; } catch { /* no python pack */ }
  try { cmJson = require('@codemirror/lang-json').json; } catch { /* no json pack */ }
  try { cmHtml = require('@codemirror/lang-html').html; } catch { /* no html pack */ }
  try { cmSql = require('@codemirror/lang-sql').sql; } catch { /* no sql pack */ }
}

// {{var}} / {% %} highlighter for template editors (best-effort; preserves text
// regardless). Falls back to no decoration when @codemirror/view is absent.
const TEMPLATE_HIGHLIGHT = (() => {
  try {
    if (!cmView?.Decoration || !cmView?.ViewPlugin || !cmView?.MatchDecorator) return null;
    const deco = cmView.Decoration.mark({ class: 'cm-template-var' });
    const matcher = new cmView.MatchDecorator({
      regexp: /\{\{[^}]*\}\}|\{%[^%]*%\}/g,
      decoration: deco,
    });
    return cmView.ViewPlugin.define(
      (view) => ({
        decorations: matcher.createDeco(view),
        update(u) { this.decorations = matcher.updateDeco(u, this.decorations); },
      }),
      { decorations: (v) => v.decorations },
    );
  } catch { return null; }
})();

// ── Explicit, self-contained CodeMirror theme (injected once) ────
const CM_STYLE_ID = 'cm-citra-theme';
function injectCodeMirrorStyles() {
  if (!isWeb || typeof document === 'undefined') return;
  if (document.getElementById(CM_STYLE_ID)) return;
  const css = `
  .cm-citra-light .cm-editor, .cm-citra-light .cm-scroller, .cm-citra-light .cm-gutters { background: #ffffff !important; }
  .cm-citra-light .cm-content, .cm-citra-light .cm-line { color: #1e293b !important; }
  .cm-citra-light .cm-content { caret-color: #2563eb !important; }
  .cm-citra-light .cm-gutters { color: #94a3b8 !important; border-right: 1px solid #e2e8f0 !important; }
  .cm-citra-light .cm-gutterElement { color: #94a3b8 !important; }
  .cm-citra-light .cm-activeLine { background: #eff6ff !important; }
  .cm-citra-light .cm-activeLineGutter { background: #e0e7ff !important; color: #475569 !important; }
  .cm-citra-light .cm-cursor, .cm-citra-light .cm-dropCursor { border-left: 2px solid #2563eb !important; }
  .cm-citra-light .cm-selectionBackground,
  .cm-citra-light.cm-focused .cm-selectionBackground,
  .cm-citra-light .cm-content ::selection { background: #bfdbfe !important; }
  .cm-citra-light .cm-matchingBracket { background: #c7d2fe !important; outline: 1px solid #6366f1 !important; }
  .cm-citra-light .cm-template-var { color: #7c3aed !important; background: #f5f3ff !important; border-radius: 3px; padding: 0 1px; }

  .cm-citra-dark .cm-editor, .cm-citra-dark .cm-scroller, .cm-citra-dark .cm-gutters { background: #0b1220 !important; }
  .cm-citra-dark .cm-content, .cm-citra-dark .cm-line { color: #e2e8f0 !important; }
  .cm-citra-dark .cm-content { caret-color: #60a5fa !important; }
  .cm-citra-dark .cm-gutters { color: #64748b !important; border-right: 1px solid #334155 !important; }
  .cm-citra-dark .cm-gutterElement { color: #64748b !important; }
  .cm-citra-dark .cm-activeLine { background: #111c33 !important; }
  .cm-citra-dark .cm-activeLineGutter { background: #1e293b !important; color: #cbd5e1 !important; }
  .cm-citra-dark .cm-cursor, .cm-citra-dark .cm-dropCursor { border-left: 2px solid #60a5fa !important; }
  .cm-citra-dark .cm-selectionBackground,
  .cm-citra-dark.cm-focused .cm-selectionBackground,
  .cm-citra-dark .cm-content ::selection { background: #334155 !important; }
  .cm-citra-dark .cm-template-var { color: #c4b5fd !important; background: #312e81 !important; border-radius: 3px; padding: 0 1px; }

  .cm-citra-light .cm-editor, .cm-citra-dark .cm-editor { border-radius: 8px; }
  .cm-citra-light .cm-scroller, .cm-citra-dark .cm-scroller {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace !important;
    font-size: 13px !important; line-height: 1.55 !important;
  }
  .cm-citra-light .cm-content, .cm-citra-dark .cm-content { padding: 8px 0 !important; }
  .cm-citra-light .cm-line, .cm-citra-dark .cm-line { padding: 0 10px !important; }
  .cm-citra-light .cm-editor.cm-focused, .cm-citra-dark .cm-editor.cm-focused { outline: none !important; }
  `;
  const el = document.createElement('style');
  el.id = CM_STYLE_ID;
  el.textContent = css;
  document.head.appendChild(el);
}
injectCodeMirrorStyles();

const safeStringify = (v) => {
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
};
const toText = (value) => {
  if (value == null) return '';
  if (typeof value === 'object') return safeStringify(value);
  return String(value);
};

const CODE_LANGS = new Set(['python', 'json', 'html', 'sql']);
const CHIP_LABEL = { python: 'Python', json: 'JSON', html: 'HTML', sql: 'SQL', template: 'Template' };
const LANG_TITLE = { python: 'Python editor', json: 'JSON editor', html: 'HTML editor', sql: 'SQL editor' };

// Strip a trailing "(JSON)" / "(PEM)" / "(JSON Template)" qualifier from a label.
const cleanLabel = (s) => String(s || '').replace(/\s*\([^)]*\)\s*$/, '').trim();

// ── Centralized editor resolver ──────────────────────────────────
// Given a node field schema + its current value, decide which editor to use.
// Returns null for fields that should NOT use this editor (handled elsewhere).
export function pickFieldEditor(field, value, nodeType) {
  const type = field?.type;
  const name = String(field?.name || '').toLowerCase();
  const labelLc = String(field?.label || '').toLowerCase();
  const valStr = typeof value === 'string' ? value : (value != null ? toText(value) : '');
  const trimmed = valStr.replace(/^\s+/, '');

  // Schema explicitly expects JSON → object-mode (parse + save object).
  if (type === 'json') return { language: 'json', jsonSave: 'object' };

  // Only multiline text fields get the rich editor; single-line inputs unchanged.
  if (type !== 'textarea') return null;

  // Python code
  if ((nodeType === 'code_block' && name === 'code') || name === 'code' || /\bpython\b/.test(labelLc)) {
    return { language: 'python' };
  }
  // HTML / markup (by content or by name/label)
  if (/<!doctype|<html|<head|<body|<table|<div|<p[\s>]|<span|<style/i.test(valStr)
      || /\bhtml\b/.test(name) || /\bhtml\b/.test(labelLc) || /\bmarkup\b/.test(labelLc)) {
    return { language: 'html' };
  }
  // SQL query
  if (/\bsql\b/.test(name) || /\bsql\b/.test(labelLc)
      || (name === 'query' && /sql/.test(String(nodeType || '')))) {
    return { language: 'sql' };
  }
  // Explicit JSON-by-name/label, stored as a STRING (e.g. "Payload Template
  // (JSON)"). Highlight as JSON but ALWAYS save the raw string.
  if (/\bjson\b/.test(name) || /\bjson\b/.test(labelLc)) {
    return { language: 'json', jsonSave: 'string' };
  }
  // Template text with {{var}} / {% %} placeholders — checked before the
  // value-shape JSON heuristic so "{{email_body}}" isn't mistaken for JSON.
  if (/\{\{|\{%/.test(valStr)) return { language: 'template' };
  // JSON-ish by value shape (a real JSON object/array, no template markers).
  if (/^[[{]/.test(trimmed)) return { language: 'json', jsonSave: 'string' };

  // Plain long text (prompts, rules, labels, lists, descriptions, PEM keys…).
  return { language: 'text' };
}

export default function CodeEditorField({
  value,
  onChange,
  language = 'text',          // python | json | html | sql | template | text
  jsonSave = 'object',        // 'object' | 'string' (only used when language === 'json')
  fieldLabel = '',
  placeholder = '',
  isDark,
  text: textColor,
  muted,
  border,
  inputBg,
  minHeight,
}) {
  const [buffer, setBuffer] = useState(() => toText(value));
  const [jsonError, setJsonError] = useState(null);
  const [maximized, setMaximized] = useState(false);
  const lastEmitted = useRef(value);

  const jsonObjectMode = language === 'json' && jsonSave === 'object';

  useEffect(() => {
    if (value === lastEmitted.current) return;
    lastEmitted.current = value;
    setBuffer(toText(value));
    setJsonError(null);
  }, [value]);

  const emit = useCallback((nextText) => {
    setBuffer(nextText);
    if (jsonObjectMode) {
      const trimmed = (nextText || '').trim();
      if (trimmed === '') {
        lastEmitted.current = '';
        setJsonError(null);
        onChange('');
        return;
      }
      try {
        const parsed = JSON.parse(nextText);
        lastEmitted.current = parsed;
        setJsonError(null);
        onChange(parsed);
      } catch (e) {
        lastEmitted.current = nextText;
        setJsonError(e.message || 'Invalid JSON');
        onChange(nextText);
      }
      return;
    }
    // Every other mode (json-string, template, text, code) saves the raw string.
    lastEmitted.current = nextText;
    onChange(nextText);
  }, [jsonObjectMode, onChange]);

  const formatJson = useCallback(() => {
    try {
      const parsed = JSON.parse(buffer);
      const pretty = JSON.stringify(parsed, null, 2);
      lastEmitted.current = parsed;
      setBuffer(pretty);
      setJsonError(null);
      onChange(parsed);
    } catch (e) {
      setJsonError(e.message || 'Invalid JSON');
    }
  }, [buffer, onChange]);

  const useCM = isWeb && !!ReactCodeMirror;
  const defaultMinH = minHeight
    || (language === 'python' || language === 'html' ? 180 : language === 'json' ? 150 : 110);
  const chipLabel = CHIP_LABEL[language] || null;
  const editorTitle = LANG_TITLE[language]
    || (cleanLabel(fieldLabel) ? `${cleanLabel(fieldLabel)} editor`
        : (language === 'template' ? 'Template editor' : 'Text editor'));

  const langExtensions = useMemo(() => {
    const ext = [];
    if (language === 'python' && cmPython) ext.push(cmPython());
    else if (language === 'json' && cmJson) ext.push(cmJson());
    else if (language === 'html' && cmHtml) ext.push(cmHtml());
    else if (language === 'sql' && cmSql) ext.push(cmSql());
    else if (language === 'template' && TEMPLATE_HIGHLIGHT) ext.push(TEMPLATE_HIGHLIGHT);
    if (cmEditorView?.lineWrapping) ext.push(cmEditorView.lineWrapping);
    if (cmEditorView?.contentAttributes) {
      ext.push(cmEditorView.contentAttributes.of({
        spellcheck: 'false', autocapitalize: 'off', autocorrect: 'off',
      }));
    }
    return ext;
  }, [language]);

  const renderSurface = (height, autoFocus) => {
    if (useCM) {
      return (
        <ReactCodeMirror
          className={isDark ? 'cm-citra-dark' : 'cm-citra-light'}
          value={buffer}
          onChange={emit}
          extensions={langExtensions}
          theme={isDark ? 'dark' : 'light'}
          placeholder={placeholder}
          autoFocus={autoFocus}
          height={height}
          basicSetup={{
            lineNumbers: true,
            foldGutter: false,
            highlightActiveLine: true,
            autocompletion: false,
            bracketMatching: CODE_LANGS.has(language),
            highlightActiveLineGutter: true,
          }}
          style={{
            border: `1px solid ${jsonError ? '#ef4444' : border}`,
            borderRadius: 8,
            overflow: 'hidden',
            height,
          }}
        />
      );
    }
    return (
      <FallbackTextArea
        value={buffer}
        onChange={emit}
        placeholder={placeholder}
        monospace
        textColor={textColor}
        muted={muted}
        border={jsonError ? '#ef4444' : border}
        inputBg={inputBg}
        height={height}
        autoFocus={autoFocus}
      />
    );
  };

  return (
    <View>
      {/* Toolbar */}
      <View style={styles.toolbar}>
        {!!chipLabel && (
          <View style={[styles.langChip, { backgroundColor: inputBg, borderColor: border }]}>
            <Ionicons name="code-slash-outline" size={11} color={muted} />
            <Text style={[styles.langChipText, { color: muted }]}>{chipLabel}</Text>
          </View>
        )}
        <View style={{ flex: 1 }} />
        {jsonObjectMode && (
          <TouchableOpacity
            onPress={formatJson}
            style={[styles.toolBtn, { borderColor: border }]}
            accessibilityRole="button"
            accessibilityLabel="Format and validate JSON"
          >
            <Ionicons name="sparkles-outline" size={12} color={muted} />
            <Text style={[styles.toolBtnText, { color: muted }]}>Format</Text>
          </TouchableOpacity>
        )}
        <TouchableOpacity
          onPress={() => setMaximized(true)}
          style={[styles.toolBtn, { borderColor: border }]}
          accessibilityRole="button"
          accessibilityLabel="Maximize editor"
        >
          <Ionicons name="expand-outline" size={12} color={muted} />
          <Text style={[styles.toolBtnText, { color: muted }]}>Maximize</Text>
        </TouchableOpacity>
      </View>

      {renderSurface(`${defaultMinH}px`, false)}

      {jsonError && (
        <Text style={styles.jsonError} numberOfLines={2}>⚠ {jsonError}</Text>
      )}

      {/* Fullscreen editor */}
      <Modal visible={maximized} transparent animationType="fade" onRequestClose={() => setMaximized(false)}>
        <View style={styles.modalOverlay}>
          <View style={[styles.modalCard, { backgroundColor: isDark ? '#0f172a' : '#fff', borderColor: border }]}>
            <View style={[styles.modalHeader, { borderBottomColor: border }]}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, flex: 1 }}>
                <Ionicons name="code-slash-outline" size={16} color={textColor} />
                <Text style={[styles.modalTitle, { color: textColor }]}>{editorTitle}</Text>
              </View>
              {jsonObjectMode && (
                <TouchableOpacity
                  onPress={formatJson}
                  style={[styles.toolBtn, { borderColor: border, marginRight: 8 }]}
                  accessibilityRole="button"
                  accessibilityLabel="Format and validate JSON"
                >
                  <Ionicons name="sparkles-outline" size={12} color={muted} />
                  <Text style={[styles.toolBtnText, { color: muted }]}>Format</Text>
                </TouchableOpacity>
              )}
              <TouchableOpacity
                onPress={() => setMaximized(false)}
                style={styles.doneBtn}
                accessibilityRole="button"
                accessibilityLabel="Close maximized editor"
              >
                <Ionicons name="checkmark" size={14} color="#fff" />
                <Text style={styles.doneBtnText}>Done</Text>
              </TouchableOpacity>
            </View>
            <View style={{ flex: 1, padding: 14 }}>
              {maximized && renderSurface('100%', true)}
              {jsonError && (
                <Text style={styles.jsonError} numberOfLines={3}>⚠ {jsonError}</Text>
              )}
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

// ── Fallback enhanced textarea ───────────────────────────────────
function FallbackTextArea({
  value, onChange, placeholder, monospace, textColor, muted, border, inputBg, height, autoFocus,
}) {
  const [autoH, setAutoH] = useState(0);
  const isPercent = typeof height === 'string' && height.endsWith('%');
  const baseH = !isPercent ? parseInt(height, 10) || 120 : 0;

  return (
    <TextInput
      value={value}
      onChangeText={onChange}
      placeholder={placeholder}
      placeholderTextColor={muted}
      autoFocus={autoFocus}
      multiline
      spellCheck={false}
      autoCorrect={false}
      autoCapitalize="none"
      onContentSizeChange={isPercent ? undefined : (e) => {
        const h = e?.nativeEvent?.contentSize?.height || 0;
        setAutoH(Math.min(Math.max(h, baseH), 420));
      }}
      style={[
        styles.fallback,
        {
          color: textColor,
          borderColor: border,
          backgroundColor: inputBg,
          ...(monospace ? { fontFamily: 'monospace' } : {}),
          ...(isPercent
            ? { flex: 1, minHeight: 120 }
            : { minHeight: Math.max(baseH, autoH) }),
        },
      ]}
    />
  );
}

const styles = StyleSheet.create({
  toolbar: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 6 },
  langChip: {
    flexDirection: 'row', alignItems: 'center', gap: 4,
    paddingHorizontal: 7, paddingVertical: 3, borderRadius: 6, borderWidth: 1,
  },
  langChipText: { fontSize: 10, fontWeight: '700', letterSpacing: 0.4 },
  toolBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 4,
    paddingHorizontal: 8, paddingVertical: 4, borderRadius: 6, borderWidth: 1,
  },
  toolBtnText: { fontSize: 11, fontWeight: '600' },
  jsonError: { color: '#ef4444', fontSize: 11, marginTop: 5, lineHeight: 15 },
  fallback: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 13,
    lineHeight: 20,
    textAlignVertical: 'top',
    ...(isWeb ? { outline: 'none' } : {}),
  },
  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.55)',
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
  },
  modalCard: {
    width: '88%',
    maxWidth: 1100,
    height: '84%',
    borderRadius: 16,
    borderWidth: 1,
    overflow: 'hidden',
    ...(isWeb ? { boxShadow: '0 20px 60px rgba(0,0,0,0.35)' } : {}),
  },
  modalHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 1,
  },
  modalTitle: { fontSize: 15, fontWeight: '700' },
  doneBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 5,
    backgroundColor: '#6366f1', paddingHorizontal: 14, paddingVertical: 7, borderRadius: 8,
  },
  doneBtnText: { color: '#fff', fontWeight: '700', fontSize: 13 },
});
