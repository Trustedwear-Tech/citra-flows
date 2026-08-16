/**
 * LoginScreen — email + password against POST /api/auth/login.
 *
 * There is no sign-up link on purpose: this engine does not own identity.
 * Accounts, passwords and roles live in Citra-User-Service, and /api/auth/login
 * proxies to it. The hint below points there rather than leaving someone without
 * an account stuck at a form they cannot get past.
 */

import React, { useState, useRef } from 'react';
import {
  View,
  Text,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import authService from '../services/authService';
import theme from './theme';

export default function LoginScreen({ onSignedIn, onBack }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const passwordRef = useRef(null);

  const canSubmit = email.trim().length > 0 && password.length > 0 && !busy;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError('');
    try {
      const user = await authService.signIn(email.trim(), password);
      onSignedIn?.(user);
    } catch (err) {
      // Show what the server actually said. A generic "login failed" hides the
      // difference between wrong credentials and an unreachable backend, which
      // is the first thing anyone self-hosting needs to tell apart.
      setError(err?.message || 'Sign in failed.');
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.root}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <View style={styles.card}>
        <TouchableOpacity style={styles.back} onPress={onBack} accessibilityRole="button">
          <Ionicons name="arrow-back" size={18} color={theme.textSecondary} />
          <Text style={styles.backText}>Back</Text>
        </TouchableOpacity>

        <View style={styles.brandMark}>
          <Ionicons name="git-network" size={22} color={theme.primaryText} />
        </View>
        <Text style={styles.title}>Sign in to Citra Flows</Text>
        <Text style={styles.subtitle}>
          Your workflows, runs and saved connections are tied to this account.
        </Text>

        {error ? (
          <View style={styles.errorBox} accessibilityLiveRegion="polite">
            <Ionicons name="alert-circle" size={16} color={theme.danger} />
            <Text style={styles.errorText}>{error}</Text>
          </View>
        ) : null}

        <Text style={styles.label}>Email</Text>
        <TextInput
          style={styles.input}
          value={email}
          onChangeText={setEmail}
          placeholder="you@example.com"
          placeholderTextColor={theme.textMuted}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="email-address"
          textContentType="username"
          returnKeyType="next"
          onSubmitEditing={() => passwordRef.current?.focus()}
          editable={!busy}
        />

        <Text style={styles.label}>Password</Text>
        <View style={styles.passwordRow}>
          <TextInput
            ref={passwordRef}
            style={[styles.input, styles.passwordInput]}
            value={password}
            onChangeText={setPassword}
            placeholder="••••••••"
            placeholderTextColor={theme.textMuted}
            secureTextEntry={!showPassword}
            autoCapitalize="none"
            autoCorrect={false}
            textContentType="password"
            returnKeyType="go"
            onSubmitEditing={submit}
            editable={!busy}
          />
          <TouchableOpacity
            style={styles.eye}
            onPress={() => setShowPassword((v) => !v)}
            accessibilityRole="button"
            accessibilityLabel={showPassword ? 'Hide password' : 'Show password'}
          >
            <Ionicons
              name={showPassword ? 'eye-off-outline' : 'eye-outline'}
              size={18}
              color={theme.textSecondary}
            />
          </TouchableOpacity>
        </View>

        <TouchableOpacity
          style={[styles.submit, !canSubmit && styles.submitDisabled]}
          onPress={submit}
          disabled={!canSubmit}
          accessibilityRole="button"
        >
          {busy ? (
            <ActivityIndicator color={theme.primaryText} />
          ) : (
            <Text style={styles.submitText}>Sign in</Text>
          )}
        </TouchableOpacity>

        <Text style={styles.hint}>
          Use your Citra account. Accounts, passwords and roles are managed in
          {' '}
          <Text style={styles.mono}>Citra-User-Service</Text> — ask an admin
          there if you need access.
        </Text>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1, backgroundColor: theme.background,
    alignItems: 'center', justifyContent: 'center', padding: 24,
  },
  card: {
    width: '100%', maxWidth: 420, backgroundColor: theme.surface,
    borderColor: theme.border, borderWidth: 1, borderRadius: 14, padding: 28,
  },

  back: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 18 },
  backText: { color: theme.textSecondary, fontSize: 14 },

  brandMark: {
    width: 40, height: 40, borderRadius: 10, backgroundColor: theme.primary,
    alignItems: 'center', justifyContent: 'center', marginBottom: 16,
  },
  title: { color: theme.text, fontSize: 21, fontWeight: '700' },
  subtitle: { color: theme.textSecondary, fontSize: 14, lineHeight: 20, marginTop: 6, marginBottom: 20 },

  errorBox: {
    flexDirection: 'row', alignItems: 'flex-start', gap: 8,
    backgroundColor: 'rgba(239,68,68,0.10)', borderColor: 'rgba(239,68,68,0.35)',
    borderWidth: 1, borderRadius: 8, padding: 11, marginBottom: 16,
  },
  errorText: { color: theme.danger, fontSize: 13, lineHeight: 19, flex: 1 },

  label: { color: theme.textSecondary, fontSize: 13, fontWeight: '600', marginBottom: 6 },
  input: {
    backgroundColor: theme.inputBackground, borderColor: theme.border, borderWidth: 1,
    borderRadius: 8, paddingVertical: 11, paddingHorizontal: 13,
    color: theme.text, fontSize: 15, marginBottom: 14,
  },
  passwordRow: { position: 'relative', justifyContent: 'center' },
  passwordInput: { paddingRight: 44 },
  eye: { position: 'absolute', right: 12, top: 11 },

  submit: {
    backgroundColor: theme.primary, borderRadius: 9, paddingVertical: 13,
    alignItems: 'center', justifyContent: 'center', marginTop: 4, minHeight: 46,
  },
  submitDisabled: { opacity: 0.5 },
  submitText: { color: theme.primaryText, fontSize: 15, fontWeight: '700' },

  hint: {
    color: theme.textMuted, fontSize: 12, lineHeight: 19, marginTop: 18, textAlign: 'center',
  },
  mono: { fontFamily: Platform.OS === 'web' ? 'monospace' : undefined, color: theme.textSecondary },
});
