// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * WorkflowCanvas.js — React Flow canvas for the visual workflow builder
 *
 * Drag nodes from the palette, connect them with edges, configure node settings.
 */
import React, { useState, useCallback, useRef, useEffect, useMemo } from 'react';
import {
  View, Text, TouchableOpacity, StyleSheet, ActivityIndicator,
  TextInput, ScrollView, Modal, Dimensions, Platform,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';

// React Flow (web only)
let ReactFlow, Background, Controls, MiniMap, addEdge, useNodesState, useEdgesState,
  MarkerType, ReactFlowProvider, useReactFlow, Handle, Position;

if (Platform.OS === 'web') {
  const rf = require('@xyflow/react');
  ReactFlow = rf.ReactFlow;
  Background = rf.Background;
  Controls = rf.Controls;
  MiniMap = rf.MiniMap;
  addEdge = rf.addEdge;
  useNodesState = rf.useNodesState;
  useEdgesState = rf.useEdgesState;
  MarkerType = rf.MarkerType;
  ReactFlowProvider = rf.ReactFlowProvider;
  useReactFlow = rf.useReactFlow;
  Handle = rf.Handle;
  Position = rf.Position;
  // Import CSS
  require('@xyflow/react/dist/style.css');
}

import WorkflowService from '../../services/WorkflowService';
import { WORKFLOW_API_BASE } from '../../config/config';
import NodePalette from './NodePalette';
import Splitter from './Splitter';
import ExecutionMonitor from './ExecutionMonitor';
import WorkflowRightDock from './WorkflowRightDock';
import WorkflowSettingsModal from './WorkflowSettingsModal';
import WorkflowVersionsModal from './WorkflowVersionsModal';

const { width: SCREEN_WIDTH } = Dimensions.get('window');

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Custom node component rendered inside React Flow
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
const CATEGORY_BG = {
  trigger: { bg: '#f0fdf4', border: '#22c55e', accent: '#16a34a' },
  source: { bg: '#eff6ff', border: '#3b82f6', accent: '#2563eb' },
  agent: { bg: '#f5f3ff', border: '#7c3aed', accent: '#6d28d9' },
  processor: { bg: '#faf5ff', border: '#8b5cf6', accent: '#7c3aed' },
  logic: { bg: '#fdf2f8', border: '#ec4899', accent: '#db2777' },
  dept_flow: { bg: '#f0f9ff', border: '#0ea5e9', accent: '#0284c7' },
  output: { bg: '#fffbeb', border: '#f59e0b', accent: '#d97706' },
};

const TOOL_ICONS = {
  web_search: '🔍', code_execute: '💻', db_query: '🗄️',
  vector_search: '🧠', http_request: '🌐',
};

const SWITCH_DEFAULT_HANDLE_ID = 'out-5';
const MAX_SWITCH_OUTPUTS = 6;
const MAX_SWITCH_NON_DEFAULT_OUTPUTS = MAX_SWITCH_OUTPUTS - 1;

function _getConfigSummary(config, nodeType) {
  // Return a short text showing key config values on the node face
  if (!config) return null;
  const lines = [];
  if (config.agent_name) lines.push(config.agent_name);
  if (config.model) lines.push('AI model');
  if (config.system_prompt) lines.push(config.system_prompt.substring(0, 60) + (config.system_prompt.length > 60 ? '…' : ''));
  else if (config.user_prompt) lines.push(config.user_prompt.substring(0, 60) + (config.user_prompt.length > 60 ? '…' : ''));
  else if (config.operation) lines.push(`Op: ${config.operation}`);
  else if (config.field) lines.push(`Field: ${config.field}`);
  else if (config.url) lines.push(config.url.substring(0, 45));
  else if (config.collection) lines.push(`Collection: ${config.collection}`);
  if (lines.length === 0) return null;
  return lines.slice(0, 2);
}

function _getFieldDefault(schema, fieldName) {
  return (schema?.fields || []).find((field) => field.name === fieldName)?.default;
}

function _coerceRouteList(rawRoutes) {
  if (Array.isArray(rawRoutes)) return rawRoutes;
  if (typeof rawRoutes === 'string' && rawRoutes.trim()) {
    try {
      const parsed = JSON.parse(rawRoutes);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }
  return [];
}

function _getSwitchRoutes(config, schema) {
  const configuredRoutes = _coerceRouteList(config?.routes);
  if (configuredRoutes.length) return configuredRoutes;
  return _coerceRouteList(_getFieldDefault(schema, 'routes'));
}

function _compactRouteText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value.trim();
  return String(value).trim();
}

function _switchRouteLabel(route, routeIndex) {
  const label = _compactRouteText(route?.label);
  const value = _compactRouteText(route?.value);
  const isDefault = value === '__default__';

  if (isDefault) {
    if (label && !/^default$/i.test(label)) return `Default: ${label}`;
    return 'Default';
  }

  const detail = label && !/^route\s*\d+$/i.test(label)
    ? label
    : value;
  return detail ? `Route ${routeIndex}: ${detail}` : `Route ${routeIndex}`;
}

export function getWorkflowNodeOutputSpecs({ nodeType, schema, config }) {
  const outputCount = schema?.outputs ?? 1;
  const outputLabels = schema?.output_labels || [];

  if (nodeType === 'switch_router') {
    const routes = _getSwitchRoutes(config, schema);
    if (routes.length) {
      const specs = [];
      const usedHandles = new Set();
      const hasDefaultRoute = routes.some((route) => _compactRouteText(route?.value) === '__default__');
      let nonDefaultCount = 0;
      routes.forEach((route, routeIndex) => {
        const value = _compactRouteText(route?.value);
        if (value === '__default__') {
          if (usedHandles.has(SWITCH_DEFAULT_HANDLE_ID)) return;
          specs.push({
            key: `out-default-${routeIndex}`,
            handleId: SWITCH_DEFAULT_HANDLE_ID,
            label: _switchRouteLabel(route, routeIndex),
          });
          usedHandles.add(SWITCH_DEFAULT_HANDLE_ID);
          return;
        }

        if (nonDefaultCount >= MAX_SWITCH_NON_DEFAULT_OUTPUTS) return;
        if (hasDefaultRoute && routeIndex >= MAX_SWITCH_NON_DEFAULT_OUTPUTS) return;
        const handleId = `out-${routeIndex}`;
        if (usedHandles.has(handleId)) return;
        specs.push({
          key: `out-${routeIndex}`,
          handleId,
          label: _switchRouteLabel(route, routeIndex),
        });
        usedHandles.add(handleId);
        nonDefaultCount += 1;
      });

      return specs.slice(0, MAX_SWITCH_OUTPUTS);
    }
  }

  return Array.from({ length: outputCount }).map((_, i) => {
    const handleId = outputCount <= 2
      ? (i === 0 ? 'true' : 'false')
      : `out-${i}`;
    return {
      key: `out-${i}`,
      handleId,
      label: outputLabels[i] || (outputCount === 2 ? (i === 0 ? 'T' : 'F') : ''),
    };
  });
}

const WorkflowNode = ({ data, selected }) => {
  if (!Handle || !Position) return null;
  const { label, icon, color, category, schema, config, nodeType, executionStatus } = data;
  const inputCount = schema?.inputs ?? 1;
  const catStyle = CATEGORY_BG[category] || CATEGORY_BG.processor;
  const tools = config?.tools || [];
  const configSummary = _getConfigSummary(config, nodeType);
  const outputSpecs = getWorkflowNodeOutputSpecs({ nodeType, schema, config });
  const hasOutputLabels = outputSpecs.some((spec) => spec.label);
  const outputLabelWidth = nodeType === 'switch_router' ? 132 : 54;
  const contentRightPadding = hasOutputLabels ? outputLabelWidth + 34 : 14;
  const minNodeHeight = Math.max(
    nodeType === 'switch_router' && hasOutputLabels ? 86 : 0,
    outputSpecs.length > 2 && hasOutputLabels ? outputSpecs.length * 24 + 28 : 0
  );

  // Execution status styling
  let statusBorder = selected ? '#3b82f6' : catStyle.border;
  let statusGlow = 'none';
  let statusBadge = null;
  if (executionStatus === 'running') {
    statusBorder = '#3b82f6';
    statusGlow = '0 0 12px rgba(59,130,246,.5)';
    statusBadge = { bg: '#3b82f6', text: '⟳' };
  } else if (executionStatus === 'completed') {
    statusBorder = '#22c55e';
    statusGlow = '0 0 8px rgba(34,197,94,.3)';
    statusBadge = { bg: '#22c55e', text: '✓' };
  } else if (executionStatus === 'failed') {
    statusBorder = '#ef4444';
    statusGlow = '0 0 8px rgba(239,68,68,.3)';
    statusBadge = { bg: '#ef4444', text: '✗' };
  } else if (executionStatus === 'skipped') {
    statusBadge = { bg: '#94a3b8', text: '—' };
  }

  return (
    <div
      style={{
        background: catStyle.bg,
        border: `2px solid ${statusBorder}`,
        borderRadius: 14,
        minWidth: 180,
        ...(nodeType === 'switch_router'
          ? { minWidth: 320, maxWidth: 340 }
          : { maxWidth: 260 }),
        ...(minNodeHeight ? { minHeight: minNodeHeight } : {}),
        boxShadow: executionStatus === 'running'
          ? statusGlow
          : selected
            ? '0 0 0 2px rgba(59,130,246,.3)'
            : '0 2px 8px rgba(0,0,0,.06)',
        cursor: 'grab',
        position: 'relative',
        overflow: 'visible',
        transition: 'box-shadow 0.3s, border-color 0.3s',
      }}
    >
      {/* Status badge (top-right) */}
      {statusBadge && (
        <div style={{
          position: 'absolute', top: -8, right: -8, width: 22, height: 22,
          borderRadius: '50%', background: statusBadge.bg, color: '#fff',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 12, fontWeight: 700, boxShadow: '0 1px 4px rgba(0,0,0,.15)',
          zIndex: 10,
        }}>
          {statusBadge.text}
        </div>
      )}

      {/* Input handles */}
      {category !== 'trigger' && Array.from({ length: inputCount }).map((_, i) => (
        <Handle
          key={`in-${i}`}
          type="target"
          position={Position.Left}
          id={`in-${i}`}
          style={{
            top: `${((i + 1) / (inputCount + 1)) * 100}%`,
            background: catStyle.accent,
            width: 10, height: 10, border: '2px solid #fff',
          }}
        />
      ))}

      {/* Header bar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '10px 14px 6px',
        paddingRight: contentRightPadding,
        borderBottom: configSummary ? `1px solid ${catStyle.border}33` : 'none',
      }}>
        <span style={{ fontSize: 20 }}>{icon}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            fontWeight: 700, fontSize: 13, color: '#1e293b',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {label}
          </div>
          <div style={{
            fontSize: 9, color: catStyle.accent, marginTop: 1,
            textTransform: 'uppercase', letterSpacing: 0.8, fontWeight: 600,
          }}>
            {category}
          </div>
        </div>
      </div>

      {/* Config summary */}
      {configSummary && (
        <div style={{ padding: `4px ${contentRightPadding}px 6px 14px` }}>
          {configSummary.map((line, i) => (
            <div key={i} style={{
              fontSize: 10, color: '#64748b', lineHeight: '14px',
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>
              {line}
            </div>
          ))}
        </div>
      )}

      {/* Tool badges (for agent nodes) */}
      {tools.length > 0 && (
        <div style={{
          display: 'flex', gap: 3, padding: `2px ${contentRightPadding}px 8px 14px`,
          flexWrap: 'wrap',
        }}>
          {tools.map((t) => (
            <span key={t} title={t} style={{
              fontSize: 11, background: `${catStyle.accent}15`,
              borderRadius: 4, padding: '1px 5px',
            }}>
              {TOOL_ICONS[t] || '🔧'}
            </span>
          ))}
        </div>
      )}

      {/* Bottom padding if no summary/tools */}
      {!configSummary && tools.length === 0 && (
        <div style={{ height: 6 }} />
      )}

      {/* Output handles with labels */}
      {outputSpecs.map((spec, i) => {
        const top = ((i + 1) / (outputSpecs.length + 1)) * 100;
        return (
          <React.Fragment key={spec.key}>
            <Handle
              type="source"
              position={Position.Right}
              id={spec.handleId}
              style={{
                top: `${top}%`,
                background: catStyle.accent,
                width: 10, height: 10, border: '2px solid #fff',
              }}
            />
            {spec.label && outputSpecs.length > 1 && (
              <div style={{
                position: 'absolute',
                right: 15,
                top: `${top}%`,
                transform: 'translateY(-50%)',
                width: outputLabelWidth,
                maxWidth: outputLabelWidth,
                padding: '2px 5px',
                borderRadius: 5,
                background: catStyle.bg,
                boxShadow: `0 0 0 1px ${catStyle.border}33`,
                fontSize: 9,
                lineHeight: '12px',
                color: catStyle.accent,
                fontWeight: 700,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                textAlign: 'right',
                pointerEvents: 'none',
              }}>
                {spec.label}
              </div>
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
};

const nodeTypes = { workflowNode: WorkflowNode };

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Main canvas (wrapped in ReactFlowProvider externally)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
function CanvasInner({ workflowId, theme, onClose, onShowGuide, onSaveSuccess, onShowRuns }) {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [schemas, setSchemas] = useState([]);
  const [workflowName, setWorkflowName] = useState('Untitled Workflow');
  const [workflowDesc, setWorkflowDesc] = useState('');
  const [selectedNode, setSelectedNode] = useState(null);
  const [showPalette, setShowPalette] = useState(true);
  // Resizable node-palette width (web): dragged via the splitter next to the
  // palette; persisted so the operator's layout survives reloads.
  const [paletteWidth, setPaletteWidth] = useState(() => {
    if (Platform.OS !== 'web') return 240;
    try {
      const saved = parseInt(window.localStorage.getItem('citra_wf_palette_width'), 10);
      if (Number.isFinite(saved)) return Math.min(520, Math.max(180, saved));
    } catch (e) { /* private mode etc. — fall through to default */ }
    return 240;
  });
  const onPaletteDelta = useCallback((dx) => {
    setPaletteWidth((w) => {
      const next = Math.min(520, Math.max(180, w + dx));
      try {
        window.localStorage.setItem('citra_wf_palette_width', String(next));
      } catch (err) { /* best-effort persistence */ }
      return next;
    });
  }, []);

  const [showExecution, setShowExecution] = useState(false);
  const [currentExecution, setCurrentExecution] = useState(null);
  // Inline notice banner — replaces browser window.alert() so errors/info show
  // INSIDE the app (toast at the top of the canvas) instead of a native
  // "localhost:8081 says" popup. { type: 'error'|'info'|'success', message }.
  const [notice, setNotice] = useState(null);
  const noticeTimerRef = useRef(null);
  const showNotice = useCallback((message, type = 'error') => {
    setNotice({ type, message: String(message || '') });
    if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current);
    // Errors linger longer so the user can read them; info/success auto-clear.
    noticeTimerRef.current = setTimeout(
      () => setNotice(null),
      type === 'error' ? 10000 : 4000,
    );
  }, []);
  const [saving, setSaving] = useState(false);
  const [justSaved, setJustSaved] = useState(false); // transient "Saved ✓" confirmation
  const [executing, setExecuting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [showRunInput, setShowRunInput] = useState(false);
  const [runInputSchema, setRunInputSchema] = useState([]);
  const [runInputValues, setRunInputValues] = useState({});
  const [runEnvironment, setRunEnvironment] = useState('test');
  const [canvasInteractive, setCanvasInteractive] = useState(true);
  const [deployStatus, setDeployStatus] = useState('draft'); // 'draft' | 'deployed'
  const [deployingAction, setDeployingAction] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState(null);
  const [showWebhookModal, setShowWebhookModal] = useState(false);
  // Safe-deploy: confirmation gates + version history.
  const [showDeployConfirm, setShowDeployConfirm] = useState(false);
  const [showUndeployConfirm, setShowUndeployConfirm] = useState(false);
  const [deployNote, setDeployNote] = useState('');
  const [showVersions, setShowVersions] = useState(false);
  // Bump to force the loader effect to re-fetch the workflow from the server
  // (used after a rollback rewrites the live graph).
  const [reloadNonce, setReloadNonce] = useState(0);
  const [showSaveTemplateModal, setShowSaveTemplateModal] = useState(false);
  const [saveTemplateName, setSaveTemplateName] = useState('');
  const [savingTemplate, setSavingTemplate] = useState(false);
  // Open by default: the AI assistant is the primary build surface, so the
  // dock starts with the chat visible; selecting a node adds Node Settings
  // alongside it (the dock's split mode) rather than replacing it.
  const [showAIChat, setShowAIChat] = useState(true);
  const [aiChatMessages, setAiChatMessages] = useState([]);
  const [showSettings, setShowSettings] = useState(false);
  // Failure alerts. Only the owner is emailed (no arbitrary recipients);
  // failures are also logged for IT.
  const [notifications, setNotifications] = useState({
    notify_on_failure: true,
  });
  // When set, the AI chat is editing exactly one node (Phase 4).
  // Right-click → "Edit with AI" sets this; sending the message routes
  // to /edit-node instead of /refine.
  const [focusedAINodeId, setFocusedAINodeId] = useState(null);
  const executionPollRef = useRef(null);
  const justSavedTimerRef = useRef(null);
  const reactFlowInstance = useReactFlow();
  const currentWorkflowId = useRef(workflowId);
  // Version this canvas was loaded at — sent as the optimistic-concurrency
  // token on save so a stale save can't overwrite newer server state.
  const loadedVersionRef = useRef(null);
  // Workflow-level variables aren't edited on the canvas; preserve the loaded
  // value so the AI assistant snapshot carries them (full-detail input).
  const loadedVariablesRef = useRef({});
  // The workflow id whose contents are currently materialized in this canvas.
  // Used to skip the reload effect when the id changes to one we already hold
  // locally (e.g. right after we created it and promoted its id upward) so the
  // refetch doesn't clobber the live canvas.
  const loadedWorkflowIdRef = useRef(null);

  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const headerBg = isDark ? '#1e293b' : '#ffffff';
  const textColor = isDark ? '#e2e8f0' : '#1e293b';

  // ── Load schemas + existing workflow ───────────────────
  useEffect(() => {
    // Skip reloading a workflow we already hold in the canvas — e.g. right
    // after this canvas created it: handleSave sets loadedWorkflowIdRef and the
    // parent then promotes the new id to the workflowId prop. Refetching here
    // would clobber the live canvas with a redundant round-trip and flicker.
    if (workflowId && loadedWorkflowIdRef.current === workflowId) {
      setLoading(false);
      return;
    }
    (async () => {
      try {
        const { schemas: s } = await WorkflowService.getNodeSchemas();
        setSchemas(s || []);

        // Prefer the prop, but fall back to the ref so a forced reload
        // (reloadNonce bump after a rollback) still refetches even when the
        // parent hasn't promoted the in-session-created id to the prop yet.
        const wfId = workflowId || currentWorkflowId.current;
        if (wfId) {
          const wf = await WorkflowService.getWorkflow(wfId);
          loadedWorkflowIdRef.current = wf.workflow_id;
          setWorkflowName(wf.name || 'Untitled Workflow');
          setWorkflowDesc(wf.description || '');
          loadedVariablesRef.current = wf.variables || {};
          setDeployStatus(wf.status || 'draft');
          setNotifications({
            notify_on_failure: wf.notifications?.notify_on_failure !== false,
          });
          currentWorkflowId.current = wf.workflow_id;
          loadedVersionRef.current = wf.version ?? null;
          if (wf.webhook_token && wf.status === 'deployed') {
            setWebhookUrl(`${WORKFLOW_API_BASE}/api/workflows/webhook/${wf.webhook_token}`);
          }

          // Convert stored nodes → React Flow nodes
          const rfNodes = (wf.nodes || []).map((n) => ({
            id: n.id,
            type: 'workflowNode',
            position: n.position || { x: 100, y: 100 },
            data: {
              label: n.label || n.type,
              icon: _findSchema(s, n.type)?.icon || '⚙️',
              color: _findSchema(s, n.type)?.color || '#6366f1',
              category: _findSchema(s, n.type)?.category || 'processor',
              schema: _findSchema(s, n.type),
              nodeType: n.type,
              config: n.config || {},
            },
          }));
          setNodes(rfNodes);

          const rfEdges = (wf.edges || []).map((e) => {
            // Normalize backend handle IDs to match React Flow Handle component IDs.
            // Backend may store "output"/"input" (generic) while React Flow nodes
            // use "true"/"false" (for ≤2 outputs), "out-N" (for >2), and "in-N" (inputs).
            let sh = e.source_handle;
            let th = e.target_handle;
            // Generic "output" → null lets React Flow auto-connect to the single available source handle
            if (sh === 'output') sh = null;
            // "output_N" → try "out-N" (parallel split / switch) or "true"/"false" (condition)
            if (sh && /^output_\d+$/.test(sh)) {
              const idx = sh.replace('output_', '');
              sh = `out-${idx}`;
            }
            // Generic "input" → null lets React Flow auto-connect to the single available target handle
            if (th === 'input') th = null;
            // "input_N" → "in-N"
            if (th && /^input_\d+$/.test(th)) {
              th = th.replace('input_', 'in-');
            }
            return {
              id: e.id,
              source: e.source,
              target: e.target,
              sourceHandle: sh,
              targetHandle: th,
              label: e.label,
              animated: true,
              markerEnd: { type: MarkerType.ArrowClosed },
            };
          });
          setEdges(rfEdges);
        }
      } catch (err) {
        console.error('Failed to load workflow:', err);
      } finally {
        setLoading(false);
      }
    })();
  }, [workflowId, reloadNonce]);

  // ── Edge connect ───────────────────────────────────────
  const onConnect = useCallback((params) => {
    setEdges((eds) =>
      addEdge({ ...params, animated: true, markerEnd: { type: MarkerType.ArrowClosed } }, eds)
    );
    setDirty(true);
  }, []);

  // ── Node select / deselect ─────────────────────────────
  const onNodeClick = useCallback((_evt, node) => {
    setSelectedNode(node);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  // ── Drop from palette ──────────────────────────────────
  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event) => {
      event.preventDefault();
      const raw = event.dataTransfer.getData('application/json');
      if (!raw) return;
      const schema = JSON.parse(raw);
      const position = reactFlowInstance.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });
      const id = `node_${Date.now()}`;
      const newNode = {
        id,
        type: 'workflowNode',
        position,
        data: {
          label: schema.label,
          icon: schema.icon,
          color: schema.color,
          category: schema.category,
          schema,
          nodeType: schema.type,
          config: {},
        },
      };
      setNodes((nds) => [...nds, newNode]);
      setDirty(true);
    },
    [reactFlowInstance]
  );

  // ── Save ───────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const payload = {
        name: workflowName,
        description: workflowDesc,
        notifications,
        nodes: nodes.map((n) => ({
          id: n.id,
          type: n.data.nodeType,
          label: n.data.label,
          position: n.position,
          config: n.data.config || {},
        })),
        edges: edges.map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          source_handle: e.sourceHandle,
          target_handle: e.targetHandle,
          label: e.label,
        })),
      };

      // Extract schedule from scheduled_trigger node so the backend scheduler can find it
      const scheduledNode = nodes.find((n) => n.data?.nodeType === 'scheduled_trigger');
      if (scheduledNode?.data?.config?.cron_expression) {
        payload.schedule = {
          enabled: true,
          cron_expression: scheduledNode.data.config.cron_expression,
          timezone: scheduledNode.data.config.timezone || 'UTC',
        };
      }

      if (currentWorkflowId.current) {
        // Optimistic-concurrency: tell the backend which version this save
        // is built on; it rejects with 409 if the server has moved on.
        if (loadedVersionRef.current != null) {
          payload.expected_version = loadedVersionRef.current;
        }
        await WorkflowService.updateWorkflow(currentWorkflowId.current, payload);
        // Backend $inc's version by 1 on success — keep our token in sync.
        if (loadedVersionRef.current != null) loadedVersionRef.current += 1;
      } else {
        const res = await WorkflowService.createWorkflow(payload);
        currentWorkflowId.current = res.workflow_id;
        loadedVersionRef.current = 1;
        // Mark this id as already materialized so the load effect doesn't
        // refetch+clobber the canvas when the parent promotes it to workflowId.
        loadedWorkflowIdRef.current = res.workflow_id;
        // Promote the freshly-created workflow to the parent's "active"
        // workflow so the builder switches from create-mode to edit-mode
        // (subsequent saves PUT instead of POST-ing a duplicate) and the
        // surrounding shell reflects that the save succeeded — previously the
        // id lived only in a ref, so the screen looked unchanged and users
        // assumed the save had failed.
        onSaveSuccess?.(res.workflow_id, workflowName);
      }
      setDirty(false);
      // Brief positive confirmation so the user knows the save landed.
      setJustSaved(true);
      if (justSavedTimerRef.current) clearTimeout(justSavedTimerRef.current);
      justSavedTimerRef.current = setTimeout(() => setJustSaved(false), 2000);
      return true;
    } catch (err) {
      console.error('Save failed:', err);
      showNotice('Save failed: ' + err.message, 'error');
      return false;
    } finally {
      setSaving(false);
    }
  }, [nodes, edges, workflowName, workflowDesc, notifications, onSaveSuccess]);

  // ── Execute ────────────────────────────────────────────
  const handleExecute = useCallback(async () => {
    if (!currentWorkflowId.current) {
      showNotice('Save the workflow first', 'info');
      return;
    }

    // Guard: warn if the workflow has a webhook or scheduled trigger
    const hasWebhookTrigger = nodes.some((n) => n.data?.nodeType === 'webhook_trigger');
    const hasScheduledTrigger = nodes.some((n) => n.data?.nodeType === 'scheduled_trigger');
    if (hasWebhookTrigger) {
      showNotice('This workflow uses a webhook trigger. Deploy it and use the webhook URL to execute it.', 'info');
      return;
    }
    if (hasScheduledTrigger) {
      showNotice('This workflow uses a scheduled trigger. Deploy it to activate the cron schedule.', 'info');
      return;
    }

    // Block Run if any AI Agent node has no LLM Tier selected. Without this the
    // run would proceed on a silent backend default, which reads to the user as
    // "Run did nothing". Fail loud with a clear, actionable message instead.
    const agentsMissingTier = nodes.filter(
      (n) => n.data?.nodeType === 'ai_agent' && !n.data?.config?.tier
    );
    if (agentsMissingTier.length > 0) {
      const names = agentsMissingTier.map((n) => `“${n.data?.label || n.id}”`).join(', ');
      showNotice(
        `Cannot run: agent node(s) ${names} have no LLM Tier set. ` +
        `Select each agent node and choose an LLM Tier (Small, Medium, or Large) ` +
        `in the config panel, then run again.`,
        'error',
      );
      return;
    }

    if (dirty) {
      const saved = await handleSave();
      // Save already surfaced its own error; don't run against unsaved state.
      if (!saved) return;
    }

    // Check for start_node or manual_trigger with input schema
    // Always show the run dialog so user can pick environment
    const triggerNode = nodes.find((n) =>
      n.data?.nodeType === 'start_node' || n.data?.nodeType === 'manual_trigger'
    );
    const inputSchema = triggerNode?.data?.config?.input_schema;
    if (inputSchema && Array.isArray(inputSchema) && inputSchema.length > 0) {
      setRunInputSchema(inputSchema);
      const defaults = {};
      inputSchema.forEach((f) => { defaults[f.name] = f.default || ''; });
      setRunInputValues(defaults);
    } else {
      setRunInputSchema([]);
      setRunInputValues({});
    }
    setRunEnvironment('test');
    setShowRunInput(true);
  }, [dirty, handleSave, nodes]);

  const _executeWorkflow = useCallback(async (variables, environment = 'test') => {
    // Running against PROD commits real writes/sends instantly. Require an
    // explicit confirmation (the only other differentiator was the button
    // colour), and list the write/output nodes that will actually execute so
    // the operator sees the blast radius before committing (prod-readiness #12).
    if (environment === 'prod' && Platform.OS === 'web' && typeof window !== 'undefined') {
      const WRITE_TYPES = new Set([
        'sql_writer', 'mongo_writer', 'bucket_writer', 'sftp_writer',
        'email_sender', 'webhook_output', 'dept_mcp_action',
      ]);
      let writers = [];
      try {
        writers = (reactFlowInstance.getNodes() || [])
          .filter((n) => WRITE_TYPES.has(n?.data?.nodeType))
          .map((n) => n?.data?.label || n?.data?.nodeType);
      } catch { /* node read best-effort — still confirm below */ }
      const writerLines = writers.length
        ? `\n\nThe following live actions will be COMMITTED for real:\n${writers.map((w) => `  • ${w}`).join('\n')}`
        : '';
      const ok = window.confirm(
        `⚠️ Run against PRODUCTION?\n\nThis runs the workflow against your live ` +
        `production systems — any writes, sends, or external calls are real and ` +
        `cannot be undone.${writerLines}\n\nContinue?`
      );
      if (!ok) return;
    }
    setShowRunInput(false);
    setExecuting(true);
    try {
      const body = { environment };
      if (Object.keys(variables).length > 0) body.variables = variables;
      const result = await WorkflowService.executeWorkflow(
        currentWorkflowId.current,
        body
      );
      setCurrentExecution(result.execution_id);
      setShowExecution(true);
      _startExecutionPolling(result.execution_id);
    } catch (err) {
      console.error('Execute failed:', err);
      if (err.isCapacity) {
        showNotice('⏳ ' + err.message, 'error');
      } else {
        showNotice('Execution failed: ' + err.message, 'error');
      }
    } finally {
      setExecuting(false);
    }
  }, [reactFlowInstance]);

  // ── Deploy / Undeploy ──────────────────────────────────
  // Deploying goes LIVE against production, and undeploying stops live
  // automation — both are gated behind an explicit confirmation rather than
  // firing on a single tap (prod-readiness UI finding). The button handler
  // only opens the relevant confirm modal; the confirm modal does the work.
  const handleDeployClick = useCallback(() => {
    if (!currentWorkflowId.current) {
      showNotice('Save the workflow first', 'info');
      return;
    }
    if (deployStatus === 'deployed') {
      // Undeploy is a server-side stop — it needs no save, so it must NOT be
      // gated on the dirty flag.
      setShowUndeployConfirm(true);
    } else {
      // Deploy ships what's on the server, so unsaved local edits would be
      // silently excluded — make the user save first.
      if (dirty) {
        showNotice('Save your changes before deploying', 'info');
        return;
      }
      setDeployNote('');
      setShowDeployConfirm(true);
    }
  }, [deployStatus, dirty]);

  const confirmDeploy = useCallback(async () => {
    if (!currentWorkflowId.current) return;
    setDeployingAction(true);
    try {
      const result = await WorkflowService.deployWorkflow(currentWorkflowId.current, deployNote.trim());
      setDeployStatus('deployed');
      setShowDeployConfirm(false);
      const v = result?.deployed_version;
      showNotice(v ? `Deployed v${v} to production` : 'Deployed to production', 'success');
      if (result?.webhook_url) {
        const fullUrl = `${WORKFLOW_API_BASE}${result.webhook_url}`;
        setWebhookUrl(fullUrl);
        setShowWebhookModal(true);
      }
    } catch (err) {
      showNotice(`Deploy failed: ${err.message}`, 'error');
    } finally {
      setDeployingAction(false);
    }
  }, [deployNote]);

  const confirmUndeploy = useCallback(async () => {
    if (!currentWorkflowId.current) return;
    setDeployingAction(true);
    try {
      await WorkflowService.undeployWorkflow(currentWorkflowId.current);
      setDeployStatus('draft');
      setWebhookUrl(null);
      setShowUndeployConfirm(false);
      showNotice('Workflow undeployed — live automation stopped', 'success');
    } catch (err) {
      showNotice(`Undeploy failed: ${err.message}`, 'error');
    } finally {
      setDeployingAction(false);
    }
  }, []);

  // After a rollback rewrites the live graph server-side, re-fetch so the
  // canvas reflects the restored nodes/edges/status.
  const handleRolledBack = useCallback((result) => {
    loadedWorkflowIdRef.current = null;   // bypass the "already loaded" guard
    setReloadNonce((n) => n + 1);
    const restored = result?.restored_from_version;
    showNotice(restored ? `Rolled back to v${restored}` : 'Rollback complete', 'success');
  }, []);

  // Output/write-capable node types whose presence makes a prod deploy
  // consequential — surfaced in the deploy confirmation so the operator sees
  // exactly which live writes/sends will run.
  const writeNodeLabels = useMemo(() => {
    const WRITE_HINTS = ['writer', 'sink', 'sender', 'email', 'action', 'mcp_action', 'http_request', 'webhook_call'];
    return nodes
      .filter((n) => {
        const t = (n.data?.nodeType || '').toLowerCase();
        return WRITE_HINTS.some((h) => t.includes(h));
      })
      .map((n) => n.data?.label || n.data?.nodeType)
      .filter(Boolean);
  }, [nodes]);

  // ── Live execution polling ─────────────────────────────
  const _startExecutionPolling = useCallback((executionId) => {
    if (executionPollRef.current) clearInterval(executionPollRef.current);
    executionPollRef.current = setInterval(async () => {
      try {
        const status = await WorkflowService.getExecutionStatus(executionId);
        const nodeStatuses = status.node_statuses || {};
        setNodes((nds) =>
          nds.map((n) => {
            const s = nodeStatuses[n.id];
            if (s && n.data.executionStatus !== s) {
              return { ...n, data: { ...n.data, executionStatus: s } };
            }
            return n;
          })
        );
        // `paused` is NOT terminal — the execution resumes after a human
        // approval. Keep polling so the canvas reflects completion; just
        // surface the approval monitor. Only stop on genuine terminal
        // states, otherwise the canvas freezes forever post-approval.
        const TERMINAL = ['completed', 'failed', 'timed_out', 'cancelled', 'error'];
        if (status.status === 'paused') {
          setShowExecution(true);
        } else if (TERMINAL.includes(status.status)) {
          clearInterval(executionPollRef.current);
          executionPollRef.current = null;
        }
      } catch { /* ignore poll errors */ }
    }, 5000);
  }, []);

  // Clean up polling + transient timers on unmount
  useEffect(() => {
    return () => {
      if (executionPollRef.current) clearInterval(executionPollRef.current);
      if (justSavedTimerRef.current) clearTimeout(justSavedTimerRef.current);
    };
  }, []);

  // ── Update node config ─────────────────────────────────
  const updateNodeConfig = useCallback((nodeId, config) => {
    setNodes((nds) =>
      nds.map((n) =>
        n.id === nodeId
          ? { ...n, data: { ...n.data, config: { ...n.data.config, ...config } } }
          : n
      )
    );
    setDirty(true);
  }, []);

  // ── Update node label ──────────────────────────────────
  // Keeps both the canvas node and the selectedNode (which drives the
  // config panel header) in sync so the rename shows immediately.
  const updateNodeLabel = useCallback((nodeId, label) => {
    setNodes((nds) =>
      nds.map((n) =>
        n.id === nodeId ? { ...n, data: { ...n.data, label } } : n
      )
    );
    setSelectedNode((prev) =>
      prev && prev.id === nodeId
        ? { ...prev, data: { ...prev.data, label } }
        : prev
    );
    setDirty(true);
  }, []);

  // ── Unsaved changes protection ─────────────────────────
  useEffect(() => {
    if (Platform.OS !== 'web') return;
    const handler = (e) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  // ── AI Chat helpers ─────────────────────────────────────
  const getCurrentWorkflowSnapshot = useCallback(() => {
    // Send the assistant the COMPLETE current canvas — every node with its full,
    // live config (the user's latest internal edits), edges with handles,
    // variables and meta. Nothing is summarised here.
    return {
      workflow_id: currentWorkflowId.current || workflowId || null,
      name: workflowName,
      description: workflowDesc,
      nodes: nodes.map((n) => ({
        id: n.id,
        type: n.data.nodeType,
        label: n.data.label,
        position: n.position,
        config: n.data.config || {},
      })),
      edges: edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        source_handle: e.sourceHandle,
        target_handle: e.targetHandle,
        label: e.label,
      })),
      variables: loadedVariablesRef.current || {},
    };
  }, [nodes, edges, workflowName, workflowDesc, workflowId]);

  const handleApplyAIWorkflow = useCallback((workflow) => {
    // Full-replace of the canvas. If the user has hand-built nodes,
    // confirm before discarding them — the AI "generate" result replaces
    // everything (the "diff" path preserves edits; this one does not).
    if (nodes.length > 0 && Platform.OS === 'web') {
      const ok = window.confirm(
        `Apply this AI workflow? It replaces all ${nodes.length} node(s) `
        + `currently on the canvas — any manual edits will be lost.`
      );
      if (!ok) return;
    }
    const s = schemas;
    const rfNodes = (workflow.nodes || []).map((n) => ({
      id: n.id,
      type: 'workflowNode',
      position: n.position || { x: 100, y: 100 },
      data: {
        label: n.label || n.type,
        icon: _findSchema(s, n.type)?.icon || '⚙️',
        color: _findSchema(s, n.type)?.color || '#6366f1',
        category: _findSchema(s, n.type)?.category || 'processor',
        schema: _findSchema(s, n.type),
        nodeType: n.type,
        config: n.config || {},
      },
    }));

    const nodeIds = new Set(rfNodes.map((n) => n.id));
    const specsByNode = {};
    rfNodes.forEach((n) => {
      specsByNode[n.id] = getWorkflowNodeOutputSpecs({
        nodeType: n.data.nodeType, schema: n.data.schema, config: n.data.config,
      });
    });
    const rfEdges = (workflow.edges || [])
      .filter((e) => {
        const ok = nodeIds.has(e.source) && nodeIds.has(e.target);
        if (!ok) console.warn('[WorkflowAI] dropping edge with unknown endpoint:', e);
        return ok;
      })
      .map((e) => ({
        id: e.id || `${e.source}->${e.target}`,
        source: e.source,
        target: e.target,
        sourceHandle: _resolveSourceHandle(specsByNode[e.source], e.source_handle),
        targetHandle: _normTargetHandle(e.target_handle),
        label: e.label,
        animated: true,
        markerEnd: { type: MarkerType.ArrowClosed },
      }));

    setNodes(rfNodes);
    setEdges(rfEdges);
    if (workflow.name) setWorkflowName(workflow.name);
    if (workflow.description) setWorkflowDesc(workflow.description);
    setDirty(true);
    setTimeout(() => reactFlowInstance.fitView({ padding: 0.15 }), 200);
  }, [schemas, reactFlowInstance, nodes.length]);

  // Apply a diff patch from the AI's refine endpoint. Preserves the
  // user's manual edits between AI turns — only nodes the AI changed
  // get touched.
  const handleApplyDiff = useCallback((diff) => {
    if (!diff) return;
    const s = schemas;

    const buildRfNode = (n) => ({
      id: n.id,
      type: 'workflowNode',
      position: n.position || { x: 100, y: 100 },
      data: {
        label: n.label || n.type,
        icon: _findSchema(s, n.type)?.icon || '⚙️',
        color: _findSchema(s, n.type)?.color || '#6366f1',
        category: _findSchema(s, n.type)?.category || 'processor',
        schema: _findSchema(s, n.type),
        nodeType: n.type,
        config: n.config || {},
      },
    });

    const removedNodeIds = new Set(diff.nodes_removed || []);
    const updatedById = Object.fromEntries(
      (diff.nodes_updated || []).map((n) => [n.id, n])
    );

    setNodes((prev) => {
      const kept = prev
        .filter((n) => !removedNodeIds.has(n.id))
        .map((n) => {
          const upd = updatedById[n.id];
          if (!upd) return n;
          return {
            ...n,
            data: {
              ...n.data,
              label: upd.label || n.data.label,
              nodeType: upd.type || n.data.nodeType,
              config: upd.config || n.data.config,
              schema: _findSchema(s, upd.type) || n.data.schema,
              icon: _findSchema(s, upd.type)?.icon || n.data.icon,
              color: _findSchema(s, upd.type)?.color || n.data.color,
            },
          };
        });
      const added = (diff.nodes_added || []).map(buildRfNode);
      return [...kept, ...added];
    });

    const removedEdgeIds = new Set(diff.edges_removed || []);
    const addedNodeIds = new Set((diff.nodes_added || []).map((n) => n.id));
    const existingNodeIds = new Set(nodes.map((n) => n.id));
    const availableNodeIds = new Set(
      [...existingNodeIds, ...addedNodeIds].filter((id) => !removedNodeIds.has(id))
    );
    setEdges((prev) => {
      const kept = prev.filter((e) =>
        !removedEdgeIds.has(e.id)
        && !removedNodeIds.has(e.source)
        && !removedNodeIds.has(e.target)
        && availableNodeIds.has(e.source)
        && availableNodeIds.has(e.target)
      );
      // Source-node output specs (existing canvas nodes + added/updated nodes)
      // so each edge's handle resolves to one the node actually renders.
      const specsByNode = {};
      nodes.forEach((n) => {
        specsByNode[n.id] = getWorkflowNodeOutputSpecs({
          nodeType: n.data.nodeType, schema: n.data.schema, config: n.data.config,
        });
      });
      [...(diff.nodes_added || []), ...(diff.nodes_updated || [])].forEach((n) => {
        specsByNode[n.id] = getWorkflowNodeOutputSpecs({
          nodeType: n.type, schema: _findSchema(s, n.type), config: n.config || {},
        });
      });
      const added = (diff.edges_added || []).filter((e) => {
        const ok = availableNodeIds.has(e.source) && availableNodeIds.has(e.target);
        if (!ok) console.warn('[WorkflowAI] dropping diff edge with unknown endpoint:', e);
        return ok;
      }).map((e) => ({
        id: e.id || `${e.source}->${e.target}`,
        source: e.source, target: e.target,
        sourceHandle: _resolveSourceHandle(specsByNode[e.source], e.source_handle),
        targetHandle: _normTargetHandle(e.target_handle),
        label: e.label,
        animated: true, markerEnd: { type: MarkerType.ArrowClosed },
      }));
      return [...kept, ...added];
    });

    setDirty(true);
    setTimeout(() => reactFlowInstance.fitView({ padding: 0.15 }), 200);
  }, [schemas, reactFlowInstance, nodes]);

  // Apply a single-node edit from the AI's edit-node endpoint.
  const handleApplyNodeEdit = useCallback((nodeId, newNode) => {
    if (!nodeId || !newNode) return;
    const s = schemas;
    setNodes((prev) =>
      prev.map((n) => {
        if (n.id !== nodeId) return n;
        return {
          ...n,
          position: newNode.position || n.position,
          data: {
            ...n.data,
            label: newNode.label || n.data.label,
            nodeType: newNode.type || n.data.nodeType,
            config: newNode.config || n.data.config,
            schema: _findSchema(s, newNode.type) || n.data.schema,
            icon: _findSchema(s, newNode.type)?.icon || n.data.icon,
            color: _findSchema(s, newNode.type)?.color || n.data.color,
          },
        };
      })
    );
    setDirty(true);
  }, [schemas]);

  // ── Delete selected node(s) ────────────────────────────
  // Targets every React-Flow-selected node (box-select / shift-click) and
  // falls back to the single tracked `selectedNode` so the trash button
  // still works when only the click-tracked node is set.
  const deleteSelectedNode = useCallback(() => {
    const flowSelectedNodes = nodes.filter((n) => n.selected);
    const flowSelectedEdges = edges.filter((e) => e.selected);
    const nodesToDelete = flowSelectedNodes.length
      ? flowSelectedNodes
      : (selectedNode ? [selectedNode] : []);
    if (nodesToDelete.length === 0 && flowSelectedEdges.length === 0) return;

    if (Platform.OS === 'web') {
      const label = nodesToDelete.length === 1
        ? `node "${nodesToDelete[0].data?.label || nodesToDelete[0].id}"`
        : `${nodesToDelete.length} nodes`;
      const target = nodesToDelete.length ? label : `${flowSelectedEdges.length} edge(s)`;
      if (!window.confirm(`Delete ${target}?`)) return;
    }

    reactFlowInstance.deleteElements({
      nodes: nodesToDelete.map((n) => ({ id: n.id })),
      edges: flowSelectedEdges.map((e) => ({ id: e.id })),
    });
    setSelectedNode(null);
    setDirty(true);
  }, [selectedNode, nodes, edges, reactFlowInstance]);

  // Keep our tracked selectedNode in sync when React Flow deletes nodes
  // (e.g. via the Delete key or canvas-driven removals).
  const handleNodesDelete = useCallback((deletedNodes) => {
    if (selectedNode && deletedNodes.some((n) => n.id === selectedNode.id)) {
      setSelectedNode(null);
    }
    setDirty(true);
  }, [selectedNode]);

  if (loading) {
    return (
      <View style={[styles.container, { backgroundColor: bg }]}>
        <ActivityIndicator size="large" color="#3b82f6" />
      </View>
    );
  }

  return (
    <View style={[styles.container, { backgroundColor: bg }]}>
      {/* ── Inline notice banner (replaces window.alert) ─────────────── */}
      {notice && (
        <View
          pointerEvents="box-none"
          style={{
            position: 'absolute', top: 56, left: 0, right: 0, zIndex: 10000,
            alignItems: 'center',
          }}
        >
          <View
            style={{
              flexDirection: 'row', alignItems: 'center', maxWidth: 720,
              paddingVertical: 10, paddingHorizontal: 14, borderRadius: 10,
              backgroundColor: notice.type === 'error' ? '#7f1d1d'
                : notice.type === 'success' ? '#14532d' : '#1e3a8a',
              borderWidth: 1,
              borderColor: notice.type === 'error' ? '#ef4444'
                : notice.type === 'success' ? '#22c55e' : '#3b82f6',
              shadowColor: '#000', shadowOpacity: 0.3, shadowRadius: 8,
              shadowOffset: { width: 0, height: 2 }, elevation: 6,
            }}
          >
            <Ionicons
              name={notice.type === 'error' ? 'alert-circle'
                : notice.type === 'success' ? 'checkmark-circle' : 'information-circle'}
              size={18} color="#fff" style={{ marginRight: 8 }}
            />
            <Text style={{ color: '#fff', flexShrink: 1, fontSize: 13 }}>{notice.message}</Text>
            <TouchableOpacity
              onPress={() => setNotice(null)}
              accessibilityRole="button"
              accessibilityLabel="Dismiss notice"
              style={{ marginLeft: 10, padding: 2 }}
            >
              <Ionicons name="close" size={16} color="#fff" />
            </TouchableOpacity>
          </View>
        </View>
      )}
      {/* Force a visible cursor on the web canvas regardless of OS pointer theme. */}
      {Platform.OS === 'web' && (
        <style>{`
          .react-flow,
          .react-flow__renderer,
          .react-flow__pane,
          .react-flow__viewport,
          .react-flow__background,
          .react-flow__controls,
          .react-flow__minimap {
            cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 20 20'%3E%3Cpath fill='%23000' stroke='%23fff' stroke-width='0.8' d='M3 2l0 15 4.5-4 2.7 4.3 2-1.2-2.7-4.4 5.2-.1z'/%3E%3C/svg%3E") 2 2, default !important;
          }

          .react-flow__node {
            cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' stroke='%23fff' stroke-width='0.8' d='M7 11V8.5C7 5.46 9.46 3 12.5 3S18 5.46 18 8.5V11h1a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-1v2.5a1 1 0 0 1-1.45.9L12 16.12 7.45 18.4A1 1 0 0 1 6 17.5V15H5a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1h1z'/%3E%3C/svg%3E") 12 12, grab !important;
          }

          .react-flow__node.dragging {
            cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' stroke='%23fff' stroke-width='0.8' d='M9 4h2v4h2V4h2v5h1a2 2 0 0 1 2 2v5.5A3.5 3.5 0 0 1 14.5 20h-2A4.5 4.5 0 0 1 8 15.5V9h1z'/%3E%3C/svg%3E") 12 12, grabbing !important;
          }

          input,
          textarea,
          [contenteditable='true'] {
            caret-color: ${isDark ? '#e2e8f0' : '#000'} !important;
            cursor: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath fill='%23000' d='M11 2h2v20h-2z'/%3E%3Cpath fill='%23000' d='M8 2h8v2H8z'/%3E%3Cpath fill='%23000' d='M8 20h8v2H8z'/%3E%3C/svg%3E") 12 12, text !important;
          }
        `}</style>
      )}
      {/* ── Header / Toolbar ─────────────────────────────── */}
      <View style={[styles.header, { backgroundColor: headerBg, borderBottomColor: isDark ? '#334155' : '#e2e8f0' }]}>
        <TouchableOpacity
          onPress={onClose}
          style={styles.backBtn}
          accessibilityRole="button"
          accessibilityLabel="Back to workflow list"
        >
          <Ionicons name="arrow-back" size={20} color={textColor} />
        </TouchableOpacity>

        <TextInput
          value={workflowName}
          onChangeText={(t) => { setWorkflowName(t); setDirty(true); }}
          style={[styles.titleInput, { color: textColor }]}
          placeholder="Workflow name"
          placeholderTextColor="#94a3b8"
        />

        <View style={styles.toolbarActions}>
          <TouchableOpacity
            style={[styles.toolBtn, { opacity: showPalette ? 1 : 0.5 }]}
            onPress={() => setShowPalette(!showPalette)}
            accessibilityRole="button"
            accessibilityLabel={showPalette ? 'Hide node palette' : 'Show node palette'}
          >
            <Ionicons name="grid-outline" size={18} color={textColor} />
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.toolBtn, showAIChat && { backgroundColor: isDark ? '#312e81' : '#eef2ff', borderRadius: 8 }]}
            onPress={() => setShowAIChat((v) => !v)}
            title="AI Assistant"
            accessibilityRole="button"
            accessibilityLabel={showAIChat ? 'Close AI assistant' : 'Open AI assistant'}
          >
            <Ionicons name="sparkles" size={18} color={showAIChat ? '#8b5cf6' : textColor} />
            {aiChatMessages.length > 0 && !showAIChat && (
              <View style={styles.aiBadge}>
                <Text style={styles.aiBadgeText}>{aiChatMessages.length}</Text>
              </View>
            )}
          </TouchableOpacity>

          {onShowGuide && (
            <TouchableOpacity
              style={styles.toolBtn}
              onPress={onShowGuide}
              accessibilityRole="button"
              accessibilityLabel="Show help guide"
            >
              <Ionicons name="help-circle-outline" size={18} color={textColor} />
            </TouchableOpacity>
          )}

          <TouchableOpacity
            style={styles.toolBtn}
            onPress={() => setShowSettings(true)}
            title="Workflow settings — failure alerts"
            accessibilityRole="button"
            accessibilityLabel="Workflow settings"
          >
            <Ionicons name="settings-outline" size={18} color={textColor} />
          </TouchableOpacity>

          {selectedNode && (
            <TouchableOpacity
              style={styles.toolBtn}
              onPress={deleteSelectedNode}
              accessibilityRole="button"
              accessibilityLabel="Delete selected node"
            >
              <Ionicons name="trash-outline" size={18} color="#ef4444" />
            </TouchableOpacity>
          )}

          <TouchableOpacity
            style={[styles.saveBtn, saving && { opacity: 0.5 }, justSaved && { backgroundColor: '#22c55e' }]}
            onPress={handleSave}
            disabled={saving}
            accessibilityRole="button"
            accessibilityLabel={justSaved ? 'Workflow saved' : 'Save workflow'}
          >
            {saving ? (
              <ActivityIndicator size="small" color="#fff" />
            ) : justSaved ? (
              <>
                <Ionicons name="checkmark" size={16} color="#fff" />
                <Text style={styles.saveBtnText}>Saved</Text>
              </>
            ) : (
              <>
                <Ionicons name="save-outline" size={16} color="#fff" />
                <Text style={styles.saveBtnText}>Save</Text>
              </>
            )}
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.toolBtn, { opacity: 0.7 }]}
            onPress={() => setShowSaveTemplateModal(true)}
            title="Save as Template"
            accessibilityRole="button"
            accessibilityLabel="Save as template"
          >
            <Ionicons name="bookmark-outline" size={18} color={textColor} />
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.runBtn, executing && { opacity: 0.5 }]}
            onPress={handleExecute}
            disabled={executing}
            accessibilityRole="button"
            accessibilityLabel="Run workflow"
          >
            {executing ? (
              <ActivityIndicator size="small" color="#fff" />
            ) : (
              <>
                <Ionicons name="play" size={16} color="#fff" />
                <Text style={styles.runBtnText}>Run</Text>
              </>
            )}
          </TouchableOpacity>

          {currentWorkflowId.current && onShowRuns && (
            <TouchableOpacity
              style={styles.runsBtn}
              onPress={() => onShowRuns(currentWorkflowId.current)}
              title="Run history"
              accessibilityRole="button"
              accessibilityLabel="View run history"
              testID="canvas-runs-btn"
            >
              <Ionicons name="time-outline" size={16} color={textColor} />
              <Text style={[styles.runsBtnText, { color: textColor }]}>Runs</Text>
            </TouchableOpacity>
          )}

          {webhookUrl && deployStatus === 'deployed' && (
            <TouchableOpacity
              style={[styles.deployBtn, { backgroundColor: '#0891b2', flexDirection: 'row', gap: 4 }]}
              onPress={() => setShowWebhookModal(true)}
              accessibilityRole="button"
              accessibilityLabel="Show webhook URL"
            >
              <Ionicons name="link-outline" size={16} color="#fff" />
              <Text style={styles.deployBtnText}>Webhook URL</Text>
            </TouchableOpacity>
          )}

          {currentWorkflowId.current && (
            <TouchableOpacity
              style={styles.runsBtn}
              onPress={() => setShowVersions(true)}
              title="Deploy history"
              accessibilityRole="button"
              accessibilityLabel="View deploy history and roll back"
              testID="canvas-versions-btn"
            >
              <Ionicons name="git-branch-outline" size={16} color={textColor} />
              <Text style={[styles.runsBtnText, { color: textColor }]}>History</Text>
            </TouchableOpacity>
          )}

          {currentWorkflowId.current && (
            <TouchableOpacity
              style={[
                styles.deployBtn,
                deployStatus === 'deployed' ? styles.deployBtnLive : styles.deployBtnDraft,
                deployingAction && { opacity: 0.5 },
              ]}
              onPress={handleDeployClick}
              disabled={deployingAction}
              accessibilityRole="button"
              accessibilityLabel={deployStatus === 'deployed' ? 'Undeploy workflow' : 'Deploy workflow'}
            >
              {deployingAction ? (
                <ActivityIndicator size="small" color="#fff" />
              ) : (
                <>
                  <Ionicons
                    name={deployStatus === 'deployed' ? 'stop-circle-outline' : 'rocket-outline'}
                    size={16}
                    color="#fff"
                  />
                  <Text style={styles.deployBtnText}>
                    {deployStatus === 'deployed' ? 'Undeploy' : 'Deploy'}
                  </Text>
                </>
              )}
            </TouchableOpacity>
          )}
        </View>
      </View>

      {/* ── Main area: Palette + Canvas + Config ──────────── */}
      <View style={styles.body}>
        {showPalette && (
          <NodePalette schemas={schemas} theme={theme} width={paletteWidth} />
        )}
        {showPalette && Platform.OS === 'web' && (
          <Splitter orientation="vertical" onDelta={onPaletteDelta} theme={theme} />
        )}

        <View style={styles.canvasWrap}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            onNodeContextMenu={(evt, node) => {
              evt.preventDefault?.();
              // Right-click a node → focus the AI chat on it. Opens the
              // panel if closed; sets focusedAINodeId so the next send
              // routes to /edit-node.
              setFocusedAINodeId(node.id);
              setShowAIChat(true);
            }}
            onPaneClick={onPaneClick}
            onNodesDelete={handleNodesDelete}
            onDragOver={onDragOver}
            onDrop={onDrop}
            nodeTypes={nodeTypes}
            nodesDraggable={canvasInteractive}
            nodesConnectable={canvasInteractive}
            elementsSelectable={canvasInteractive}
            panOnDrag={canvasInteractive}
            zoomOnScroll={canvasInteractive}
            zoomOnPinch={canvasInteractive}
            zoomOnDoubleClick={canvasInteractive}
            fitView
            deleteKeyCode={['Delete', 'Backspace']}
            style={{ width: '100%', height: '100%' }}
          >
            <Background color={isDark ? '#334155' : '#e2e8f0'} gap={20} />
            <MiniMap
              nodeColor={(n) => n.data?.color || '#6366f1'}
              style={{ background: isDark ? '#1e293b' : '#f1f5f9' }}
            />
          </ReactFlow>

          <View style={styles.flowControls}>
            <TouchableOpacity
              onPress={() => reactFlowInstance.zoomIn()}
              style={[styles.flowControlButton, { backgroundColor: isDark ? '#0f172a' : '#ffffff', borderColor: isDark ? '#334155' : '#cbd5e1' }]}
              accessibilityRole="button"
              accessibilityLabel="Zoom in"
            >
              <Ionicons name="add" size={18} color={isDark ? '#e2e8f0' : '#1e293b'} />
            </TouchableOpacity>
            <TouchableOpacity
              onPress={() => reactFlowInstance.zoomOut()}
              style={[styles.flowControlButton, { backgroundColor: isDark ? '#0f172a' : '#ffffff', borderColor: isDark ? '#334155' : '#cbd5e1' }]}
              accessibilityRole="button"
              accessibilityLabel="Zoom out"
            >
              <Ionicons name="remove" size={18} color={isDark ? '#e2e8f0' : '#1e293b'} />
            </TouchableOpacity>
            <TouchableOpacity
              onPress={() => reactFlowInstance.fitView({ padding: 0.15 })}
              style={[styles.flowControlButton, { backgroundColor: isDark ? '#0f172a' : '#ffffff', borderColor: isDark ? '#334155' : '#cbd5e1' }]}
              accessibilityRole="button"
              accessibilityLabel="Fit workflow to view"
            >
              <Ionicons name="scan-outline" size={16} color={isDark ? '#e2e8f0' : '#1e293b'} />
            </TouchableOpacity>
            <TouchableOpacity
              onPress={() => setCanvasInteractive((value) => !value)}
              accessibilityRole="button"
              accessibilityLabel={canvasInteractive ? 'Lock canvas' : 'Unlock canvas'}
              style={[
                styles.flowControlButton,
                styles.flowControlToggle,
                canvasInteractive
                  ? { backgroundColor: '#2563eb', borderColor: '#2563eb' }
                  : { backgroundColor: isDark ? '#0f172a' : '#ffffff', borderColor: isDark ? '#334155' : '#cbd5e1' },
              ]}
            >
              <Ionicons
                name={canvasInteractive ? 'hand-left' : 'lock-closed'}
                size={16}
                color={canvasInteractive ? '#ffffff' : (isDark ? '#e2e8f0' : '#1e293b')}
              />
              <Text
                style={[
                  styles.flowControlToggleText,
                  !canvasInteractive && { color: isDark ? '#e2e8f0' : '#1e293b' },
                ]}
              >
                {canvasInteractive ? 'Interactive' : 'Locked'}
              </Text>
            </TouchableOpacity>
          </View>
        </View>

        {(selectedNode || showAIChat) && (
          <WorkflowRightDock
            theme={theme}
            node={selectedNode}
            schemas={schemas}
            onUpdateConfig={(config) => selectedNode && updateNodeConfig(selectedNode.id, config)}
            onUpdateLabel={(label) => selectedNode && updateNodeLabel(selectedNode.id, label)}
            onCloseNode={() => setSelectedNode(null)}
            showAI={showAIChat}
            onCloseAI={() => setShowAIChat(false)}
            aiProps={{
              messages: aiChatMessages,
              setMessages: setAiChatMessages,
              getWorkflowSnapshot: getCurrentWorkflowSnapshot,
              onApplyWorkflow: handleApplyAIWorkflow,
              onApplyDiff: handleApplyDiff,
              onApplyNodeEdit: handleApplyNodeEdit,
              focusedNodeId: focusedAINodeId,
              onClearFocusedNode: () => setFocusedAINodeId(null),
            }}
          />
        )}
      </View>

      {/* ── Run Input Form ─────────────────────────────────── */}
      {showRunInput && (
        <Modal visible transparent animationType="fade" onRequestClose={() => setShowRunInput(false)}>
          <View style={styles.runInputOverlay}>
            <View style={[styles.runInputCard, { backgroundColor: isDark ? '#1e293b' : '#fff' }]}>
              <Text style={[styles.runInputTitle, { color: textColor }]}>Workflow Inputs</Text>
              <Text style={{ color: isDark ? '#94a3b8' : '#64748b', marginBottom: 16, fontSize: 13 }}>
                {runInputSchema.length > 0 ? 'Provide values and select environment.' : 'Select which environment to run against.'}
              </Text>

              {/* Environment selector */}
              <Text style={{ color: textColor, fontWeight: '600', fontSize: 13, marginBottom: 6 }}>Environment</Text>
              <View style={{ flexDirection: 'row', gap: 8, marginBottom: 16 }}>
                <TouchableOpacity
                  onPress={() => setRunEnvironment('test')}
                  accessibilityRole="button"
                  accessibilityLabel="Select test environment"
                  accessibilityState={{ selected: runEnvironment === 'test' }}
                  style={{
                    flex: 1, flexDirection: 'row', alignItems: 'center', justifyContent: 'center',
                    gap: 6, paddingVertical: 10, borderRadius: 8, borderWidth: 1.5,
                    borderColor: runEnvironment === 'test' ? '#3b82f6' : (isDark ? '#475569' : '#e2e8f0'),
                    backgroundColor: runEnvironment === 'test' ? '#3b82f615' : 'transparent',
                  }}
                >
                  <Ionicons name="flask-outline" size={16} color={runEnvironment === 'test' ? '#3b82f6' : '#94a3b8'} />
                  <Text style={{ fontWeight: '600', color: runEnvironment === 'test' ? '#3b82f6' : '#94a3b8' }}>Test</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  onPress={() => setRunEnvironment('prod')}
                  accessibilityRole="button"
                  accessibilityLabel="Select production environment"
                  accessibilityState={{ selected: runEnvironment === 'prod' }}
                  style={{
                    flex: 1, flexDirection: 'row', alignItems: 'center', justifyContent: 'center',
                    gap: 6, paddingVertical: 10, borderRadius: 8, borderWidth: 1.5,
                    borderColor: runEnvironment === 'prod' ? '#22c55e' : (isDark ? '#475569' : '#e2e8f0'),
                    backgroundColor: runEnvironment === 'prod' ? '#22c55e15' : 'transparent',
                  }}
                >
                  <Ionicons name="rocket-outline" size={16} color={runEnvironment === 'prod' ? '#22c55e' : '#94a3b8'} />
                  <Text style={{ fontWeight: '600', color: runEnvironment === 'prod' ? '#22c55e' : '#94a3b8' }}>Production</Text>
                </TouchableOpacity>
              </View>

              <ScrollView style={{ maxHeight: 320 }}>
                {runInputSchema.map((field) => (
                  <View key={field.name} style={{ marginBottom: 14 }}>
                    <Text style={{ color: textColor, fontWeight: '600', fontSize: 13, marginBottom: 4 }}>
                      {field.name}{field.required ? ' *' : ''}
                    </Text>
                    {field.type === 'boolean' ? (
                      <TouchableOpacity
                        onPress={() => setRunInputValues((v) => ({ ...v, [field.name]: !v[field.name] }))}
                        style={{
                          flexDirection: 'row', alignItems: 'center', gap: 8,
                          padding: 10, borderRadius: 8,
                          backgroundColor: isDark ? '#334155' : '#f1f5f9',
                        }}
                      >
                        <Ionicons
                          name={runInputValues[field.name] ? 'checkbox' : 'square-outline'}
                          size={20}
                          color={runInputValues[field.name] ? '#3b82f6' : '#94a3b8'}
                        />
                        <Text style={{ color: textColor }}>{runInputValues[field.name] ? 'True' : 'False'}</Text>
                      </TouchableOpacity>
                    ) : (
                      <TextInput
                        value={String(runInputValues[field.name] || '')}
                        onChangeText={(t) => setRunInputValues((v) => ({ ...v, [field.name]: t }))}
                        placeholder={`Enter ${field.name} (${field.type || 'text'})`}
                        placeholderTextColor="#94a3b8"
                        multiline={field.type === 'json'}
                        numberOfLines={field.type === 'json' ? 3 : 1}
                        style={[
                          styles.runInputField,
                          {
                            color: textColor,
                            backgroundColor: isDark ? '#334155' : '#f1f5f9',
                            minHeight: field.type === 'json' ? 70 : 40,
                          },
                        ]}
                      />
                    )}
                  </View>
                ))}
              </ScrollView>
              <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 10, marginTop: 12 }}>
                <TouchableOpacity
                  onPress={() => setShowRunInput(false)}
                  style={[styles.runInputCancel, { borderColor: isDark ? '#475569' : '#cbd5e1' }]}
                  accessibilityRole="button"
                  accessibilityLabel="Cancel run"
                >
                  <Text style={{ color: textColor }}>Cancel</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  onPress={() => _executeWorkflow(runInputValues, runEnvironment)}
                  style={[styles.runInputRunBtn, runEnvironment === 'prod' && { backgroundColor: '#22c55e' }]}
                  accessibilityRole="button"
                  accessibilityLabel={`Run workflow in ${runEnvironment === 'prod' ? 'production' : 'test'}`}
                >
                  <Ionicons name="play" size={16} color="#fff" />
                  <Text style={{ color: '#fff', fontWeight: '600' }}>
                    Run ({runEnvironment === 'prod' ? 'Prod' : 'Test'})
                  </Text>
                </TouchableOpacity>
              </View>
            </View>
          </View>
        </Modal>
      )}

      {/* ── Webhook URL Modal ──────────────────────────────── */}
      {showWebhookModal && webhookUrl && (
        <Modal visible transparent animationType="fade" onRequestClose={() => setShowWebhookModal(false)}>
          <View style={styles.runInputOverlay}>
            <View style={[styles.runInputCard, { backgroundColor: isDark ? '#1e293b' : '#fff', width: 520 }]}>
              <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                <Text style={[styles.runInputTitle, { color: textColor }]}>Webhook URL</Text>
                <TouchableOpacity
                  onPress={() => setShowWebhookModal(false)}
                  accessibilityRole="button"
                  accessibilityLabel="Close webhook dialog"
                >
                  <Ionicons name="close" size={22} color={textColor} />
                </TouchableOpacity>
              </View>
              <Text style={{ color: isDark ? '#94a3b8' : '#64748b', fontSize: 13, marginBottom: 16 }}>
                Share this URL with external systems to trigger this workflow via HTTP POST. No authentication header needed — the token in the URL is the secret.
              </Text>

              <View style={{
                flexDirection: 'row', alignItems: 'center', gap: 8,
                backgroundColor: isDark ? '#0f172a' : '#f1f5f9',
                borderRadius: 10, padding: 12,
                borderWidth: 1, borderColor: isDark ? '#334155' : '#e2e8f0',
              }}>
                <Text
                  selectable
                  style={{ flex: 1, color: isDark ? '#38bdf8' : '#0369a1', fontSize: 12, fontFamily: 'monospace', lineHeight: 18 }}
                >
                  {webhookUrl}
                </Text>
                <TouchableOpacity
                  onPress={() => {
                    if (Platform.OS === 'web' && navigator.clipboard) {
                      navigator.clipboard.writeText(webhookUrl);
                    }
                  }}
                  style={{ padding: 6, borderRadius: 6, backgroundColor: isDark ? '#1e293b' : '#e2e8f0' }}
                  accessibilityRole="button"
                  accessibilityLabel="Copy webhook URL"
                >
                  <Ionicons name="copy-outline" size={18} color={isDark ? '#94a3b8' : '#475569'} />
                </TouchableOpacity>
              </View>

              <View style={{ marginTop: 16, padding: 12, borderRadius: 8, backgroundColor: isDark ? '#0f172a' : '#fef3c7', borderWidth: 1, borderColor: isDark ? '#334155' : '#fbbf24' }}>
                <Text style={{ color: isDark ? '#fbbf24' : '#92400e', fontSize: 12, fontWeight: '600', marginBottom: 4 }}>Example cURL</Text>
                <Text selectable style={{ color: isDark ? '#94a3b8' : '#374151', fontSize: 11, fontFamily: 'monospace', lineHeight: 18 }}>
                  {`curl -X POST "${webhookUrl}" \\
  -H "Content-Type: application/json" \\
  -d '{"key": "value"}'`}
                </Text>
              </View>

              <Text style={{ color: isDark ? '#64748b' : '#94a3b8', fontSize: 11, marginTop: 12, textAlign: 'center' }}>
                The token is preserved across redeploys and undeploy/redeploy, so external callers keep working.
                Rotate it deliberately from connection settings if it leaks.
              </Text>
            </View>
          </View>
        </Modal>
      )}

      {/* ── Save as Template Modal ─────────────────────────── */}
      {showSaveTemplateModal && (
        <Modal visible transparent animationType="fade" onRequestClose={() => setShowSaveTemplateModal(false)}>
          <View style={styles.runInputOverlay}>
            <View style={[styles.runInputCard, { backgroundColor: isDark ? '#1e293b' : '#fff', width: 420 }]}>
              <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
                <Text style={[styles.runInputTitle, { color: textColor }]}>Save as Template</Text>
                <TouchableOpacity
                  onPress={() => setShowSaveTemplateModal(false)}
                  accessibilityRole="button"
                  accessibilityLabel="Close save template dialog"
                >
                  <Ionicons name="close" size={22} color={textColor} />
                </TouchableOpacity>
              </View>
              <Text style={{ color: isDark ? '#94a3b8' : '#64748b', fontSize: 13, marginBottom: 16 }}>
                Save this workflow as a reusable template in your personal library.
              </Text>
              <TextInput
                value={saveTemplateName}
                onChangeText={setSaveTemplateName}
                style={{
                  borderWidth: 1, borderColor: isDark ? '#334155' : '#e2e8f0', borderRadius: 8,
                  padding: 12, fontSize: 14, color: textColor,
                  backgroundColor: isDark ? '#0f172a' : '#f8fafc', marginBottom: 16,
                }}
                placeholder="Template name"
                placeholderTextColor="#94a3b8"
              />
              <TouchableOpacity
                style={{
                  backgroundColor: '#8b5cf6', paddingVertical: 10, borderRadius: 8,
                  alignItems: 'center', opacity: (!saveTemplateName.trim() || savingTemplate) ? 0.5 : 1,
                }}
                disabled={!saveTemplateName.trim() || savingTemplate}
                accessibilityRole="button"
                accessibilityLabel="Save as template"
                onPress={async () => {
                  setSavingTemplate(true);
                  try {
                    const rfNodes = nodes.map((n) => ({
                      id: n.id,
                      type: n.data?.nodeType || n.type,
                      label: n.data?.label || n.data?.nodeType || '',
                      position: n.position,
                      config: n.data?.config || {},
                    }));
                    const rfEdges = edges.map((e) => ({
                      id: e.id,
                      source: e.source,
                      target: e.target,
                      source_handle: e.sourceHandle || null,
                      target_handle: e.targetHandle || null,
                      label: e.label || null,
                    }));
                    await WorkflowService.saveAsTemplate({
                      name: saveTemplateName.trim(),
                      description: workflowDesc || '',
                      icon: '📋',
                      tags: ['custom'],
                      nodes: rfNodes,
                      edges: rfEdges,
                      variables: {},
                    });
                    setShowSaveTemplateModal(false);
                    setSaveTemplateName('');
                    showNotice('Template saved!', 'success');
                  } catch (err) {
                    showNotice('Failed to save template: ' + err.message, 'error');
                  } finally {
                    setSavingTemplate(false);
                  }
                }}
              >
                {savingTemplate ? (
                  <ActivityIndicator size="small" color="#fff" />
                ) : (
                  <Text style={{ color: '#fff', fontWeight: '600', fontSize: 14 }}>Save Template</Text>
                )}
              </TouchableOpacity>
            </View>
          </View>
        </Modal>
      )}

      {/* ── Workflow Settings (failure alerts) ─────────────── */}
      <WorkflowSettingsModal
        visible={showSettings}
        theme={theme}
        notifications={notifications}
        onClose={() => setShowSettings(false)}
        onSave={(n) => { setNotifications(n); setDirty(true); }}
      />

      {/* ── Execution Monitor ──────────────────────────────── */}
      {showExecution && currentExecution && (
        <Modal visible transparent animationType="slide" onRequestClose={() => setShowExecution(false)}>
          <ExecutionMonitor
            executionId={currentExecution}
            theme={theme}
            onClose={() => setShowExecution(false)}
          />
        </Modal>
      )}

      {/* ── Deploy confirmation (safe-deploy gate) ─────────── */}
      {showDeployConfirm && (
        <Modal visible transparent animationType="fade" onRequestClose={() => setShowDeployConfirm(false)}>
          <View style={styles.runInputOverlay}>
            <View style={[styles.runInputCard, { backgroundColor: isDark ? '#1e293b' : '#fff', width: 480 }]}>
              <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                  <Ionicons name="rocket-outline" size={20} color="#22c55e" />
                  <Text style={[styles.runInputTitle, { color: textColor }]}>Deploy to Production</Text>
                </View>
                <TouchableOpacity onPress={() => setShowDeployConfirm(false)} accessibilityRole="button" accessibilityLabel="Cancel deploy">
                  <Ionicons name="close" size={22} color={textColor} />
                </TouchableOpacity>
              </View>
              <Text style={{ color: isDark ? '#94a3b8' : '#64748b', fontSize: 13, marginBottom: 14 }}>
                This goes <Text style={{ fontWeight: '700' }}>live against production</Text>. The workflow will run
                unattended on its schedule and any webhook, using PROD connections. A version snapshot is recorded so you can roll back.
              </Text>

              {writeNodeLabels.length > 0 && (
                <View style={{ marginBottom: 14, padding: 12, borderRadius: 8, backgroundColor: isDark ? '#3b1d1d' : '#fef2f2', borderWidth: 1, borderColor: isDark ? '#7f1d1d' : '#fecaca' }}>
                  <Text style={{ color: isDark ? '#fca5a5' : '#b91c1c', fontSize: 12, fontWeight: '700', marginBottom: 4 }}>
                    ⚠ {writeNodeLabels.length} node{writeNodeLabels.length === 1 ? '' : 's'} will perform live writes/sends:
                  </Text>
                  <Text style={{ color: isDark ? '#fca5a5' : '#b91c1c', fontSize: 12, lineHeight: 18 }}>
                    {writeNodeLabels.join(', ')}
                  </Text>
                </View>
              )}

              <Text style={{ color: textColor, fontSize: 12, fontWeight: '600', marginBottom: 6 }}>Deployment note (optional)</Text>
              <TextInput
                value={deployNote}
                onChangeText={setDeployNote}
                placeholder="e.g. ticket OPS-431: switch to nightly cadence"
                placeholderTextColor="#94a3b8"
                maxLength={500}
                style={{
                  borderWidth: 1, borderColor: isDark ? '#334155' : '#e2e8f0', borderRadius: 8,
                  padding: 10, fontSize: 13, color: textColor,
                  backgroundColor: isDark ? '#0f172a' : '#f8fafc', marginBottom: 16,
                }}
              />

              <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 10 }}>
                <TouchableOpacity onPress={() => setShowDeployConfirm(false)} style={{ paddingHorizontal: 16, paddingVertical: 10 }} disabled={deployingAction}>
                  <Text style={{ color: isDark ? '#94a3b8' : '#64748b', fontWeight: '600', fontSize: 14 }}>Cancel</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  onPress={confirmDeploy}
                  disabled={deployingAction}
                  style={{ flexDirection: 'row', alignItems: 'center', gap: 6, backgroundColor: '#16a34a', paddingHorizontal: 18, paddingVertical: 10, borderRadius: 8, opacity: deployingAction ? 0.6 : 1 }}
                  accessibilityRole="button"
                  accessibilityLabel="Confirm deploy to production"
                >
                  {deployingAction ? (
                    <ActivityIndicator size="small" color="#fff" />
                  ) : (
                    <>
                      <Ionicons name="rocket" size={16} color="#fff" />
                      <Text style={{ color: '#fff', fontWeight: '700', fontSize: 14 }}>Deploy to Production</Text>
                    </>
                  )}
                </TouchableOpacity>
              </View>
            </View>
          </View>
        </Modal>
      )}

      {/* ── Undeploy confirmation ──────────────────────────── */}
      {showUndeployConfirm && (
        <Modal visible transparent animationType="fade" onRequestClose={() => setShowUndeployConfirm(false)}>
          <View style={styles.runInputOverlay}>
            <View style={[styles.runInputCard, { backgroundColor: isDark ? '#1e293b' : '#fff', width: 440 }]}>
              <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                  <Ionicons name="stop-circle-outline" size={20} color="#ef4444" />
                  <Text style={[styles.runInputTitle, { color: textColor }]}>Undeploy Workflow</Text>
                </View>
                <TouchableOpacity onPress={() => setShowUndeployConfirm(false)} accessibilityRole="button" accessibilityLabel="Cancel undeploy">
                  <Ionicons name="close" size={22} color={textColor} />
                </TouchableOpacity>
              </View>
              <Text style={{ color: isDark ? '#94a3b8' : '#64748b', fontSize: 13, marginBottom: 16 }}>
                This <Text style={{ fontWeight: '700' }}>stops all live automation</Text> — the cron schedule is removed
                and the workflow no longer fires. The webhook URL is preserved, so a later redeploy keeps the same token.
                Your version history is kept.
              </Text>
              <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 10 }}>
                <TouchableOpacity onPress={() => setShowUndeployConfirm(false)} style={{ paddingHorizontal: 16, paddingVertical: 10 }} disabled={deployingAction}>
                  <Text style={{ color: isDark ? '#94a3b8' : '#64748b', fontWeight: '600', fontSize: 14 }}>Cancel</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  onPress={confirmUndeploy}
                  disabled={deployingAction}
                  style={{ flexDirection: 'row', alignItems: 'center', gap: 6, backgroundColor: '#dc2626', paddingHorizontal: 18, paddingVertical: 10, borderRadius: 8, opacity: deployingAction ? 0.6 : 1 }}
                  accessibilityRole="button"
                  accessibilityLabel="Confirm undeploy"
                >
                  {deployingAction ? (
                    <ActivityIndicator size="small" color="#fff" />
                  ) : (
                    <>
                      <Ionicons name="stop-circle" size={16} color="#fff" />
                      <Text style={{ color: '#fff', fontWeight: '700', fontSize: 14 }}>Undeploy</Text>
                    </>
                  )}
                </TouchableOpacity>
              </View>
            </View>
          </View>
        </Modal>
      )}

      {/* ── Deploy history + rollback ──────────────────────── */}
      <WorkflowVersionsModal
        visible={showVersions}
        theme={theme}
        workflowId={currentWorkflowId.current}
        onClose={() => setShowVersions(false)}
        onRolledBack={handleRolledBack}
      />
    </View>
  );
}

// Helper to match schema by node type
function _findSchema(schemas, nodeType) {
  return (schemas || []).find((s) => s.type === nodeType);
}

// React Flow DROPS any edge whose handle id doesn't match a handle actually
// rendered on the node. A node's OUTPUT handle ids are 'true'/'false' for ≤2
// outputs and 'out-N' for switches (see getWorkflowNodeOutputSpecs); INPUT
// handles are 'in-N'. AI-generated edges often omit the handle or guess a
// wrong string, so we resolve each edge's source handle against the SOURCE
// node's real output specs (this is the "disconnected nodes" fix).
function _resolveSourceHandle(outputSpecs, raw) {
  const specs = outputSpecs || [];
  if (specs.length === 0) return undefined;                  // node has no output handle
  if (raw && specs.some((s) => s.handleId === raw)) return raw;  // already a real handle id
  let idx = 0;                                               // default: first output
  if (raw === 'false') idx = 1;
  else if (raw === 'true' || raw === 'output' || raw == null || raw === '') idx = 0;
  else {
    const m = /^(?:out-|output_)(\d+)$/.exec(raw);
    if (m) idx = parseInt(m[1], 10);
  }
  if (idx < 0 || idx >= specs.length) idx = 0;
  return specs[idx].handleId;
}
function _normTargetHandle(h) {
  if (!h || h === 'input' || h === 'true') return 'in-0';
  const m = /^(?:in-|input_)(\d+)$/.exec(h);
  return m ? `in-${m[1]}` : h;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Exported wrapper with ReactFlowProvider
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
export default function WorkflowCanvas(props) {
  if (Platform.OS !== 'web' || !ReactFlowProvider) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center' }}>
        <Text>Workflow Builder is only available on web.</Text>
      </View>
    );
  }

  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderBottomWidth: 1,
    gap: 8,
  },
  backBtn: { padding: 6 },
  titleInput: {
    flex: 1,
    fontSize: 16,
    fontWeight: '600',
    paddingVertical: 4,
    paddingHorizontal: 8,
    outline: 'none',
  },
  toolbarActions: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  toolBtn: { padding: 6, borderRadius: 6 },
  runsBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 7,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#94a3b8',
  },
  runsBtnText: { fontWeight: '600', fontSize: 13 },
  saveBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#3b82f6',
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 8,
  },
  saveBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  runBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    backgroundColor: '#22c55e',
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 8,
  },
  runBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  deployBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 8,
  },
  deployBtnDraft: { backgroundColor: '#6366f1' },
  deployBtnLive: { backgroundColor: '#ef4444' },
  deployBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  body: { flex: 1, flexDirection: 'row' },
  canvasWrap: { flex: 1, position: 'relative' },
  flowControls: {
    position: 'absolute',
    left: 16,
    bottom: 16,
    gap: 10,
    alignItems: 'stretch',
  },
  flowControlButton: {
    width: 42,
    height: 42,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
    boxShadow: '0 10px 24px rgba(15, 23, 42, 0.16)',
  },
  flowControlToggle: {
    width: 'auto',
    minWidth: 120,
    paddingHorizontal: 12,
    flexDirection: 'row',
    gap: 8,
  },
  flowControlToggleText: {
    color: '#ffffff',
    fontWeight: '700',
    fontSize: 12,
  },
  aiBadge: {
    position: 'absolute',
    top: -4,
    right: -4,
    backgroundColor: '#8b5cf6',
    borderRadius: 8,
    minWidth: 16,
    height: 16,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 3,
  },
  aiBadgeText: {
    color: '#fff',
    fontSize: 9,
    fontWeight: '700',
  },
  // Run Input Form
  runInputOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.5)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  runInputCard: {
    width: 440,
    maxHeight: '80%',
    borderRadius: 16,
    padding: 24,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.15,
    shadowRadius: 24,
    elevation: 10,
  },
  runInputTitle: {
    fontSize: 18,
    fontWeight: '700',
    marginBottom: 4,
  },
  runInputField: {
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 8,
    fontSize: 14,
    outline: 'none',
  },
  runInputCancel: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    borderWidth: 1,
  },
  runInputRunBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: 20,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: '#22c55e',
  },
});
