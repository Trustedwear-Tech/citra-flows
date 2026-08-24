// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * WorkflowAIChat.js — Popup AI chat panel for the workflow canvas.
 *
 * Lets users ask AI to create, edit, or fix the current workflow via natural
 * language. Chat context is kept alive for the lifetime of the WorkflowCanvas
 * (messages state is lifted to the parent).
 */
import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, ScrollView,
  StyleSheet, ActivityIndicator, Platform, Animated,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import Markdown from 'react-native-markdown-display';
import WorkflowService from '../../services/WorkflowService';
import { statusMeta } from './executionUi';
import WorkflowDiffReview from './WorkflowDiffReview';

const NODE_TYPE_ICONS = {
  manual_trigger: '▶️', scheduled_trigger: '⏰', webhook_trigger: '🔗', start_node: '🟢',
  sql_source: '🗄️', mongo_source: '🗃️', csv_source: '📄', api_source: '🌐',
  s3_source: '☁️', sftp_source: '📁', ai_agent: '🤖', llm_processor: '🧠',
  rules_engine: '📏', data_transform: '🔄', classifier: '🏷️', extractor: '📤',
  summarizer: '📝', validator: '✅', deduplicator: '🔍', merge_data: '🔀',
  code_block: '💻', condition: '🔀', switch_router: '🔀', loop: '🔁',
  parallel_split: '⚡', merge_wait: '⏳', human_approval: '👤', delay: '⏱️',
  set_variable: '📌', sql_writer: '💾', mongo_writer: '💾', pdf_export: '📑',
  excel_export: '📊', csv_export: '📋', email_sender: '📧', s3_writer: '☁️',
  sftp_writer: '📁', webhook_output: '📡', file_download: '⬇️',
};

export default function WorkflowAIChat({
  theme,
  messages,
  setMessages,
  getWorkflowSnapshot,
  onApplyWorkflow,
  onApplyDiff,         // (diff) — apply a delta patch, preserving manual edits
  focusedNodeId,       // when set, the chat is editing one specific node
  onClearFocusedNode,
  onClose,
}) {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [appliedSet, setAppliedSet] = useState(new Set()); // indices of applied messages
  const [starterPrompts, setStarterPrompts] = useState([]);
  const scrollRef = useRef(null);
  // Dock-hosted now (no longer an absolute overlay): no horizontal slide.
  const slideAnim = useRef(new Animated.Value(0)).current;

  const isDark = theme?.isDark;
  // Grey canvas (not white) so the chat reads as a distinct surface from the
  // Node Settings panel it shares the dock with.
  const bg = isDark ? '#0b1220' : '#f1f5f9';
  const cardBg = isDark ? '#1e293b' : '#f8fafc';
  const inputBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';
  const userBubble = isDark ? '#312e81' : '#eef2ff';
  const aiBubble = isDark ? '#1e293b' : '#ffffff';

  // Slide in on mount
  useEffect(() => {
    Animated.timing(slideAnim, {
      toValue: 0,
      duration: 200,
      useNativeDriver: true,
    }).start();
  }, []);

  // Fetch starter prompts once. They replace the old Templates view —
  // shown as one-click pills in the welcome state.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { prompts } = await WorkflowService.getStarterPrompts();
        if (!cancelled && Array.isArray(prompts)) {
          setStarterPrompts(prompts);
        }
      } catch { /* degrade silently */ }
    })();
    return () => { cancelled = true; };
  }, []);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      setTimeout(() => scrollRef.current?.scrollToEnd?.({ animated: true }), 100);
    }
  }, [messages, loading]);

  // The conversation we ship to the LLM. Each turn's `content` is
  // enriched with a one-line summary of what the assistant actually
  // produced (full workflow, diff, per-node edit, validation errors)
  // so the LLM has continuity across turns even when the user hasn't
  // clicked "Apply" between messages.
  const getConversation = useCallback(
    () =>
      messages.map((m) => {
        if (m.role === 'assistant') {
          let content = m.content || '';
          if (m.workflow) {
            const n = (m.workflow.nodes || []).length;
            const e = (m.workflow.edges || []).length;
            content += `\n[produced full workflow: ${n} nodes, ${e} edges]`;
          }
          if (m.diff) {
            const d = m.diff;
            content += `\n[produced diff: +${(d.nodes_added || []).length} new`
              + ` · ~${(d.nodes_updated || []).length} updated`
              + ` · −${(d.nodes_removed || []).length} removed]`;
          }
          // Record prerequisites already told to the user so the next turn
          // doesn't repeat them or treat a user-side setup step as a
          // refinement it can perform.
          const prereqs = (m.workflow && m.workflow.prerequisites) || m.prerequisites || [];
          if (prereqs.length > 0) {
            content += `\n[already told the user these setup steps are theirs to do: ${prereqs.join('; ')}]`;
          }
          if (m.validation && (m.validation.errors || []).length > 0) {
            const codes = m.validation.errors.map(
              (er) => `${er.node_id}:${er.code}`
            ).join(', ');
            content += `\n[validation issues you must fix: ${codes}]`;
          }
          return { role: 'assistant', content };
        }
        if (m.role === 'user' && m.focusedNodeId) {
          return {
            role: 'user',
            content: `[focusing node ${m.focusedNodeId}] ${m.content}`,
          };
        }
        return { role: m.role, content: m.content || '' };
      }),
    [messages]
  );

  // Build the "effective workflow" — what the assistant currently
  // believes the workflow looks like, regardless of whether the user
  // has applied any of it to the canvas.
  //
  // Replays every assistant message's emitted `workflow` snapshot on top of
  // the canvas snapshot, so we send the assistant its own latest output (not a
  // stale empty canvas) as the working copy.
  const getEffectiveWorkflow = useCallback(() => {
    const canvas = getWorkflowSnapshot() || {};
    // Preserve EVERY canvas field (workflow_id, description, variables, meta) —
    // not just nodes/edges — so the assistant receives the complete state.
    let wf = {
      ...canvas,
      name: canvas.name || '',
      nodes: Array.isArray(canvas.nodes) ? [...canvas.nodes] : [],
      edges: Array.isArray(canvas.edges) ? [...canvas.edges] : [],
      variables: canvas.variables || {},
    };
    for (const m of messages) {
      if (m.role !== 'assistant') continue;
      if (m.workflow) {
        wf = {
          ...wf,
          name: m.workflow.name || wf.name,
          description: m.workflow.description ?? wf.description,
          icon: m.workflow.icon ?? wf.icon,
          tags: m.workflow.tags ?? wf.tags,
          nodes: Array.isArray(m.workflow.nodes) ? [...m.workflow.nodes] : wf.nodes,
          edges: Array.isArray(m.workflow.edges) ? [...m.workflow.edges] : wf.edges,
          variables: m.workflow.variables || wf.variables,
        };
      }
    }
    return wf;
  }, [messages, getWorkflowSnapshot]);

  // Patch the most recent assistant message in place as stream events arrive.
  const updateLastAssistant = useCallback((patch) => {
    setMessages((prev) => {
      const copy = [...prev];
      for (let i = copy.length - 1; i >= 0; i--) {
        if (copy[i].role === 'assistant') {
          copy[i] = typeof patch === 'function' ? patch(copy[i]) : { ...copy[i], ...patch };
          break;
        }
      }
      return copy;
    });
  }, [setMessages]);

  const handleSend = async () => {
    const prompt = input.trim();
    if (!prompt || loading) return;

    setInput('');
    // The "effective workflow" reflects every prior AI artefact in the chat,
    // even before the user clicked Apply — sent as the assistant's working copy.
    const effective = getEffectiveWorkflow();
    const focusedNodeExists =
      focusedNodeId && (effective.nodes || []).some((n) => n.id === focusedNodeId);

    const userMsg = { role: 'user', content: prompt, focusedNodeId };
    // Push the user turn AND a streaming assistant placeholder we mutate live.
    setMessages((prev) => [
      ...prev,
      userMsg,
      { role: 'assistant', content: '', streaming: true, statusText: 'Thinking…' },
    ]);
    setLoading(true);

    const handlers = {
      onStatus: (text) => updateLastAssistant({ statusText: text }),
      onValidation: (validation) => updateLastAssistant({ validation }),
      onRunResult: (run) => updateLastAssistant((m) => ({
        ...m, runResults: [...(m.runResults || []), run],
      })),
      onOperation: (op) => updateLastAssistant((m) => {
        if (op.type === 'workflow') {
          const prerequisites = (op.setup_gaps || [])
            .map((g) => (g && g.message) || String(g));
          return {
            ...m,
            workflow: { ...op.workflow, prerequisites },
            diff: op.diff || null,
            validation: op.validation || m.validation,
          };
        }
        return m;
      }),
      onDone: (evt) => updateLastAssistant((m) => ({
        ...m,
        content: (evt.message || m.content || 'Done.'),
        streaming: false,
        statusText: '',
      })),
      onError: (msg) => updateLastAssistant((m) => ({
        ...m,
        content: (m.content ? `${m.content}\n\n` : '') + `Error: ${msg}`,
        isError: true,
        streaming: false,
        statusText: '',
      })),
    };

    try {
      // `effective` already carries the complete canvas (incl. workflow_id,
      // description, variables and full node configs) via getWorkflowSnapshot.
      await WorkflowService.aiChatStream(
        {
          prompt,
          workflow: effective,
          conversation: getConversation(),
          focused_node_id: focusedNodeExists ? focusedNodeId : null,
        },
        handlers,
      );
    } catch (err) {
      updateLastAssistant((m) => ({
        ...m,
        content: `Error: ${err.message}`,
        isError: true,
        streaming: false,
        statusText: '',
      }));
    } finally {
      setLoading(false);
    }
  };

  const handleSuggestion = (suggestion) => {
    setInput(suggestion);
  };

  const handleApply = (msgIndex, msg) => {
    // Edit with a diff → apply the diff so the user's manual edits survive.
    if (msg.diff && onApplyDiff) {
      onApplyDiff(msg.diff);
    }
    // Fresh build (no diff) → replace the canvas.
    else if (msg.workflow) {
      onApplyWorkflow(msg.workflow);
    }
    setAppliedSet((prev) => new Set(prev).add(msgIndex));
  };

  const handleStartOver = () => {
    setMessages([]);
    setAppliedSet(new Set());
    setInput('');
  };

  const handleClose = () => {
    // Dock controls visibility; just notify the parent (no slide-out).
    onClose?.();
  };

  // ── Reusable validation banner ──────────────────────────
  const renderValidation = (validation) => {
    if (!validation || !validation.errors || validation.errors.length === 0) return null;
    return (
      <View style={[styles.validation, { backgroundColor: isDark ? '#451a1a' : '#fef2f2', borderColor: '#ef4444' }]}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 4 }}>
          <Ionicons name="warning" size={13} color="#ef4444" />
          <Text style={{ color: '#ef4444', fontSize: 11, fontWeight: '700' }}>
            {validation.errors.length} reference issue{validation.errors.length === 1 ? '' : 's'}
          </Text>
        </View>
        {validation.errors.slice(0, 5).map((e, i) => (
          <Text key={i} style={{ color: isDark ? '#fca5a5' : '#7f1d1d', fontSize: 10, lineHeight: 14 }}>
            • node <Text style={{ fontWeight: '700' }}>{e.node_id}</Text> — {e.code}
            {e.field ? ` (${e.field}=${e.value})` : ''}
            {e.dataset_id ? ` (dataset=${e.dataset_id})` : ''}
          </Text>
        ))}
      </View>
    );
  };

  // ── Prerequisites checklist (user-side setup, NOT clickable) ─────
  // These are things only the user can do outside this chat (register a
  // connection / data source). Rendered as an informational checklist so
  // the user never clicks one as if it were a refinement the AI can do —
  // that was the source of the silent "no-op refine" loop.
  const renderPrerequisites = (prerequisites) => {
    const list = prerequisites || [];
    if (list.length === 0) return null;
    return (
      <View style={[styles.prereqs, { backgroundColor: isDark ? '#1e293b' : '#fffbeb', borderColor: isDark ? '#475569' : '#fcd34d' }]}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 4 }}>
          <Ionicons name="construct-outline" size={13} color={isDark ? '#fbbf24' : '#b45309'} />
          <Text style={{ color: isDark ? '#fbbf24' : '#b45309', fontSize: 11, fontWeight: '700' }}>
            Before this can run — your setup
          </Text>
        </View>
        {list.map((p, i) => (
          <Text key={i} style={{ color: isDark ? '#fde68a' : '#78350f', fontSize: 10, lineHeight: 15 }}>
            • {p}
          </Text>
        ))}
      </View>
    );
  };

  // ── Test-run result (per-node pass/fail) ───────────────
  const renderRunResults = (runResults) => {
    const list = runResults || [];
    if (list.length === 0) return null;
    return list.map((run, ri) => {
      const rm = statusMeta(run.status);
      const nodes = run.node_results || [];
      const failed = nodes.filter((n) => ['failed', 'timed_out'].includes(String(n.status).toLowerCase()));
      return (
        <View key={ri} style={[styles.preview, { backgroundColor: isDark ? '#0f172a' : '#f8fafc', borderColor: border }]}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 6 }}>
            <Ionicons name={rm.name} size={15} color={rm.color} />
            <Text style={[styles.previewTitle, { color: rm.color }]}>Test run · {rm.label}</Text>
            <View style={{ flex: 1 }} />
            <View style={[styles.envBadge, { backgroundColor: isDark ? '#78350f' : '#fef3c7' }]}>
              <Text style={{ color: isDark ? '#fde68a' : '#b45309', fontSize: 9, fontWeight: '700' }}>TEST</Text>
            </View>
          </View>
          {run.error ? (
            <Text style={{ color: '#ef4444', fontSize: 11, marginBottom: 6 }}>{run.error}</Text>
          ) : null}
          {nodes.map((n, i) => {
            const nm = statusMeta(n.status);
            return (
              <View key={n.node_id || i} style={styles.runNodeRow}>
                <Ionicons name={nm.name} size={13} color={nm.color} />
                <Text style={[styles.runNodeId, { color: text }]} numberOfLines={1}>{n.node_id}</Text>
                {n.error ? (
                  <Text style={{ color: '#ef4444', fontSize: 10, flex: 1 }} numberOfLines={2}>{n.error}</Text>
                ) : <View style={{ flex: 1 }} />}
              </View>
            );
          })}
          {failed.length > 0 && (
            <Text style={{ color: muted, fontSize: 10, fontStyle: 'italic', marginTop: 4 }}>
              {failed.length} node{failed.length === 1 ? '' : 's'} failed — see the diagnosis above.
            </Text>
          )}
        </View>
      );
    });
  };

  // ── Workflow preview inside chat bubbles ────────────────
  const renderWorkflowPreview = (msg, msgIndex) => {
    const workflow = msg.workflow || {};
    const nodes = workflow.nodes || [];
    const edges = workflow.edges || [];
    const isApplied = appliedSet.has(msgIndex);
    const diff = msg.diff;
    // A refine that changed nothing — don't offer an "Apply diff" button
    // that would do nothing; the message text already explains why.
    const isNoOp = !!msg.noOp;
    const prerequisites = workflow.prerequisites || [];

    return (
      <View style={[styles.preview, { backgroundColor: isDark ? '#0f172a' : '#f8fafc', borderColor: border }]}>
        <View style={styles.previewHeader}>
          <Text style={[styles.previewTitle, { color: text }]}>
            {workflow.icon || '🤖'} {workflow.name}
          </Text>
          <Text style={[styles.previewMeta, { color: muted }]}>
            {diff
              ? `+${(diff.nodes_added || []).length} new · ~${(diff.nodes_updated || []).length} updated · −${(diff.nodes_removed || []).length} removed`
              : `${nodes.length} nodes · ${edges.length} edges`}
          </Text>
        </View>

        <View style={styles.nodeChips}>
          {nodes.map((n, i) => (
            <View key={n.id || i} style={[styles.nodeChip, { backgroundColor: isDark ? '#334155' : '#e2e8f0' }]}>
              <Text style={{ fontSize: 12 }}>{NODE_TYPE_ICONS[n.type] || '⚙️'}</Text>
              <Text style={[styles.nodeChipText, { color: text }]} numberOfLines={1}>
                {n.label || n.type}
              </Text>
            </View>
          ))}
        </View>

        {renderValidation(msg.validation)}

        {renderPrerequisites(prerequisites)}

        {diff && !isNoOp && <WorkflowDiffReview diff={diff} theme={theme} />}

        {isNoOp ? (
          <View style={[styles.noOpNote, { borderColor: border }]}>
            <Ionicons name="information-circle-outline" size={14} color={muted} />
            <Text style={{ color: muted, fontSize: 11, flex: 1 }}>
              No changes to apply — see the message above.
            </Text>
          </View>
        ) : (
          <TouchableOpacity
            style={[
              styles.applyBtn,
              isApplied && styles.applyBtnApplied,
            ]}
            onPress={() => handleApply(msgIndex, msg)}
            disabled={isApplied}
          >
            <Ionicons
              name={isApplied ? 'checkmark-circle' : 'arrow-forward-circle-outline'}
              size={16}
              color="#fff"
            />
            <Text style={styles.applyBtnText}>
              {isApplied ? 'Applied' : diff ? 'Apply diff to Canvas' : 'Apply to Canvas'}
            </Text>
          </TouchableOpacity>
        )}

        {(workflow.suggestions || []).length > 0 && (
          <View style={styles.suggestions}>
            <Text style={[styles.suggestLabel, { color: muted }]}>Refine:</Text>
            {workflow.suggestions.map((s, i) => (
              <TouchableOpacity
                key={i}
                style={[styles.suggestionChip, { borderColor: border }]}
                onPress={() => handleSuggestion(s)}
              >
                <Text style={[styles.suggestionText, { color: isDark ? '#a78bfa' : '#6d28d9' }]}>{s}</Text>
              </TouchableOpacity>
            ))}
          </View>
        )}
      </View>
    );
  };

  // ── Render ─────────────────────────────────────────────
  return (
    <Animated.View
      style={[
        styles.container,
        {
          backgroundColor: bg,
          borderLeftColor: border,
          transform: [{ translateX: slideAnim }],
        },
      ]}
    >
      {/* Header */}
      <View style={[styles.header, { borderBottomColor: border }]}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, flex: 1 }}>
          <Text style={{ fontSize: 18 }}>✨</Text>
          <View style={{ flex: 1 }}>
            <Text style={[styles.headerTitle, { color: text }]}>AI Assistant</Text>
            <Text style={[styles.headerSub, { color: muted }]}>
              Describe changes or ask for help
            </Text>
          </View>
        </View>
        <View style={{ flexDirection: 'row', gap: 6 }}>
          {messages.length > 0 && (
            <TouchableOpacity
              style={[styles.headerBtn, { borderColor: border }]}
              onPress={handleStartOver}
            >
              <Ionicons name="refresh" size={14} color={muted} />
              <Text style={{ color: muted, fontSize: 11 }}>New</Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity onPress={handleClose} style={{ padding: 4 }}>
            <Ionicons name="close" size={20} color={muted} />
          </TouchableOpacity>
        </View>
      </View>

      {/* Messages */}
      <ScrollView
        ref={scrollRef}
        style={styles.messagesArea}
        contentContainerStyle={styles.messagesContent}
        showsVerticalScrollIndicator={false}
      >
        {focusedNodeId && (
          <View style={[styles.focusBanner, { backgroundColor: isDark ? '#1e1b4b' : '#eef2ff', borderColor: '#6366f1' }]}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, flex: 1 }}>
              <Ionicons name="locate" size={14} color="#6366f1" />
              <Text style={{ color: text, fontSize: 12 }}>
                Editing node <Text style={{ fontFamily: 'monospace', fontWeight: '700' }}>{focusedNodeId}</Text>
              </Text>
            </View>
            <TouchableOpacity onPress={() => onClearFocusedNode?.()}>
              <Ionicons name="close-circle" size={16} color={muted} />
            </TouchableOpacity>
          </View>
        )}

        {messages.length === 0 && (
          <View style={styles.welcome}>
            <Text style={{ fontSize: 32, textAlign: 'center', marginBottom: 10 }}>✨</Text>
            <Text style={[styles.welcomeTitle, { color: text }]}>
              How can I help?
            </Text>
            <Text style={[styles.welcomeSub, { color: muted }]}>
              Describe what you want to build. I know your saved connections and registered dept-MCP datasets, and I'll wire the workflow against real ids.
            </Text>

            <View style={styles.exampleList}>
              {(starterPrompts.length > 0
                ? starterPrompts
                : [{ label: 'Custom — describe your workflow', prompt: '' }]
              ).map((s, i) => (
                <TouchableOpacity
                  key={i}
                  style={[styles.exampleCard, { backgroundColor: cardBg, borderColor: border }]}
                  onPress={() => setInput(s.prompt || '')}
                >
                  <Ionicons name="sparkles-outline" size={13} color={isDark ? '#a78bfa' : '#7c3aed'} />
                  <Text style={[styles.exampleText, { color: text }]}>{s.label}</Text>
                </TouchableOpacity>
              ))}
            </View>
          </View>
        )}

        {messages.map((msg, i) => (
          <View
            key={i}
            style={[
              styles.bubble,
              msg.role === 'user'
                ? [styles.userBubble, { backgroundColor: userBubble }]
                : [styles.aiBubble, { backgroundColor: aiBubble }],
              msg.isError && { borderLeftColor: '#ef4444', borderLeftWidth: 3 },
            ]}
          >
            {/* Live working line while the agent reasons / calls tools. */}
            {msg.role === 'assistant' && msg.streaming && (
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: msg.content ? 6 : 0 }}>
                <ActivityIndicator size="small" color="#8b5cf6" />
                <Text style={[styles.bubbleText, { color: muted, fontStyle: 'italic' }]}>
                  {msg.statusText || 'Working…'}
                </Text>
              </View>
            )}
            {!!msg.content && (
              msg.role === 'assistant'
                ? <MarkdownBubble content={msg.content} text={text} muted={muted} isDark={isDark} />
                : <Text style={[styles.bubbleText, { color: text }]}>{msg.content}</Text>
            )}
            {msg.runResults && renderRunResults(msg.runResults)}
            {msg.workflow && (msg.workflow.nodes || []).length > 0
              && renderWorkflowPreview(msg, i)}
          </View>
        ))}
      </ScrollView>

      {/* Input */}
      <View style={[styles.inputBar, { backgroundColor: bg, borderTopColor: border }]}>
        <TextInput
          value={input}
          onChangeText={setInput}
          style={[styles.textInput, { color: text, backgroundColor: inputBg, borderColor: border }]}
          placeholder={
            messages.some((m) => m.workflow)
              ? 'Describe changes...'
              : 'Describe your workflow...'
          }
          placeholderTextColor={muted}
          multiline
          maxLength={8000}
          onSubmitEditing={Platform.OS === 'web' ? handleSend : undefined}
          blurOnSubmit={Platform.OS === 'web'}
        />
        <TouchableOpacity
          style={[styles.sendBtn, (!input.trim() || loading) && { opacity: 0.4 }]}
          onPress={handleSend}
          disabled={!input.trim() || loading}
        >
          <Ionicons name="send" size={16} color="#fff" />
        </TouchableOpacity>
      </View>
    </Animated.View>
  );
}

// Assistant replies are markdown (the agent returns prose + fenced code
// blocks for "give me the Python of this node"). Falls back to plain text if
// the markdown parser throws on odd content.
function MarkdownBubble({ content, text, muted, isDark }) {
  const codeBg = isDark ? '#020617' : '#e2e8f0';
  const mdStyles = {
    body: { color: text, fontSize: 13, lineHeight: 19 },
    heading1: { color: text, fontSize: 16, fontWeight: '700', marginTop: 4, marginBottom: 4 },
    heading2: { color: text, fontSize: 15, fontWeight: '700', marginTop: 4, marginBottom: 4 },
    heading3: { color: text, fontSize: 14, fontWeight: '700', marginTop: 4, marginBottom: 2 },
    paragraph: { color: text, marginTop: 0, marginBottom: 8 },
    bullet_list: { marginBottom: 6 },
    ordered_list: { marginBottom: 6 },
    list_item: { color: text },
    strong: { fontWeight: '700' },
    link: { color: isDark ? '#a78bfa' : '#6d28d9' },
    // No background on inline code — a boxed background renders as overlapping
    // chips when the span wraps across lines in the narrow panel. Just a
    // distinct monospace color so it still reads as code but flows as text.
    code_inline: { color: isDark ? '#a5b4fc' : '#4338ca', fontFamily: 'monospace', backgroundColor: 'transparent' },
    // Real multi-line code blocks keep a background (they don't wrap inline).
    code_block: { backgroundColor: codeBg, color: text, fontFamily: 'monospace', padding: 10, borderRadius: 6 },
    fence: { backgroundColor: codeBg, color: text, fontFamily: 'monospace', padding: 10, borderRadius: 6 },
  };
  try {
    return <Markdown style={mdStyles}>{String(content)}</Markdown>;
  } catch {
    return <Text style={{ color: text, fontSize: 13, lineHeight: 19 }}>{String(content)}</Text>;
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
const styles = StyleSheet.create({
  container: {
    flex: 1,
    minWidth: 0,
    minHeight: 0,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 1,
  },
  headerTitle: { fontSize: 14, fontWeight: '700' },
  headerSub: { fontSize: 11 },
  headerBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 6,
    borderWidth: 1,
  },
  messagesArea: { flex: 1 },
  messagesContent: { padding: 16, paddingBottom: 8 },
  welcome: { alignItems: 'center', paddingTop: 24 },
  welcomeTitle: { fontSize: 16, fontWeight: '700', textAlign: 'center', marginBottom: 6 },
  welcomeSub: { fontSize: 12, textAlign: 'center', marginBottom: 20, maxWidth: 320, lineHeight: 18 },
  exampleList: { width: '100%', gap: 8 },
  exampleCard: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: 8,
    padding: 12,
    borderRadius: 10,
    borderWidth: 1,
  },
  exampleText: { fontSize: 12, flex: 1, lineHeight: 17 },
  bubble: { marginBottom: 10, padding: 12, borderRadius: 12, maxWidth: '92%' },
  userBubble: { alignSelf: 'flex-end', borderBottomRightRadius: 4 },
  aiBubble: { alignSelf: 'flex-start', borderBottomLeftRadius: 4 },
  bubbleText: { fontSize: 13, lineHeight: 19 },
  preview: {
    marginTop: 10,
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
  },
  previewHeader: { marginBottom: 8 },
  previewTitle: { fontSize: 13, fontWeight: '700' },
  envBadge: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 6 },
  runNodeRow: { flexDirection: 'row', alignItems: 'center', gap: 6, paddingVertical: 2 },
  runNodeId: { fontSize: 11, fontWeight: '600', fontFamily: 'monospace', maxWidth: '45%' },
  previewMeta: { fontSize: 10, marginTop: 2 },
  nodeChips: { flexDirection: 'row', flexWrap: 'wrap', gap: 5, marginBottom: 10 },
  nodeChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 6,
    paddingVertical: 3,
    borderRadius: 6,
  },
  nodeChipText: { fontSize: 10, fontWeight: '500', maxWidth: 90 },
  applyBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    backgroundColor: '#6366f1',
    paddingVertical: 8,
    borderRadius: 8,
  },
  applyBtnApplied: {
    backgroundColor: '#22c55e',
    opacity: 0.7,
  },
  applyBtnText: { color: '#fff', fontWeight: '600', fontSize: 12 },
  prereqs: {
    marginTop: 8,
    marginBottom: 8,
    borderWidth: 1,
    borderRadius: 8,
    padding: 8,
  },
  noOpNote: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
  },
  suggestions: { marginTop: 8 },
  suggestLabel: { fontSize: 10, fontWeight: '600', marginBottom: 4 },
  suggestionChip: {
    paddingHorizontal: 8,
    paddingVertical: 5,
    borderRadius: 8,
    borderWidth: 1,
    marginBottom: 5,
  },
  suggestionText: { fontSize: 11 },
  validation: {
    marginTop: 8,
    marginBottom: 10,
    borderWidth: 1,
    borderRadius: 8,
    padding: 8,
  },
  focusBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
    borderRadius: 8,
    borderWidth: 1,
    marginBottom: 10,
  },
  inputBar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderTopWidth: 1,
  },
  textInput: {
    flex: 1,
    borderWidth: 1,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 13,
    lineHeight: 19,
    maxHeight: 240,
    minHeight: 114,
    textAlignVertical: 'top',
    outline: 'none',
  },
  sendBtn: {
    backgroundColor: '#6366f1',
    width: 36,
    height: 36,
    borderRadius: 18,
    justifyContent: 'center',
    alignItems: 'center',
  },
});
