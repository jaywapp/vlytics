# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
FROM ${NODE_IMAGE}
COPY patch-npm-bundle.cjs /tmp/patch-npm-bundle.cjs
RUN npm install --global --ignore-scripts npm@12.1.0 \
    && npm install --prefix /tmp/vlytics-npm-patches --ignore-scripts --omit=dev \
        --no-audit --no-fund --package-lock=false brace-expansion@5.0.11 undici@6.28.1 \
    && node /tmp/patch-npm-bundle.cjs /usr/local/lib/node_modules/npm /tmp/vlytics-npm-patches/node_modules \
    && rm -rf /tmp/vlytics-npm-patches /tmp/patch-npm-bundle.cjs \
    && npm --version \
    && npm cache clean --force
