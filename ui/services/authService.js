/**
 * authService — token lifecycle + the authenticated fetch every other client uses.
 *
 * The backend authenticates on a bearer JWT (see citra_workflow/auth_routes.py).
 * This module is the only place that knows where the token lives, so nothing
 * else has to think about storage or the 401 path.
 *
 * `authenticatedFetch` deliberately does NOT throw on a non-2xx response — the
 * WorkflowService methods read `response.ok` and build a rich error from the
 * body. It DOES surface a 401 by clearing the session and notifying listeners,
 * so an expired token sends the user back to sign-in instead of leaving the UI
 * showing stale data with silently failing requests.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import { WORKFLOW_API_BASE, API_CONFIG } from '../config/config';

const TOKEN_KEY = API_CONFIG.TOKEN_STORAGE_KEY;
const USER_KEY = API_CONFIG.USER_STORAGE_KEY;

class AuthService {
  constructor() {
    this._token = null;
    this._user = null;
    this._listeners = new Set();
  }

  // ── session state ───────────────────────────────────────────────────

  /** Subscribe to sign-in / sign-out. Returns an unsubscribe function. */
  subscribe(fn) {
    this._listeners.add(fn);
    return () => this._listeners.delete(fn);
  }

  _emit() {
    for (const fn of this._listeners) {
      try {
        fn({ token: this._token, user: this._user });
      } catch (err) {
        console.error('[auth] listener threw:', err);
      }
    }
  }

  getToken() {
    return this._token;
  }

  getUser() {
    return this._user;
  }

  isAuthenticated() {
    return Boolean(this._token);
  }

  /**
   * Load a persisted session on app start.
   *
   * A token in storage is not trusted on its own — it may be expired or belong
   * to a deleted user. We call /api/auth/me and only consider the session live
   * if the server agrees. Otherwise the stored token is discarded.
   */
  async restore() {
    try {
      const [token, rawUser] = await Promise.all([
        AsyncStorage.getItem(TOKEN_KEY),
        AsyncStorage.getItem(USER_KEY),
      ]);
      if (!token) return null;

      this._token = token;
      this._user = rawUser ? JSON.parse(rawUser) : null;

      const resp = await fetch(`${WORKFLOW_API_BASE}/api/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) {
        await this.signOut();
        return null;
      }
      const data = await resp.json();
      this._user = data.user || this._user;
      await AsyncStorage.setItem(USER_KEY, JSON.stringify(this._user));
      this._emit();
      return this._user;
    } catch (err) {
      // Network failure on restore is not proof the token is bad, but we cannot
      // confirm it either. Treat as signed out rather than letting the app run
      // in a half-authenticated state.
      console.warn('[auth] session restore failed:', err?.message || err);
      await this.signOut();
      return null;
    }
  }

  async signIn(email, password) {
    const resp = await fetch(`${WORKFLOW_API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });

    let body = null;
    try {
      body = await resp.json();
    } catch {
      body = null;
    }

    if (!resp.ok) {
      const detail = body?.detail;
      const message =
        (typeof detail === 'string' && detail) ||
        detail?.message ||
        `Sign in failed (${resp.status}).`;
      throw new Error(message);
    }

    this._token = body.token;
    this._user = body.user;
    await AsyncStorage.multiSet([
      [TOKEN_KEY, this._token],
      [USER_KEY, JSON.stringify(this._user)],
    ]);
    this._emit();
    return this._user;
  }

  async signOut() {
    this._token = null;
    this._user = null;
    try {
      await AsyncStorage.multiRemove([TOKEN_KEY, USER_KEY]);
    } catch (err) {
      console.warn('[auth] could not clear stored session:', err?.message || err);
    }
    this._emit();
  }

  // ── the shared fetch ────────────────────────────────────────────────

  /**
   * fetch() with the bearer token attached.
   *
   * Returns the raw Response — callers inspect `.ok` themselves. A 401 clears
   * the session first, so the app routes back to sign-in.
   */
  async authenticatedFetch(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (this._token) headers.Authorization = `Bearer ${this._token}`;
    if (!headers['Content-Type'] && options.body && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
    }

    const resp = await fetch(url, { ...options, headers });

    if (resp.status === 401 && this._token) {
      // The token was accepted at sign-in and is not now: expired, revoked, or
      // the user was removed. Drop it so the UI stops pretending.
      console.warn('[auth] 401 from', url, '— clearing session');
      await this.signOut();
    }
    return resp;
  }
}

const authService = new AuthService();
export default authService;
