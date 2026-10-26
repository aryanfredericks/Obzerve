# Obzerve dashboard: static build served by nginx, which also proxies /api to the
# API so the browser talks to a single origin. Build from the repository root:
#   podman build -f web/Dockerfile -t obzerve-web .

FROM docker.io/library/node:24-alpine AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# Leave empty for real deployments: users enter their API key in the dashboard.
ARG VITE_OBZERVE_API_KEY=""
ENV VITE_OBZERVE_API_KEY=$VITE_OBZERVE_API_KEY
RUN npm run build

FROM docker.io/library/nginx:1.27-alpine
# Where nginx forwards /api requests (rendered into the config at start-up).
ENV OBZERVE_API_UPSTREAM=http://api:8000
COPY deploy/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
