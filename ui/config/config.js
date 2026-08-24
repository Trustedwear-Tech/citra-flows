// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * Build-time configuration.
 *
 * `WORKFLOW_API_BASE` comes from `EXPO_PUBLIC_WORKFLOW_API_BASE`, set when the
 * bundle is built:
 *
 *   EXPO_PUBLIC_WORKFLOW_API_BASE=https://flows.example.com npm run build:web
 *
 * It must be written as a LITERAL `process.env.EXPO_PUBLIC_...` member
 * expression. Metro inlines these by static text substitution, so a dynamic
 * lookup — `process.env[key]`, which this file used to do behind an `env()`
 * helper — is never substituted and the fallback always won. That silently
 * made every EXPO_PUBLIC_* var here inert, including in Docker builds that
 * passed the value as a build arg.
 *
 * It is baked into the JS, so setting the variable on a *running* container
 * has no effect; rebuild to change it.
 */

// eslint-disable-next-line no-undef
const RAW_API_BASE = process.env.EXPO_PUBLIC_WORKFLOW_API_BASE;

/**
 * Base URL of the citra-workflow FastAPI service (no trailing slash).
 *
 * The default matches the service's own default port (WORKFLOW_SERVICE_PORT
 * =9200, and the port `citra-workflow/Dockerfile` exposes), so a stock backend
 * and a stock UI talk to each other with no configuration.
 */
export const WORKFLOW_API_BASE = String(
  RAW_API_BASE || 'http://localhost:9200'
).replace(/\/+$/, '');

export const API_CONFIG = {
  WORKFLOW_API_BASE,
  /** Where the token is kept between reloads. */
  TOKEN_STORAGE_KEY: 'citra_flows_token',
  USER_STORAGE_KEY: 'citra_flows_user',
  /** Default request timeout (ms). AI authoring calls override this. */
  DEFAULT_TIMEOUT: 30000,
};

export const CONFIG = API_CONFIG;

export default API_CONFIG;
