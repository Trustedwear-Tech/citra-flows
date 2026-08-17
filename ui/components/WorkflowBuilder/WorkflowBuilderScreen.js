// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * WorkflowBuilderScreen.js — Main container that orchestrates
 * list / canvas / templates / approvals / connections views.
 *
 * The Templates gallery is a read-only showcase of pre-built example
 * workflows so users can see what's possible. AI-assisted building
 * lives only in the AI Chat panel on the canvas — the gallery has no
 * "create with AI" path of its own.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { View, StyleSheet, Platform } from 'react-native';
import WorkflowListScreen from './WorkflowListScreen';
import WorkflowCanvas from './WorkflowCanvas';
import TemplateGallery from './TemplateGallery';
import ApprovalQueue from './ApprovalQueue';
import ConnectionManager from './ConnectionManager';
import MaintenancePanel from './MaintenancePanel';
import AutomationControlPanel from './AutomationControlPanel';
import WorkflowRunHistory from './WorkflowRunHistory';
import WorkflowRunDetail from './WorkflowRunDetail';
import HowToUseModal from '../HowToUseModal';

export default function WorkflowBuilderScreen({ theme, onClose, initialWorkflowId = null, deepLink = null }) {
  // initialWorkflowId: if set, jump straight into the canvas with that
  // workflow loaded. Used by the admin "Open" affordance on
  // AdminManagedResourcesScreen so IT can land directly on a specific
  // workflow (incl. smart_app_action workflows that are hidden from the
  // default list).
  const [activeWorkflow, setActiveWorkflow] = useState(initialWorkflowId); // null = list, string = canvas
  const [view, setView] = useState(initialWorkflowId ? 'canvas' : 'list'); // 'list' | 'canvas' | 'templates' | 'approvals' | 'connections' | 'maintenance' | 'runs' | 'run-detail'
  const [deepLinkExecutionId, setDeepLinkExecutionId] = useState(null);
  const [runsWorkflowId, setRunsWorkflowId] = useState(null);     // workflow whose runs are shown
  const [detailExecutionId, setDetailExecutionId] = useState(null); // execution shown in run-detail
  const [showGuide, setShowGuide] = useState(false);

  // Handle deep link params:
  //   ?view=approval&execution=xxx
  //   ?view=runs&workflow=xxx
  //   ?view=run-detail&workflow=xxx&execution=yyy
  useEffect(() => {
    if (Platform.OS !== 'web') return;
    try {
      const params = new URLSearchParams(window.location.search);
      const v = params.get('view');
      const execId = params.get('execution');
      const wfId = params.get('workflow');
      let consumed = false;
      if (v === 'approval') {
        if (execId) setDeepLinkExecutionId(execId);
        setView('approvals');
        consumed = true;
      } else if (v === 'runs' && wfId) {
        setRunsWorkflowId(wfId);
        setView('runs');
        consumed = true;
      } else if (v === 'run-detail' && execId) {
        if (wfId) setRunsWorkflowId(wfId);
        setDetailExecutionId(execId);
        setView('run-detail');
        consumed = true;
      }
      if (consumed) {
        // Clean up URL params without reload
        const url = new URL(window.location.href);
        url.searchParams.delete('view');
        url.searchParams.delete('execution');
        url.searchParams.delete('workflow');
        window.history.replaceState({}, '', url.toString());
      }
    } catch { /* ignore */ }
  }, []);

  // Apply a deep link handed down from the app root (App.js captures the
  // ?view=runs / ?view=run-detail params before they're cleaned and opens
  // this module). This is the reliable cold-load path; the URL effect above
  // remains for the in-module ?view=approval case and same-session links.
  useEffect(() => {
    if (!deepLink) return;
    if (deepLink.view === 'runs' && deepLink.workflowId) {
      setRunsWorkflowId(deepLink.workflowId);
      setView('runs');
    } else if (deepLink.view === 'run-detail' && deepLink.executionId) {
      if (deepLink.workflowId) setRunsWorkflowId(deepLink.workflowId);
      setDetailExecutionId(deepLink.executionId);
      setView('run-detail');
    }
  }, [deepLink]);

  const handleOpenWorkflow = useCallback((workflowId) => {
    setActiveWorkflow(workflowId);
    setView('canvas');
  }, []);

  // When the canvas creates a brand-new workflow, promote its id to the
  // active workflow so the builder switches from create-mode to edit-mode.
  // Without this the new id lived only inside the canvas ref, so the screen
  // looked unchanged after save and users assumed it had failed.
  const handleSaveSuccess = useCallback((newWorkflowId) => {
    if (newWorkflowId) setActiveWorkflow(newWorkflowId);
  }, []);

  const handleCreateNew = useCallback(() => {
    setActiveWorkflow(null);
    setView('canvas');
  }, []);

  const handleBackToList = useCallback(() => {
    setActiveWorkflow(null);
    setDeepLinkExecutionId(null);
    setRunsWorkflowId(null);
    setDetailExecutionId(null);
    setView('list');
  }, []);

  // Open the per-workflow runs history. Triggered from the canvas header
  // ("Runs") and from each workflow card's "History" action on the list.
  const handleShowRuns = useCallback((workflowId) => {
    if (!workflowId) return;
    setRunsWorkflowId(workflowId);
    setView('runs');
  }, []);

  const handleOpenExecution = useCallback((executionId) => {
    if (!executionId) return;
    setDetailExecutionId(executionId);
    setView('run-detail');
  }, []);

  // Back from the runs list: return to whichever surface launched it. If a
  // workflow is open in the canvas, go back there; otherwise the list.
  const handleBackFromRuns = useCallback(() => {
    setRunsWorkflowId(null);
    setView(activeWorkflow ? 'canvas' : 'list');
  }, [activeWorkflow]);

  // Back from detail returns to the runs list it was opened from.
  const handleBackFromDetail = useCallback(() => {
    setDetailExecutionId(null);
    setView('runs');
  }, []);

  const handleShowTemplates = useCallback(() => {
    setView('templates');
  }, []);

  const handleShowApprovals = useCallback(() => {
    setView('approvals');
  }, []);

  const handleShowConnections = useCallback(() => {
    setView('connections');
  }, []);

  const handleShowMaintenance = useCallback(() => {
    setView('maintenance');
  }, []);

  const handleShowControl = useCallback(() => {
    setView('control');
  }, []);

  const handleShowGuide = useCallback(() => {
    setShowGuide(true);
  }, []);

  return (
    <View style={styles.container}>
      {view === 'list' ? (
        <WorkflowListScreen
          theme={theme}
          onClose={onClose}
          onOpenWorkflow={handleOpenWorkflow}
          onCreateNew={handleCreateNew}
          onShowTemplates={handleShowTemplates}
          onShowApprovals={handleShowApprovals}
          onShowConnections={handleShowConnections}
          onShowMaintenance={handleShowMaintenance}
          onShowControl={handleShowControl}
          onShowRuns={handleShowRuns}
          onShowGuide={handleShowGuide}
        />
      ) : view === 'templates' ? (
        <TemplateGallery
          theme={theme}
          onSelectTemplate={handleOpenWorkflow}
          onBack={handleBackToList}
        />
      ) : view === 'approvals' ? (
        <ApprovalQueue
          theme={theme}
          onBack={handleBackToList}
          deepLinkExecutionId={deepLinkExecutionId}
        />
      ) : view === 'connections' ? (
        <ConnectionManager
          theme={theme}
          onBack={handleBackToList}
        />
      ) : view === 'maintenance' ? (
        <MaintenancePanel
          theme={theme}
          onBack={handleBackToList}
        />
      ) : view === 'control' ? (
        <AutomationControlPanel
          theme={theme}
          onBack={handleBackToList}
        />
      ) : view === 'runs' ? (
        <WorkflowRunHistory
          workflowId={runsWorkflowId}
          theme={theme}
          onBack={handleBackFromRuns}
          onOpenExecution={handleOpenExecution}
        />
      ) : view === 'run-detail' ? (
        <WorkflowRunDetail
          executionId={detailExecutionId}
          workflowId={runsWorkflowId}
          theme={theme}
          onBack={runsWorkflowId ? handleBackFromDetail : handleBackToList}
          onShowApprovals={handleShowApprovals}
        />
      ) : (
        <WorkflowCanvas
          workflowId={activeWorkflow}
          theme={theme}
          onClose={handleBackToList}
          onShowGuide={handleShowGuide}
          onSaveSuccess={handleSaveSuccess}
          onShowRuns={handleShowRuns}
        />
      )}

      <HowToUseModal
        visible={showGuide}
        onClose={() => setShowGuide(false)}
        initialSection="agent-builder-intro"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
});
