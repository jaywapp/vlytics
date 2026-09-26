# Vlytics 운영 Release Checklist

- 판정일: 2026-09-20 (Asia/Seoul)
- 대상: `codex/implement-vlytics-mvp`
- 패키지 판정: **배포 준비 산출물 완료 / 운영 활성화 NO-GO**

NO-GO는 미확정 외부 운영값과 현재 host 시계·container runtime 상태에 대한 판정이다. 구현 또는 합성 MVP 검증 실패를 뜻하지 않는다. 실제 배포·유료 Provider·KOVO network·공개 접근 변경은 수행하지 않았다.

## 활성화 차단 장부

| Gate | 현재 상태 | 활성화 전 필수 증거 | 판정 |
| --- | --- | --- | --- |
| OP-001 source 접근 | 허용 요청량·정책 미확정, bulk collection false | 출처·결정일·분당 요청·동시성·429 정책 | 차단 |
| OP-002 host/backup/alert | host·region·비용·backup/PITR·alert 목적지 미확정 | 계약/비용, RPO/RTO, 외부 backup, restore 주기, 실제 alert test | 차단 |
| OP-003 Provider | 실제 model/version·통화·일/월 예산·key 미확정 | 세 Provider별 pinned ID/version, token/call/금액 cap, 소량 실제 smoke ID | 차단 |
| OP-004 live timing | 합성 replay 통과, 실제 소량 source/Provider dry-run 없음 | 활성 config hash와 일치하는 source sync·freeze·3 Provider evidence | 차단 |
| OP-005 Market | 실제 adapter 없음 | `missing` 상태 명시 | 허용 |
| Host clock | `W32Time` Stopped/Manual, 상태 조회 실패 | NTP service Running, 동기화 source/offset 확인 및 alert | 차단 |
| Container runtime | 로컬 Docker/Podman CLI 없음 | 운영 host에서 backend/frontend digest pull, frontend 다단계 build, `docker compose config`와 same-origin smoke | 차단 |

`infra/operational.production.example.toml`, `infra/.env.example`, `infra/live-dry-run-evidence.example.json`은 위 항목을 placeholder/0/false로 남겨 preflight가 성공할 수 없게 한다. 값을 임의로 채워 통과시키지 않는다.

## Security와 접근

- [x] React SPA만 host `127.0.0.1:${WEB_PORT}`에 bind된다.
- [x] API는 host port 없이 private network에서만 접근된다.
- [x] Nginx `/api/`가 internal `api:8000`으로 전달되고 SPA history fallback이 구성됐다.
- [x] PostgreSQL host port가 없고 internal network만 사용한다.
- [x] API/worker/migrate가 같은 digest 변수의 backend image를 사용한다.
- [x] frontend는 digest 고정 Node/Nginx base를 받는 다단계 build와 별도 final image digest를 사용한다.
- [x] frontend는 UID 101, read-only filesystem, tmpfs, capability drop, `no-new-privileges`로 실행된다.
- [x] frontend build context에서 `.env*`, local dependency와 test artifact를 제외한다.
- [x] API/worker command가 명시되어 있고 backend root filesystem은 read-only다.
- [x] non-root UID, capability drop, `no-new-privileges`, 제한된 tmpfs가 적용됐다.
- [x] DB URL은 read API/engine/migrator 역할로 분리했다.
- [x] committed env에는 placeholder만 있고 `.env`, dump, 운영 restore report는 ignore된다.
- [ ] 실제 backend/frontend final image와 Node/Nginx base를 digest로 고정하고 SBOM/vulnerability 결과를 승인했다.
- [ ] 운영 host에서 frontend `/healthz`, SPA route fallback, same-origin `/api` 401/200을 확인했다.
- [ ] operator/readonly secret을 생성하고 401/403/200 경계를 운영 host에서 확인했다.
- [ ] private tunnel/VPN 경로와 접근자·폐기 절차를 기록했다.

## 데이터베이스, backup과 복구

- [x] migration은 health한 PostgreSQL 뒤 일회성 service로 실행된다.
- [x] API/worker는 migration 성공 후에만 시작된다.
- [x] local PostgreSQL 17.11에서 실제 custom `pg_dump`와 `pg_restore`를 수행했다.
- [x] restore DB의 migration checksum, schema/table set, 모든 table row count를 원본과 비교했다.
- [x] duplicate prediction identity/job attempt/published event와 orphan projection이 0임을 확인했다.
- [x] restore 임시 DB와 dump를 자동 제거했고 종료 후 임시 DB 수가 0이었다.
- [ ] 외부 암호화 backup 목적지, 보존, RPO/RTO를 승인했다.
- [ ] WAL archive/관리형 PITR를 활성화하고 목표 시각 복구를 검증했다.
- [ ] restore drill 주기와 실패 alert 담당자를 정했다.

## Source, worker와 재시작

- [x] TASK-017에서 합성 source 오류, 연기, 취소, 결과 정정, lease 만료를 재생했다.
- [x] PostgreSQL unique/lease 계약으로 재시작 후 semantic job과 대표 성공 중복을 방지한다.
- [x] 성공 Provider 재호출 금지와 늦은 응답 discard를 검증했다.
- [x] T-60, 초기 30초, 완료 grace 300초, 경기 시작 이후 거부 계약이 고정됐다.
- [ ] OP-001 승인 한도로 소량 source sync를 실제 dry-run했다.
- [ ] 활성 config hash와 일치하고 설정된 유효기간 안에 있는 OP-004 evidence를 생성했으며 worker의 read-only 경로에 마운트했다.
- [ ] 운영 host에서 worker 재시작 후 중복 0, lease 회수, coverage 전진을 확인했다.

## Provider, 비용과 알림

- [x] budget reservation/settlement가 DB에 영속되고 unknown timeout은 보수적으로 청구된다.
- [x] Provider 하나의 timeout/refusal/schema 오류가 다른 Provider 결과를 취소하지 않는다.
- [x] 예산 초과 시 해당 호출을 fail-closed하는 계약을 검증했다.
- [ ] GPT·Claude·Gemini의 실제 pinned model/version과 pricing 출처를 기록했다.
- [ ] 통화, 일/월 금액·호출·token cap을 승인했다.
- [ ] source/coverage, Provider, budget, deadline, DB/backup/PITR, clock alert 목적지를 실제 시험했다.
- [ ] API/worker/DB healthcheck 실패와 재기동 alert를 시험했다.

## Release와 rollback

- [ ] 이전/new backend·frontend image digest, Node/Nginx base digest, config hash, migration checksum, backup hash를 release 기록에 남겼다.
- [ ] preflight가 placeholder, exact dry-run evidence, image, compose, clock을 모두 통과했다.
- [ ] migration → API → frontend → worker 순서와 각 health를 확인했다.
- [ ] frontend-only rollback을 이전 digest로 수행하고 SPA 및 same-origin API를 확인했다.
- [ ] schema 하위 호환 app rollback을 시험했다.
- [ ] forward-only migration rollback은 전체 restore/PITR로 수행하는 절차를 시험했다.
- [ ] `docker compose down -v`를 운영 절차와 자동화에서 배제했다.

## 2026-09-20 검증 증거

| 검사 | 결과 |
| --- | --- |
| TASK-017 backend full suite | clean PostgreSQL 17.11, 297 passed / 0 skipped |
| TASK-017 browser E2E | Chromium production bundle, 9 passed |
| TASK-018 PowerShell 구문 | 운영 script 5개, Windows PowerShell parser 통과 |
| Deployment package | static compose/security, placeholder secret, TOML/JSON syntax 통과 |
| Docker compose parser/backend·frontend image build | 로컬 CLI 부재로 미실행; 성공 주장하지 않음 |
| Frontend static contract | 다단계 Dockerfile, non-root Nginx, SPA fallback, internal `/api` proxy 정적 검사 통과 |
| Frontend local bundle/API path | production build와 Playwright에서 상대 `/api/v1/**` 경로 검증 |
| Backup/restore drill | 통과, PostgreSQL 17.11, migration 11, domain schema 4, table 38 |
| Restore row counts | 모든 domain table 원본/복구 일치 |
| Restore invariants | 네 항목 모두 0, cleanup 완료, 임시 DB 0 |
| Clock | W32Time Stopped/Manual; 활성화 차단 확인 |
| OP example fail-closed | placeholder와 미확정 값으로 preflight 거부 대상 |

restore report는 `artifacts/operations/`에 생성되며 Git에서 제외된다. report에는 연결 URL, 비밀번호, Raw, Provider 원문이 없고 해시와 집계 증거만 있다.

## 2026-09-21 Web P1 재검증

| 검사 | 결과 |
| --- | --- |
| Frontend lint / TypeScript | 통과 |
| Frontend unit | 3 files, 20 passed |
| Frontend production build | 47 modules, build 성공 |
| Chromium production bundle | 단일 worker 재검증 9 passed |
| 상대 API contract | production JS에 `/api/v1/` 존재, compile-time host 없음 |
| Bundle secret scan | token·VLYTICS 변수·API key·private key 패턴 0건 |
| Deployment package | frontend build contract·same-origin proxy·API 비노출 정적 검사 통과 |
| PowerShell 5.1 / example preflight | script 5개 parse 통과, placeholder/digest에서 예상 차단 |
| Nginx/Docker image build | Docker CLI 부재로 미실행, 운영 활성화 NO-GO 유지 |

병렬 Playwright 최초 실행은 기능 assertion이 아니라 trace artifact `ENOENT`로 8/9가 됐다. artifact 경합을 제거한 단일 worker 재실행에서 같은 9개가 모두 통과했다.
## 2026-09-25 CI 합성 Compose 검증

[GitHub Actions 실행](https://github.com/jaywapp/vlytics/actions/runs/36088207723)에서 다음을 통과했다.

| 검사 | 결과 |
| --- | --- |
| Backend image와 Compose | 저장소 구조에 맞춘 image 빌드, PostgreSQL·migration·API·worker 기동 |
| API 경계 | health 200, 익명 schedule 401, 합성 운영자 인증 schedule 200 |
| Worker 재시작 | 외부 수집이 금지된 합성 job을 1회 격리하고 재시작 후 attempt 1건 유지 |
| Migration과 복구 | 재실행 후 ledger 동일, custom dump를 격리 PostgreSQL 17.11에 복구해 checksum·job 기록 확인 |

이 검사는 개발용 비활성 설정만 사용한다. 운영 host의 production Compose, frontend Nginx image 기동, PITR, 실제 Provider·source 및 T-60 dry-run 검증은 남아 있다.

## 2026-09-27 CI frontend image 검증

[GitHub Actions 실행](https://github.com/jaywapp/vlytics/actions/runs/36270882620)에서 frontend production Dockerfile을 digest로 확인한 Node·Nginx base image로 빌드하고 합성 API와 연결했다.

| 검사 | 결과 |
| --- | --- |
| Nginx 컨테이너 | UID 101, read-only filesystem, UID/GID가 지정된 tmpfs, capability drop, `no-new-privileges`로 기동 |
| SPA와 보안 헤더 | `/healthz` 200, `/history` fallback, CSP 응답 확인 |
| Same-origin API | 익명 401, readonly 권한 403, operator 인증 200 확인 |
| Production Compose parser | 합성 값으로 `docker compose -f infra/compose.production.yaml config --quiet` 통과 ([CI 실행](https://github.com/jaywapp/vlytics/actions/runs/36271516676)); 운영 host 기동은 별도 게이트 |
| 기존 Compose smoke | worker 재시작 중복 0, migration 재실행, custom dump와 격리 복구 계속 통과 |

이 결과는 CI의 개발용 비활성 Compose에서 얻었다. 운영 host의 production Compose 배포, 실제 image digest 승인·PITR·알림·NTP·source 및 Provider live dry-run은 활성화 전 게이트로 남는다.

## 최종 Go/No-Go

운영 패키지 자체는 reviewable하고 restore 가능하다. **실제 활성화는 NO-GO**다. OP-001~004, host NTP, external backup/PITR, alert 목적지, pinned backend/frontend/base images와 운영 host frontend build·compose·same-origin smoke가 모두 완료된 뒤 이 체크리스트를 새 날짜로 다시 실행한다. Market `missing`만은 UC-005 C에 따라 Go 판단을 막지 않는다.
