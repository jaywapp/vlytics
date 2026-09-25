# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
ARG NGINX_IMAGE

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
COPY --chown=101:101 nginx.production.conf /etc/nginx/conf.d/default.conf
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html
USER 101:101
EXPOSE 8080