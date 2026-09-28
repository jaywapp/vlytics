# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
FROM ${NODE_IMAGE}
RUN npm install --global --ignore-scripts npm@12.1.0 \
    && npm cache clean --force
