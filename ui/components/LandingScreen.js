/**
 * LandingScreen — what an evaluator sees before signing in.
 *
 * Deliberately plain: what the product is, what it needs to run, and one way
 * in. No pricing, no testimonials, no logos, and nothing loaded from a remote
 * host — a developer deciding whether to run this wants the facts, and an
 * air-gapped install must render identically.
 */

import React from 'react';
import {
  View,
  Text,
  ScrollView,
  TouchableOpacity,
  StyleSheet,
  useWindowDimensions,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import theme from './theme';

const CAPABILITIES = [
  {
    icon: 'git-network-outline',
    title: 'Describe it, review it, run it',
    body:
      'Write the pipeline in plain English. The builder assembles it from typed '
      + 'nodes you can inspect — it cannot invent one — then you edit the graph '
      + 'directly on the canvas.',
  },
  {
    icon: 'repeat-outline',
    title: 'Built for runs that fail',
    body:
      'Per-node retries with backoff, a durable Redis Streams queue so a restart '
      + 'resumes rather than restarts, and leader-elected cron so replicas do not '
      + 'double-fire.',
  },
  {
    icon: 'link-outline',
    title: 'Connect to what you already have',
    body:
      'SQL, MongoDB, REST, SFTP/FTP, S3, webhooks, email and Slack/Teams — plus '
      + 'any vector database and any standards-compliant MCP server.',
  },
  {
    icon: 'shield-checkmark-outline',
    title: 'Writes are opt-in',
    body:
      'Agent tools that could mutate something are blocked unless you enable '
      + 'them on that node. Missing safety metadata is treated as "no", never as '
      + 'permission.',
  },
];

const REQUIREMENTS = [
  'MongoDB — workflow definitions and run history',
  'Redis — durable job queue and scheduler leases',
  'An OpenAI-compatible model endpoint — point it at your own vLLM or Ollama',
  'Optional: S3/MinIO for file outputs, Docker for the code sandbox',
];

export default function LandingScreen({ onSignIn }) {
  const { width } = useWindowDimensions();
  const wide = width >= 900;

  return (
    <ScrollView
      style={styles.root}
      contentContainerStyle={[styles.content, { paddingHorizontal: wide ? 64 : 24 }]}
    >
      {/* ── Masthead ─────────────────────────────────────────────── */}
      <View style={styles.brandRow}>
        <View style={styles.brandMark}>
          <Ionicons name="git-network" size={22} color={theme.primaryText} />
        </View>
        <Text style={styles.brandName}>Citra Flows</Text>
      </View>

      <Text style={[styles.headline, { fontSize: wide ? 40 : 30 }]}>
        AI-authored workflow automation{'\n'}you run on your own infrastructure.
      </Text>

      <Text style={styles.subhead}>
        Describe a pipeline in plain English; the builder assembles it from typed
        nodes. Batch or event-driven, human checkpoints where you choose, audited
        on every step. Point it at your own models and nothing leaves your network.
      </Text>

      <TouchableOpacity style={styles.cta} onPress={onSignIn} accessibilityRole="button">
        <Text style={styles.ctaText}>Sign in</Text>
        <Ionicons name="arrow-forward" size={18} color={theme.primaryText} />
      </TouchableOpacity>

      {/* ── Capabilities ─────────────────────────────────────────── */}
      <View style={[styles.grid, wide && styles.gridWide]}>
        {CAPABILITIES.map((c) => (
          <View key={c.title} style={[styles.card, wide && styles.cardWide]}>
            <Ionicons name={c.icon} size={22} color={theme.primary} />
            <Text style={styles.cardTitle}>{c.title}</Text>
            <Text style={styles.cardBody}>{c.body}</Text>
          </View>
        ))}
      </View>

      {/* ── What it needs to run ─────────────────────────────────── */}
      <View style={styles.panel}>
        <Text style={styles.panelTitle}>What it needs to run</Text>
        {REQUIREMENTS.map((r) => (
          <View key={r} style={styles.reqRow}>
            <Ionicons name="ellipse" size={6} color={theme.textMuted} style={styles.bullet} />
            <Text style={styles.reqText}>{r}</Text>
          </View>
        ))}
        <Text style={styles.panelNote}>
          Sign-in is required: workflows, runs and saved connections all belong to
          a user, and every API call is authorised on that identity.
        </Text>
      </View>

      <Text style={styles.footer}>
        BUSL-1.1 · See ARCHITECTURE.md for the execution model and PORTING.md for
        known gaps.
      </Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.background },
  content: { paddingVertical: 56, maxWidth: 1100, width: '100%', alignSelf: 'center' },

  brandRow: { flexDirection: 'row', alignItems: 'center', gap: 10, marginBottom: 36 },
  brandMark: {
    width: 36, height: 36, borderRadius: 9,
    backgroundColor: theme.primary, alignItems: 'center', justifyContent: 'center',
  },
  brandName: { color: theme.text, fontSize: 18, fontWeight: '700', letterSpacing: 0.2 },

  headline: { color: theme.text, fontWeight: '800', lineHeight: 48, marginBottom: 18 },
  subhead: {
    color: theme.textSecondary, fontSize: 16, lineHeight: 25, maxWidth: 660, marginBottom: 30,
  },

  cta: {
    flexDirection: 'row', alignItems: 'center', gap: 8, alignSelf: 'flex-start',
    backgroundColor: theme.primary, paddingVertical: 13, paddingHorizontal: 26, borderRadius: 10,
  },
  ctaText: { color: theme.primaryText, fontSize: 16, fontWeight: '700' },

  grid: { marginTop: 52, gap: 16 },
  gridWide: { flexDirection: 'row', flexWrap: 'wrap' },
  card: {
    backgroundColor: theme.surface, borderColor: theme.border, borderWidth: 1,
    borderRadius: 12, padding: 20, gap: 9,
  },
  cardWide: { width: '48%' },
  cardTitle: { color: theme.text, fontSize: 16, fontWeight: '700' },
  cardBody: { color: theme.textSecondary, fontSize: 14, lineHeight: 21 },

  panel: {
    marginTop: 40, backgroundColor: theme.surfaceAlt, borderColor: theme.border,
    borderWidth: 1, borderRadius: 12, padding: 22,
  },
  panelTitle: { color: theme.text, fontSize: 16, fontWeight: '700', marginBottom: 14 },
  reqRow: { flexDirection: 'row', alignItems: 'flex-start', gap: 10, marginBottom: 9 },
  bullet: { marginTop: 7 },
  reqText: { color: theme.textSecondary, fontSize: 14, lineHeight: 21, flex: 1 },
  panelNote: {
    color: theme.textMuted, fontSize: 13, lineHeight: 20, marginTop: 12,
    borderTopColor: theme.border, borderTopWidth: 1, paddingTop: 12,
  },

  footer: { color: theme.textMuted, fontSize: 12, marginTop: 44, textAlign: 'center' },
});
