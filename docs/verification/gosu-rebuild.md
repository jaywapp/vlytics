# gosu 1.19 재빌드 근거

## 목적과 범위

CI 36283661022에서 `postgres:17.11-alpine`의 `/usr/local/bin/gosu`가 Go 1.24.6 표준 라이브러리 HIGH/CRITICAL 22행을 발생시켰다. 모든 행의 상태는 `fixed`였고 가장 높은 수정 기준은 Go 1.25.13 또는 1.26.6이었다. PostgreSQL major 17과 공식 entrypoint를 유지하기 위해 [gosu 1.19의 동일 소스 commit](https://github.com/tianon/gosu/tree/6456aaa0f3c854d199d0f037f068eb97515b7513)을 현재 지원되는 Go 1.26.8로 다시 빌드하는 후보를 `infra/images/postgres.Dockerfile`에 정의했다.

이 후보는 아직 실제 Docker build, Trivy 검사, 배포를 통과한 결과가 아니다. HIGH/CRITICAL을 무시하거나 `--ignore-unfixed`를 사용하지 않으며, 실제 재검사가 통과할 때까지 release gate는 닫힌 상태다.

## 고정 입력

| 입력 | 고정 값 | 근거 |
|---|---|---|
| PostgreSQL runtime | `postgres:17.11-alpine3.24@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24` | Docker Official Image의 PostgreSQL 17.11 Alpine 3.24 index digest |
| Go builder | `golang:1.26.8-alpine3.24@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c` | Docker Official Image의 Go 1.26.8 Alpine 3.24 index digest |
| gosu source | commit `6456aaa0f3c854d199d0f037f068eb97515b7513` | upstream lightweight tag `1.19`이 가리키는 commit |
| source archive | `https://codeload.github.com/tianon/gosu/tar.gz/6456aaa0f3c854d199d0f037f068eb97515b7513` | commit 고정 URL |
| archive SHA256 | `33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b` | 위 archive를 2026-09-27에 계산한 값이며 Docker `ADD --checksum`이 다시 검증 |
| Go modules | `github.com/moby/sys/user v0.1.0`, `golang.org/x/sys v0.1.0` | upstream `go.mod`와 `go.sum`; build 중 `go mod verify` 실행 |

Docker Official Images의 mutable tag가 아니라 위 index digest를 build input으로 사용한다. CI inventory의 `build_inputs.postgres`에도 같은 PostgreSQL digest를 기록해야 하며, 최종 파생 이미지 자체는 별도 immutable image ID로 SBOM과 취약점 검사를 받는다. compiler도 `golang` 항목으로 검사한다. 파생 PostgreSQL의 `io.vlytics.gosu.builder` label이 있으면 그 digest의 compiler 검사 생략을 collector가 거부한다.

## 빌드와 내부 검증

다음 명령은 검증 환경에서 실행할 후보이며 이 문서 작성 시점에는 실행하지 않았다.

```sh
docker build \
  --file infra/images/postgres.Dockerfile \
  --build-arg POSTGRES_IMAGE=postgres:17.11-alpine3.24@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24 \
  --tag vlytics-postgres:gosu-go1.26.8 \
  infra/images
```

Builder는 source archive checksum과 module 선언을 확인하고 `go mod verify`와 원본 go.mod/go.sum hash 재확인 후 `-mod=readonly`, `CGO_ENABLED=0`, `-trimpath`, `-ldflags '-d -w'`로 빌드한다. archive에는 `.git`이 없으므로 VCS 정보를 꾸며 넣지 않고 `-buildvcs=false`를 사용한다. 대신 final image의 `io.vlytics.gosu.*` labels와 `/usr/local/share/vlytics/gosu-source-provenance.txt`에 실제 source commit을 기록한다.

다음 파일이 final image에 남는다.

- `gosu.sha256`: 교체된 binary SHA256
- `gosu-build-metadata.txt`: `go version -m`이 출력한 Go 버전, module과 build settings
- `gosu-module-files.sha256`: upstream `go.mod`와 `go.sum`의 SHA256
- `gosu-module-verification.txt`: `go mod verify` 결과
- `gosu-source-provenance.txt`: source URL, commit, archive hash, builder digest
- `gosu-version.txt`: 빌드된 binary의 version 출력

Build 중 upstream과 같은 `gosu --version`, `gosu nobody id`, `gosu nobody ls -l /proc/self/fd` smoke를 builder와 PostgreSQL runtime stage에서 실행한다. Runtime stage에서는 PostgreSQL entrypoint의 실제 전환 대상인 `postgres` 사용자 UID 전환도 확인한다.

## 공급망 경계

공식 PostgreSQL image는 gosu 1.19 release의 PGP 서명된 prebuilt binary를 사용한다. 이 후보는 같은 upstream source commit을 사용하지만 그 서명 binary를 자체 CI build binary로 교체한다. 따라서 결과를 "공식 gosu binary" 또는 "공식 PostgreSQL image digest"로 표현하면 안 된다. source archive hash, builder digest, module 합계, binary hash와 SBOM을 하나의 evidence 묶음으로 보존해야 한다.

Upstream gosu가 수정된 Go toolchain으로 서명된 새 release를 제공하면 자체 재빌드보다 그 release를 우선 검토한다. Source archive hash 고정은 현재 가져온 bytes를 고정하지만 GitHub나 Go module 공급망 자체를 독립적으로 보증하지는 않는다.

## 남은 필수 검증

1. 네트워크가 허용된 CI에서 위 Dockerfile을 실제 build하고 모든 내부 smoke가 실행됐는지 확인한다.
2. `go version -m /usr/local/bin/gosu`와 보존된 SHA256이 기대 값과 일치하는지 확인한다.
3. 최신 Trivy DB로 파생 PostgreSQL image의 nonempty CycloneDX SBOM과 package inventory를 생성한다. 기존 Go 1.24.6 22행이 사라지고 HIGH/CRITICAL이 0인지 실제 결과로 판정한다.
4. 파생 image로 Compose PostgreSQL을 기동해 공식 entrypoint, initialization, migration, service-role SCRAM login, API/worker smoke, backup/restore 회귀를 실행한다.
5. CI artifact manifest에 파생 image ID와 `build_inputs.postgres`의 원본 PostgreSQL digest를 함께 남긴다.
6. 현재 후보는 native linux/amd64 CI build만 대상으로 한다. 다른 architecture를 게시하려면 각 platform에서 별도 build, runtime smoke, SBOM, vulnerability scan과 digest 증거가 필요하다.

이 검증이 끝나기 전에는 production image 참조나 release evidence에 이 후보를 사용하지 않는다.
