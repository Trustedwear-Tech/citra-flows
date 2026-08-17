// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: BUSL-1.1
//
// Licensed under the Business Source License 1.1. Non-production use is granted;
// production use requires a commercial licence until the Change Date, after
// which this file converts to Apache-2.0. See LICENSE at the repository root.

/**
 * Entry point. registerRootComponent handles both the web bundle and a native
 * build, so App.js never needs to know which it is running under.
 */
import { registerRootComponent } from 'expo';
import App from './App';

registerRootComponent(App);
