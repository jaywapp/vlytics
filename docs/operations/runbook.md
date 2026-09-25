# Vlytics 개인용 운영 Runbook

이 문서는 검증된 MVP를 단일 개인용 호스트에서 비공개로 운영하기 위한 절차다. 실제 KOVO 수집, 유료 Provider 호출, 호스트 구매 또는 공개 네트워크 변경은 포함하지 않는다. `infra/compose.production.yaml`은 배포 패키지이며 OP-001~004가 해결되기 전에는 의도적으로 기동 전 검사에서 중단된다.

## 배포 경계

- React SPA/Nginx만 호스트의 `127.0.0.1:${WEB_PORT}`에 bind한다. API는 host port가 없으며 private compose network의 `api:8000`에서만 접근된다. 다른 장치에서는 인증된 SSH/VPN tunnel로 SPA 진입점에 연결한다.
- PostgreSQL은 host port를 publish하지 않고 `private` internal network에서만 접근한다. initdb 전용 `vlytics_bootstrap_admin`은 `vlytics_migrator` 생성 직후 `NOLOGIN`으로 잠기며, migrator도 최초 migration 끝에서 `NOSUPERUSER`로 강등된다.
- `migrate`, `api`, `worker`는 하나의 digest 고정 backend image를 사용한다. command는 각각 `vlytics-migrate`, `vlytics-api`, `vlytics-worker`로 분리한다.
- API는 `vlytics_read_api_login`, worker는 `vlytics_engine_login`, 일회성 migration은 `vlytics_migrator` URL을 사용한다. collector와 Market ingest URL은 그 역할이 필요한 독립 작업에만 주입한다.
- backend container는 UID/GID 65532, read-only root filesystem, `/tmp` tmpfs, 모든 capability drop, `no-new-privileges`로 실행한다. PostgreSQL만 공식 image 동작에 필요한 최소 capability와 영속 volume을 가진다.
- 모든 operator route는 Bearer secret을 요구한다. `/health`는 liveness 정보만 반환하며 원문, 키, DB URL을 포함하지 않는다.

## 산출물과 로컬 검사

| 파일 | 용도 |
| --- | --- |
| `infra/compose.production.yaml` | 운영용 독립 compose, SPA 단일 진입점, private network와 최소 권한 |
| `frontend/Dockerfile` | digest 고정 Node build와 non-root Nginx runtime 다단계 image |
| `frontend/nginx.production.conf` | SPA fallback, `/api` internal reverse proxy와 security header |
| `frontend/.dockerignore` | env, test artifact, local dependency의 build context 유입 차단 |
| `infra/.env.example` | 키 이름과 placeholder만 제공하는 예시 |
| `infra/postgres-init/001-create-migrator.sql` | initdb bootstrap 역할 잠금과 one-time migrator 생성 |
| `infra/operational.production.example.toml` | OP gate를 드러내는 fail-closed 운영 설정 예시 |
| `infra/live-dry-run-evidence.example.json` | 실제 설정 hash에 묶이는 OP-004 증거 형식 |
| `infra/scripts/Test-DeploymentPackage.ps1` | Docker 없이도 가능한 정적 보안·구문 검사 |
| `infra/scripts/Invoke-Preflight.ps1` | secret, OP 설정, dry-run hash, image, compose, 시계 활성화 검사 |
| `infra/scripts/Backup-Database.ps1` | custom-format logical backup과 SHA-256 manifest 생성 |
| `infra/scripts/Invoke-RestoreDrill.ps1` | 격리 DB restore, schema/count/invariant 비교와 자동 정리 |

Windows PowerShell 5.1에서 저장소 루트 기준으로 다음을 실행한다.

```powershell
powershell.exe -NoProfile -NonInteractive -File .\infra\scripts\Test-DeploymentPackage.ps1
```

Docker CLI가 없으면 정적 compose·secret 예시·TOML/JSON 구문 검사는 통과하고 Docker parser 검사는 `skipped-no-docker-cli`로 명시된다. 이 결과를 image build 성공으로 해석하지 않는다.

## Secret 준비와 회전

실제 값은 Git에 저장하지 않는다. secret manager가 제공한 프로세스 환경 또는 ACL로 제한하고 작업 직후 삭제하는 untracked 임시 env 파일을 사용한다. 콘솔, issue, manifest, restore report에는 값을 출력하지 않는다.

필수 secret은 다섯 DB 역할의 비밀번호/URL, operator와 readonly 인증값, backup credential, alert 목적지, 세 Provider API key다. 최초 initdb에서는 migrator 비밀번호를 bootstrap 과정에도 사용하지만 bootstrap login은 init script가 즉시 비활성화한다. DB URL은 역할별로 분리하며 복구 작업은 호스트별 별도 관리자 절차로 수행한다.

회전 순서는 다음과 같다.

1. 새 secret을 secret manager에 생성하되 기존 값은 유지한다.
2. bootstrap DB administrator로 해당 login role의 비밀번호를 바꾼다. 최초 migration 후 `vlytics_migrator`에는 다른 role을 바꿀 지속 권한이 없다.
3. API/worker에 새 URL과 인증값을 주입해 한 서비스씩 재시작한다.
4. `/health`, 인증된 schedule/operations 조회, worker lease 진행을 확인한다.
5. 이전 secret을 폐기하고 감사 시각과 담당자를 secret manager에 기록한다.

Provider key 회전 시 worker를 먼저 정지하고 새 key·model version·budget을 preflight한 뒤 다시 시작한다. operator secret 회전 시 API를 재시작하고 기존 client token을 즉시 폐기한다.

## Web image와 same-origin 배선

frontend image는 두 base image를 모두 digest로 전달해야 build된다. `NODE_IMAGE`는 Node 22 계열 build image, `NGINX_IMAGE`는 UID 101로 동작하는 unprivileged Nginx image여야 한다. tag나 빈 build arg를 사용하지 않는다.

```powershell
docker build -f .\frontend\Dockerfile `
  --build-arg NODE_IMAGE=$env:VLYTICS_NODE_BUILD_IMAGE `
  --build-arg NGINX_IMAGE=$env:VLYTICS_NGINX_RUNTIME_IMAGE `
  --tag vlytics-frontend:release .\frontend
```

build arg와 Vite 환경에는 token, operator secret, API key, DB URL을 넣지 않는다. SPA는 compile-time endpoint를 주입하지 않고 상대 경로 `/api/v1/...`만 호출한다. Nginx는 `/api/`를 private network의 `http://api:8000`으로 전달하고 Authorization header를 보존한다. `/history`, `/performance`, `/operations` 같은 browser route는 `index.html`로 fallback한다.

완성 이미지를 private registry에 push한 뒤 registry가 반환한 digest를 `VLYTICS_FRONTEND_IMAGE`에 기록한다. `VLYTICS_NODE_BUILD_IMAGE`, `VLYTICS_NGINX_RUNTIME_IMAGE`, `VLYTICS_FRONTEND_IMAGE` 모두 `@sha256:<64 hex>` 형식이어야 한다. production compose에서 외부에 열린 포트는 SPA의 loopback 8080 하나뿐이다.

운영 host에서는 `/healthz` 200, `/` SPA 응답, 인증 없는 `/api/v1/schedule` 401, 인증된 동일 URL 200을 같은 origin으로 확인한다. 개발 Vite proxy와 Playwright route interception은 client의 상대 `/api` 계약을 검증하지만 Nginx image 자체의 성공 증거가 아니므로 Docker build와 compose smoke를 별도로 수행한다.
## 운영 활성화 절차

1. `release-checklist.md`의 OP-001~004와 시계, backup, alert 항목을 모두 채운다. Market OP-005는 `missing` 상태로 시작할 수 있다.
2. backend와 frontend image를 빌드·검증한 뒤 tag가 아닌 digest(`...@sha256:...`)로 `VLYTICS_BACKEND_IMAGE`를 고정한다.
3. `operational.production.example.toml`을 저장소 밖의 운영 경로로 복사하고 placeholder, `unconfigured`, 0인 비용·보존·한도 값을 실제 승인값으로 바꾼다. source bulk collection은 OP-001 근거와 한도가 확정될 때만 켠다.
4. 승인된 소량 source와 실제 세 Provider로 T-60 dry-run을 수행한다. 동일 설정의 canonical SHA-256, 완료 시각, source sync/freeze 성공, 세 Provider 확인을 evidence JSON에 기록한다. 설정을 바꾸거나 `[activation].dry_run_evidence_max_age_hours`가 지나면 dry-run을 다시 수행한다. worker는 `VLYTICS_LIVE_DRY_RUN_EVIDENCE_PATH`의 파일을 크기 제한 내에서 직접 검증한다.
5. 외부 secret을 임시 env 파일 또는 현재 프로세스 환경에 주입하고 preflight를 실행한다.

```powershell
powershell.exe -NoProfile -NonInteractive -File .\infra\scripts\Invoke-Preflight.ps1 `
  -EnvironmentFile D:\private\vlytics.env `
  -OperationalConfig D:\private\vlytics.production.toml `
  -DryRunEvidence D:\private\vlytics-live-dry-run.json
```

6. backup과 restore drill이 통과한 뒤 migration을 일회 실행한다.

```powershell
$env:VLYTICS_BACKUP_DATABASE_URL = '<injected externally>'
$env:VLYTICS_MIGRATION_DATABASE_URL = '<injected externally>'
powershell.exe -NoProfile -NonInteractive -File .\infra\scripts\Invoke-RestoreDrill.ps1

docker compose --env-file D:\private\vlytics.env -f .\infra\compose.production.yaml run --rm migrate
docker compose --env-file D:\private\vlytics.env -f .\infra\compose.production.yaml up -d postgres api worker frontend
```

7. `docker compose ps`, API health, 인증 401/200 경계, operations coverage, worker job lease를 확인한다. 첫 실제 경기 전까지 source 요청량과 Provider 비용을 낮은 승인값으로 유지한다.

## Backup, PITR와 복구

`Backup-Database.ps1`은 `VLYTICS_BACKUP_DATABASE_URL`에서 URL을 읽고 custom-format dump, SHA-256, DB명·PostgreSQL 버전·크기만 담은 manifest를 만든다. URL과 비밀번호는 artifact에 기록하지 않는다.

```powershell
$env:VLYTICS_POSTGRES_BIN = 'C:\Program Files\PostgreSQL\17\bin'
$env:VLYTICS_BACKUP_DATABASE_URL = '<injected externally>'
powershell.exe -NoProfile -NonInteractive -File .\infra\scripts\Backup-Database.ps1 -OutputDirectory D:\private\backups
```

운영 host의 단일 volume은 backup이 아니다. OP-002에서 암호화된 외부 목적지, 보존 기간, RPO/RTO와 알림을 정한다. PostgreSQL WAL archive 또는 관리형 PITR를 켜고 복구 시각을 정기 검증한다. logical dump는 migration 전과 정기 검증용이며 PITR를 대체하지 않는다.

분기마다 restore drill을 별도 DB에서 수행한다. 스크립트는 migration checksum, 네 domain schema, table set, 모든 table row count, prediction/job/published-event 중복 및 orphan projection을 비교하고 임시 DB를 삭제한다. 실패한 dump나 DB는 민감 자료로 취급하고 즉시 격리·삭제한다.

실제 복구 순서는 worker 정지, 원본 격리, 목표 시각/PITR 또는 검증된 dump restore, 위 불변식 검사, API read-only 확인, worker 한 인스턴스 재개, coverage 재조정이다. RPO 범위를 벗어난 관측은 과거 시각으로 소급하지 않는다.

## 재시작, upgrade와 rollback

API/worker는 `restart: unless-stopped`이고 migration 완료 후 시작한다. worker job은 semantic key와 unique index로 중복 enqueue를 막고, lease 만료 작업만 회수하며 이미 성공한 Provider를 다시 호출하지 않는다. 재시작 후 operations 화면에서 running lease, abandoned attempt, 동일 job key, 성공 prediction 수를 확인한다.

Upgrade 전 현재 image digest, config hash, migration checksum, backup hash를 기록하고 restore drill을 실행한다. 새 backend image의 migration을 한 번 실행한 뒤 API, frontend, worker 순으로 교체한다. frontend-only 결함은 이전 frontend digest로 즉시 되돌리고 `/healthz`와 same-origin `/api`를 다시 확인한다. app-only 결함이고 schema가 하위 호환이면 이전 digest로 되돌린다. forward-only migration 이후 schema rollback이 필요하면 worker/API를 정지하고 검증된 backup/PITR로 전체 DB를 복구한다. `docker compose down -v`는 운영 절차에서 사용하지 않는다.

## 감시와 장애 대응

- **시계:** NTP 동기화가 아니면 worker를 시작하지 않는다. 시작 후 offset/동기화 상태를 경보에 연결한다. 시간 이상 동안 생성된 prediction은 검토 전 평가에 포함하지 않는다.
- **source/coverage:** sync 시각, expected/loaded/missing/unsupported와 429/5xx를 본다. OP-001 한도를 넘기지 말고 반복 오류 시 bulk collection을 끈다.
- **Provider/예산:** 일/월 호출·금액 reservation을 감시한다. 한도 초과 또는 통화/model alias 불일치는 해당 Provider를 fail-closed로 두며 다른 결과를 삭제하지 않는다.
- **deadline:** T-60 시작 허용 30초, 완료 grace 300초, 경기 시작 이후 완료 거부를 유지한다. late 결과를 수동 publish하지 않는다.
- **DB:** health, volume 여유, backup age, restore drill age, replication/WAL archive 상태를 경보에 연결한다.
- **Web:** frontend `/healthz`, SPA root, internal API proxy 오류율을 감시한다. 번들 또는 image에 token이 포함되면 즉시 폐기하고 재빌드한다.
- **Market:** 실제 adapter 전 `missing`은 정상 운영 상태다. 0 또는 임의 quote로 대체하지 않는다.

중대한 장애에서는 worker를 먼저 정지해 외부 비용과 오염을 막고 API를 read-only 조회용으로 유지한다. correlation ID, job/prediction ID, config hash, image digest와 시각만 사건 기록에 남기며 secret·Raw·Provider 원문은 복사하지 않는다. 원인 제거 후 preflight와 필요한 restore/replay 검증을 다시 통과하고 worker 한 인스턴스부터 재개한다.