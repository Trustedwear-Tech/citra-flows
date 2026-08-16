/**
 * AppShell — the frame around the builder once you are signed in.
 *
 * Thin on purpose. WorkflowBuilderScreen already owns its own navigation
 * (list, canvas, templates, approvals, connections, runs), so the shell adds
 * only what it cannot: who you are signed in as, and a way out.
 */

import React, { useState } from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Platform } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import WorkflowBuilderScreen from './WorkflowBuilder/WorkflowBuilderScreen';
import authService from '../services/authService';
import theme from './theme';

export default function AppShell({ user, onSignedOut }) {
  const [menuOpen, setMenuOpen] = useState(false);

  const signOut = async () => {
    setMenuOpen(false);
    await authService.signOut();
    onSignedOut?.();
  };

  const label = user?.name || user?.email || 'Signed in';
  const initial = (user?.name || user?.email || '?').trim().charAt(0).toUpperCase();

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <View style={styles.brandRow}>
          <View style={styles.brandMark}>
            <Ionicons name="git-network" size={16} color={theme.primaryText} />
          </View>
          <Text style={styles.brandName}>Citra Flows</Text>
        </View>

        <View>
          <TouchableOpacity
            style={styles.userChip}
            onPress={() => setMenuOpen((v) => !v)}
            accessibilityRole="button"
            accessibilityLabel="Account menu"
          >
            <View style={styles.avatar}>
              <Text style={styles.avatarText}>{initial}</Text>
            </View>
            <Text style={styles.userText} numberOfLines={1}>{label}</Text>
            <Ionicons
              name={menuOpen ? 'chevron-up' : 'chevron-down'}
              size={14}
              color={theme.textSecondary}
            />
          </TouchableOpacity>

          {menuOpen ? (
            <View style={styles.menu}>
              {user?.email ? (
                <View style={styles.menuHeader}>
                  <Text style={styles.menuEmail} numberOfLines={1}>{user.email}</Text>
                  {user?.org_id ? (
                    <Text style={styles.menuOrg} numberOfLines={1}>{user.org_id}</Text>
                  ) : null}
                </View>
              ) : null}
              <TouchableOpacity style={styles.menuItem} onPress={signOut} accessibilityRole="button">
                <Ionicons name="log-out-outline" size={16} color={theme.danger} />
                <Text style={styles.menuItemText}>Sign out</Text>
              </TouchableOpacity>
            </View>
          ) : null}
        </View>
      </View>

      {/* Tapping anywhere else closes the menu. Rendered behind the menu, above
          the builder, and only while open so it never eats canvas clicks. */}
      {menuOpen ? (
        <TouchableOpacity
          style={styles.scrim}
          activeOpacity={1}
          onPress={() => setMenuOpen(false)}
        />
      ) : null}

      <View style={styles.body}>
        {/* onClose is required by the screen but there is nowhere to go back to
            here — the builder IS the product. Made a no-op deliberately. */}
        <WorkflowBuilderScreen theme={theme} onClose={() => {}} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.background },

  header: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    paddingHorizontal: 16, paddingVertical: 10,
    backgroundColor: theme.surface,
    borderBottomColor: theme.border, borderBottomWidth: 1,
    zIndex: 20,
  },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  brandMark: {
    width: 26, height: 26, borderRadius: 7, backgroundColor: theme.primary,
    alignItems: 'center', justifyContent: 'center',
  },
  brandName: { color: theme.text, fontSize: 15, fontWeight: '700' },

  userChip: {
    flexDirection: 'row', alignItems: 'center', gap: 8,
    paddingVertical: 5, paddingHorizontal: 9,
    borderRadius: 8, borderColor: theme.border, borderWidth: 1,
    backgroundColor: theme.surfaceAlt, maxWidth: 260,
  },
  avatar: {
    width: 22, height: 22, borderRadius: 11, backgroundColor: theme.primary,
    alignItems: 'center', justifyContent: 'center',
  },
  avatarText: { color: theme.primaryText, fontSize: 11, fontWeight: '700' },
  userText: { color: theme.text, fontSize: 13, flexShrink: 1 },

  menu: {
    position: 'absolute', top: 40, right: 0, minWidth: 220,
    backgroundColor: theme.surfaceAlt, borderColor: theme.border, borderWidth: 1,
    borderRadius: 10, paddingVertical: 6, zIndex: 30,
    ...(Platform.OS === 'web' ? { boxShadow: '0 8px 24px rgba(0,0,0,0.4)' } : { elevation: 8 }),
  },
  menuHeader: {
    paddingHorizontal: 12, paddingVertical: 8,
    borderBottomColor: theme.border, borderBottomWidth: 1, marginBottom: 4,
  },
  menuEmail: { color: theme.text, fontSize: 13, fontWeight: '600' },
  menuOrg: { color: theme.textMuted, fontSize: 11, marginTop: 2 },
  menuItem: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 12, paddingVertical: 9 },
  menuItemText: { color: theme.danger, fontSize: 13, fontWeight: '600' },

  scrim: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 10 },

  body: { flex: 1 },
});
