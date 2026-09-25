# Phase 8: frontend container. Static build served by nginx, which proxies
# /api/* to the backend service by its Docker Compose service name - no
# frontend code changes needed between local dev (Vite's proxy) and this
# (nginx's proxy); both point the same relative /api path somewhere.
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
