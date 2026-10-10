# syntax=docker/dockerfile:1

# ── Stage 1: build the static SPA ───────────────────────────────────────────
FROM node:22-alpine AS build
WORKDIR /app

# Reproducible install from the lockfile.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# VITE_* are baked into the static bundle at build time. In a Dockerfile build,
# the hosting platform's service variables are only visible to RUN steps if declared as ARG,
# so receive them here and expose as ENV for `vite build`. These are PUBLIC values
# (publishable key) — never put secrets in VITE_*.
ARG VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY
RUN npm run build   # → /app/dist

# ── Stage 2: serve with Caddy ───────────────────────────────────────────────
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
# the hosting platform injects $PORT; the Caddyfile binds to it.
