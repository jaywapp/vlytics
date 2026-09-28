# syntax=docker/dockerfile:1.7
ARG NGINX_IMAGE
FROM ${NGINX_IMAGE}
RUN apk add --no-cache --upgrade libexpat=2.8.5-r0
