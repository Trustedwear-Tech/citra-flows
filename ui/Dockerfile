# Citra Flows web UI — Expo / react-native-web, exported to static files.
#
# Build from THIS directory (unlike the Python images, which build from the
# repo root):
#
#   docker build -t citra-flows/ui:local ui
#
# The API base URL is read from `process.env.EXPO_PUBLIC_WORKFLOW_API_BASE`
# by Metro at BUNDLE time, so it is baked into the JS and cannot be changed by
# setting an env var on the running container. It is a build arg for that
# reason — point it at wherever the browser (not the container) reaches the
# workflow API.
FROM node:20-alpine AS build

WORKDIR /app

# Install deps first so a source-only edit doesn't reinstall node_modules.
# `npm ci` needs a lockfile; fall back to `npm install` when one isn't present
# so a fresh clone still builds.
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

ARG EXPO_PUBLIC_WORKFLOW_API_BASE=http://localhost:9200
ENV EXPO_PUBLIC_WORKFLOW_API_BASE=${EXPO_PUBLIC_WORKFLOW_API_BASE}

# Exports static files to dist/.
RUN npx expo export --platform web --output-dir dist


FROM nginx:1.27-alpine AS runtime

# SPA fallback: the app routes client-side, so any unknown path must return
# index.html rather than nginx's 404, or a refresh on a deep link breaks.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
