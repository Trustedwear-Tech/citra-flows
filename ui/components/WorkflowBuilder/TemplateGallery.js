/**
 * TemplateGallery.js — Grid of pre-built workflow templates to quick-start new workflows.
 * A read-only showcase of example workflows so users can see what can be built,
 * plus a personal "My Templates" section. AI-assisted building lives in the
 * AI Chat panel on the canvas — not here.
 */
import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, ScrollView, StyleSheet, ActivityIndicator } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowService from '../../services/WorkflowService';

const TAG_COLORS = {
  agent: '#a855f7',
  'ai-tools': '#8b5cf6',
  'ai-generated': '#7c3aed',
  research: '#3b82f6',
  data: '#06b6d4',
  pipeline: '#14b8a6',
  approval: '#f59e0b',
  review: '#f97316',
  'multi-agent': '#ec4899',
  document: '#6366f1',
  custom: '#10b981',
  default: '#64748b',
};

export default function TemplateGallery({ theme, onSelectTemplate, onBack }) {
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const isDark = theme?.isDark;
  const bg = isDark ? '#0f172a' : '#f8fafc';
  const cardBg = isDark ? '#1e293b' : '#ffffff';
  const text = isDark ? '#e2e8f0' : '#1e293b';
  const muted = isDark ? '#94a3b8' : '#64748b';
  const border = isDark ? '#334155' : '#e2e8f0';

  const loadTemplates = useCallback(() => {
    setLoading(true);
    WorkflowService.listTemplates()
      .then((res) => setTemplates(res.templates || []))
      .catch((err) => console.error('Failed to load templates:', err))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadTemplates(); }, [loadTemplates]);

  const handleUseTemplate = async (templateId) => {
    setCreating(templateId);
    try {
      const result = await WorkflowService.createFromTemplate(templateId);
      onSelectTemplate(result.workflow_id);
    } catch (err) {
      alert('Failed to create from template: ' + err.message);
    } finally {
      setCreating(null);
    }
  };

  const handleDeleteTemplate = async (templateId) => {
    setDeleting(templateId);
    try {
      await WorkflowService.deleteUserTemplate(templateId);
      setTemplates((prev) => prev.filter((t) => t.template_id !== templateId));
    } catch (err) {
      alert('Failed to delete template: ' + err.message);
    } finally {
      setDeleting(null);
    }
  };

  if (loading) {
    return (
      <View style={[styles.center, { backgroundColor: bg }]}>
        <ActivityIndicator size="large" color="#3b82f6" />
      </View>
    );
  }

  const systemTemplates = templates.filter((t) => t.is_system !== false);
  const userTemplates = templates.filter((t) => t.is_system === false);

  const renderCard = (tpl, isUser = false) => (
    <View key={tpl.template_id} style={[styles.card, { backgroundColor: cardBg, borderColor: border }]}>
      {isUser && (
        <TouchableOpacity
          style={styles.deleteBtn}
          onPress={() => handleDeleteTemplate(tpl.template_id)}
          disabled={deleting === tpl.template_id}
        >
          {deleting === tpl.template_id ? (
            <ActivityIndicator size={12} color="#ef4444" />
          ) : (
            <Ionicons name="trash-outline" size={14} color="#ef4444" />
          )}
        </TouchableOpacity>
      )}
      <Text style={styles.cardIcon}>{tpl.icon || '📋'}</Text>
      <Text style={[styles.cardName, { color: text }]}>{tpl.name}</Text>
      <Text style={[styles.cardDesc, { color: muted }]} numberOfLines={2}>
        {tpl.description}
      </Text>

      <View style={styles.tagRow}>
        {(tpl.tags || []).slice(0, 3).map((tag) => (
          <View
            key={tag}
            style={[styles.tag, { backgroundColor: (TAG_COLORS[tag] || TAG_COLORS.default) + '20' }]}
          >
            <Text style={[styles.tagText, { color: TAG_COLORS[tag] || TAG_COLORS.default }]}>
              {tag}
            </Text>
          </View>
        ))}
      </View>

      <View style={styles.cardMeta}>
        <Text style={{ fontSize: 11, color: muted }}>
          {tpl.node_count || 0} nodes
        </Text>
      </View>

      <TouchableOpacity
        style={[styles.useBtn, creating === tpl.template_id && { opacity: 0.5 }]}
        onPress={() => handleUseTemplate(tpl.template_id)}
        disabled={creating === tpl.template_id}
      >
        {creating === tpl.template_id ? (
          <ActivityIndicator size="small" color="#fff" />
        ) : (
          <>
            <Ionicons name="rocket-outline" size={14} color="#fff" />
            <Text style={styles.useBtnText}>Use Template</Text>
          </>
        )}
      </TouchableOpacity>
    </View>
  );

  return (
    <ScrollView
      style={[styles.container, { backgroundColor: bg }]}
      contentContainerStyle={styles.grid}
      showsVerticalScrollIndicator={false}
    >
      {onBack && (
        <TouchableOpacity onPress={onBack} style={styles.backBtn}>
          <Ionicons name="arrow-back" size={18} color={muted} />
          <Text style={{ color: muted, fontSize: 13 }}>Back to Workflows</Text>
        </TouchableOpacity>
      )}
      <Text style={[styles.heading, { color: text }]}>Start from a Template</Text>
      <Text style={[styles.subheading, { color: muted }]}>
        Browse pre-built example workflows to see what you can build. To create one
        with AI, open a new workflow and use the AI Chat panel.
      </Text>

      {/* My Templates section */}
      {userTemplates.length > 0 && (
        <>
          <Text style={[styles.sectionTitle, { color: text }]}>My Templates</Text>
          <View style={styles.cardGrid}>
            {userTemplates.map((tpl) => renderCard(tpl, true))}
          </View>
        </>
      )}

      {/* System Templates section */}
      {systemTemplates.length > 0 && (
        <>
          <Text style={[styles.sectionTitle, { color: text }]}>
            {userTemplates.length > 0 ? 'Pre-built Templates' : ''}
          </Text>
          <View style={styles.cardGrid}>
            {systemTemplates.map((tpl) => renderCard(tpl, false))}
          </View>
        </>
      )}

      {templates.length === 0 && (
        <Text style={[styles.emptyText, { color: muted }]}>No templates available yet.</Text>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  grid: { padding: 24 },
  heading: { fontSize: 22, fontWeight: '800', marginBottom: 4 },
  backBtn: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 12 },
  subheading: { fontSize: 14, marginBottom: 20 },
  sectionTitle: { fontSize: 16, fontWeight: '700', marginBottom: 12, marginTop: 8 },
  cardGrid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 16,
    marginBottom: 24,
  },
  card: {
    width: 280,
    borderWidth: 1,
    borderRadius: 14,
    padding: 18,
    position: 'relative',
  },
  deleteBtn: {
    position: 'absolute', top: 10, right: 10, zIndex: 1,
    padding: 4,
  },
  cardIcon: { fontSize: 28, marginBottom: 10 },
  cardName: { fontSize: 15, fontWeight: '700', marginBottom: 4 },
  cardDesc: { fontSize: 12, lineHeight: 17, marginBottom: 10 },
  tagRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginBottom: 10 },
  tag: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 10 },
  tagText: { fontSize: 10, fontWeight: '600' },
  cardMeta: { marginBottom: 12 },
  useBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    backgroundColor: '#6366f1',
    paddingVertical: 8,
    borderRadius: 8,
  },
  useBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },
  emptyText: { fontSize: 14, fontStyle: 'italic', textAlign: 'center', marginTop: 40 },
});
