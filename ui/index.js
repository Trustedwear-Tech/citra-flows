// Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
// Author: Rohit Kumar Chandan
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at http://www.apache.org/licenses/LICENSE-2.0

/**
 * Entry point. registerRootComponent handles both the web bundle and a native
 * build, so App.js never needs to know which it is running under.
 */
import { registerRootComponent } from 'expo';
import App from './App';

registerRootComponent(App);
