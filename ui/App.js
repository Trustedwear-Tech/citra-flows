/**
 * App — the whole route table: landing -> sign in -> builder.
 *
 * Three states and one rule: nothing but the landing and sign-in screens render
 * without a verified session. On boot we try to restore a stored token, but a
 * stored token alone is not trusted — authService.restore() asks the server and
 * only reports a user if it agrees (see services/authService.js).
 */

import React, { useEffect, useState, useCallback } from 'react';
import { View, ActivityIndicator, StyleSheet, StatusBar, Text } from 'react-native';
import LandingScreen from './components/LandingScreen';
import LoginScreen from './components/LoginScreen';
import AppShell from './components/AppShell';
import authService from './services/authService';
import theme from './components/theme';

const ROUTE = {
  BOOTING: 'booting',
  LANDING: 'landing',
  LOGIN: 'login',
  APP: 'app',
};

export default function App() {
  const [route, setRoute] = useState(ROUTE.BOOTING);
  const [user, setUser] = useState(null);

  // Restore a previous session once, on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const restored = await authService.restore();
      if (cancelled) return;
      setUser(restored);
      setRoute(restored ? ROUTE.APP : ROUTE.LANDING);
    })();
    return () => { cancelled = true; };
  }, []);

  // authService clears the session on any 401. Follow it, so an expired token
  // mid-session drops to sign-in instead of leaving a dead UI on screen.
  useEffect(
    () =>
      authService.subscribe(({ token, user: nextUser }) => {
        if (!token) {
          setUser(null);
          setRoute((r) => (r === ROUTE.BOOTING ? r : ROUTE.LOGIN));
        } else {
          setUser(nextUser);
        }
      }),
    []
  );

  const handleSignedIn = useCallback((signedInUser) => {
    setUser(signedInUser);
    setRoute(ROUTE.APP);
  }, []);

  const handleSignedOut = useCallback(() => {
    setUser(null);
    setRoute(ROUTE.LANDING);
  }, []);

  let content;
  switch (route) {
    case ROUTE.BOOTING:
      content = (
        <View style={styles.center}>
          <ActivityIndicator size="large" color={theme.primary} />
          <Text style={styles.bootText}>Starting Citra Flows…</Text>
        </View>
      );
      break;
    case ROUTE.LOGIN:
      content = (
        <LoginScreen
          onSignedIn={handleSignedIn}
          onBack={() => setRoute(ROUTE.LANDING)}
        />
      );
      break;
    case ROUTE.APP:
      content = <AppShell user={user} onSignedOut={handleSignedOut} />;
      break;
    case ROUTE.LANDING:
    default:
      content = <LandingScreen onSignIn={() => setRoute(ROUTE.LOGIN)} />;
      break;
  }

  return (
    <View style={styles.root}>
      <StatusBar barStyle="light-content" />
      {content}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: theme.background },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 14 },
  bootText: { color: theme.textSecondary, fontSize: 14 },
});
