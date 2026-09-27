# syntax=docker/dockerfile:1.7

ARG POSTGRES_IMAGE

FROM golang:1.26.8-alpine3.24@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c AS gosu-builder

ADD --checksum=sha256:33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b \
    https://codeload.github.com/tianon/gosu/tar.gz/6456aaa0f3c854d199d0f037f068eb97515b7513 \
    /tmp/gosu.tar.gz

WORKDIR /src
RUN set -eux; \
    mkdir -p /out; \
    tar -xzf /tmp/gosu.tar.gz --strip-components=1; \
    test "$(sha256sum /tmp/gosu.tar.gz | cut -d ' ' -f 1)" = "33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b"; \
    grep -Fx 'module github.com/tianon/gosu' go.mod; \
    grep -Eq 'Version[[:space:]]*=[[:space:]]*"1[.]19"' version.go; \
    sha256sum go.mod go.sum > /out/gosu-module-files.sha256; \
    printf '%s\n' \
        'source_url=https://codeload.github.com/tianon/gosu/tar.gz/6456aaa0f3c854d199d0f037f068eb97515b7513' \
        'source_commit=6456aaa0f3c854d199d0f037f068eb97515b7513' \
        'source_archive_sha256=33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b' \
        'builder=golang:1.26.8-alpine3.24@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c' \
        > /out/gosu-source-provenance.txt
RUN set -eux; \
    go mod download; \
    go mod verify > /out/gosu-module-verification.txt; \
    sha256sum -c /out/gosu-module-files.sha256
RUN set -eux; \
    CGO_ENABLED=0 go build \
        -mod=readonly \
        -trimpath \
        -ldflags '-d -w' \
        -buildvcs=false \
        -o /out/gosu \
        .; \
    /out/gosu --version | tee /out/gosu-version.txt; \
    grep -F '1.19' /out/gosu-version.txt; \
    /out/gosu nobody id; \
    /out/gosu nobody ls -l /proc/self/fd > /dev/null; \
    go version -m /out/gosu > /out/gosu-build-metadata.txt; \
    grep -F 'go1.26.8' /out/gosu-build-metadata.txt; \
    sha256sum /out/gosu | cut -d ' ' -f 1 > /out/gosu.sha256

FROM ${POSTGRES_IMAGE}

LABEL io.vlytics.gosu.source="https://github.com/tianon/gosu" \
      io.vlytics.gosu.version="1.19" \
      io.vlytics.gosu.revision="6456aaa0f3c854d199d0f037f068eb97515b7513" \
      io.vlytics.gosu.builder="golang:1.26.8-alpine3.24@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c"

COPY --from=gosu-builder /out/gosu /usr/local/bin/gosu
COPY --from=gosu-builder /out/gosu.sha256 /usr/local/share/vlytics/gosu.sha256
COPY --from=gosu-builder /out/gosu-build-metadata.txt /usr/local/share/vlytics/gosu-build-metadata.txt
COPY --from=gosu-builder /out/gosu-module-files.sha256 /usr/local/share/vlytics/gosu-module-files.sha256
COPY --from=gosu-builder /out/gosu-module-verification.txt /usr/local/share/vlytics/gosu-module-verification.txt
COPY --from=gosu-builder /out/gosu-source-provenance.txt /usr/local/share/vlytics/gosu-source-provenance.txt
COPY --from=gosu-builder /out/gosu-version.txt /usr/local/share/vlytics/gosu-version.txt

RUN set -eux; \
    chmod 0755 /usr/local/bin/gosu; \
    test "$(sha256sum /usr/local/bin/gosu | cut -d ' ' -f 1)" = "$(cat /usr/local/share/vlytics/gosu.sha256)"; \
    gosu --version | grep -F '1.19'; \
    gosu nobody id; \
    gosu nobody ls -l /proc/self/fd > /dev/null; \
    test "$(gosu postgres id -u)" = "$(id -u postgres)"
