# 운영 상태 감시 자동화 템플릿

이 문서는 `python -m vlytics.ops.health_evidence`로 읽기 전용 증거를 수집하고 `python -m vlytics.ops.health`로 판정하기 위한 운영 설계와 템플릿을 정의한다. production worker heartbeat는 별도 `vlytics.ops.heartbeat` 모듈로 기록한다. 수집기는 DB 트랜잭션을 `READ ONLY`로 강제하고 기존 heartbeat 및 외부 NTP 증거 파일만 읽는다. 수집기 자체는 알림 전송, 데이터베이스 갱신, worker heartbeat 기록, NTP 상태 변경, 호스트 예약 등록을 수행하지 않는다. 별도 `heartbeat_export`와 선택적 `health_dispatch` CLI의 계약은 아래에 구분한다.

## 판정 계약

입력 증거는 `operations-health-evidence-v1`이어야 한다. 모든 시각은 시간대가 포함된 ISO 8601 값이어야 한다.

```json
{
  "schema_version": "operations-health-evidence-v1",
  "collected_at": "2026-09-27T00:00:00+00:00",
  "worker_heartbeat_at": null,
  "ntp": null,
  "running_jobs": null,
  "provider_budgets": null,
  "expected_providers": [],
  "diagnostics": []
}
```

`null`은 해당 증거 생산자가 실패했거나 의존 증거가 없다는 뜻이다. 빈 배열은 수집이 성공했고 현재 행이 없다는 뜻이므로 서로 바꾸어 쓰지 않는다. `diagnostics`는 고정된 producer, status, code만 기록하며 DSN, 토큰, 예외 원문을 기록하지 않는다. 설정에 없는 provider 또는 설정과 다른 currency가 DB에서 발견되면 `provider_budgets`를 `null`로 닫고 provider 이름을 진단에 남긴다. 판정 CLI는 누락되거나 형식이 잘못된 증거를 `unknown`으로 판정하고 실패 상태로 종료한다.

기본 임계값은 다음과 같다.

| 점검 | 정상 기준 | 비정상 판정 |
| --- | --- | --- |
| worker heartbeat | 3분 이내 | 누락 `unknown`, 미래 또는 3분 초과 `critical` |
| 백업 신선도 | 25시간 이내, schema `2.0`, owner/ACL 보존 및 무결성 필드 존재 | 누락·불완전 `unknown`, 미래·노후·ACL 미보존 `critical` |
| 백업 파일 무결성 | 최신 manifest와 이름이 대응하는 dump가 존재하며 실제 크기·SHA-256 일치 | 증거 누락·불완전 `unknown`, 파일 누락·손상·읽기 실패 `critical` |
| NTP | 관측 10분 이내, 동기화됨, 절대 offset 1,000 ms 이하 | 누락 `unknown`, 미래·노후·비동기·offset 초과 `critical` |
| 실행 중 job | 시작 10분 이내이고 lease가 유효함 | 누락 `unknown`, 행 불완전 `unknown`, 노후·만료 `critical` |
| provider 예산 | 모든 구성 provider의 일/월 금액 및 호출 수가 존재함 | 누락 `unknown`, 80% 이상 `warning`, 100% 이상 `critical` |

전체 상태 우선순위는 `critical`, `unknown`, `warning`, `ok` 순이다. 종료 코드는 정상 `0`, 경고 `2`, 위험 또는 증거 불명 `3`이다. 출력의 `notification_required`는 외부 알림 주체가 참고할 신호이고, `notification_dispatched`는 항상 `false`다.

## 증거 수집 책임

상태 평가기를 배치하기 전에 아래 생산 경로와 실행 증거를 채워야 한다. 현재 저장소의 구현 상태를 설정 파일 존재만으로 완료로 간주하지 않는다.

| 증거 | 제안 생산 주체 | 현재 운영 연결 증거 |
| --- | --- | --- |
| `worker_heartbeat_at` | worker가 별도 heartbeat 저장소에 기록하고 읽기 전용 collector가 조회 | 컨테이너 내부 기록과 `heartbeat_export`의 호스트 파일 전달 구현. 운영 host 예약 실행과 최근 산출물은 별도 필요 |
| 최신 백업 manifest | 승인된 백업 작업이 원자적으로 생성한 `*.manifest.json` | 수동 백업 코드 존재, 호스트 예약 실행 증거 없음 |
| `ntp` | 호스트 관리자가 만든 `host-ntp-evidence-v1` JSON | `health_evidence` 읽기 경로 구현. 호스트 producer와 최근 운영 산출물은 미연결 |
| `running_jobs` | 읽기 전용 DB 역할로 `ops.jobs`의 `state = 'running'` 조회 | `health_evidence`가 read-only transaction에서 lease 조회. 운영 예약 실행 증거는 없음 |
| `provider_budgets` | 읽기 전용 DB 집계와 승인된 provider 한도 설정 결합 | `health_evidence`가 UTC 일/월 reservation 집계와 설정을 결합. 운영 예약 실행 증거는 없음 |
| 알림 발송 | 종료 코드와 JSON을 소비하는 별도 dispatcher | `health_dispatch` generic HTTPS webhook 구현. 실제 목적지·호스트 예약·수신 성공 증거는 미연결 |

DB collector는 다음 의미를 보존한다.

- `running_jobs`: `job_id`, `lease_started_at`, `lease_until`. 조회 성공 시 결과가 없어도 `[]`을 쓴다.
- `provider_budgets`: provider별 `daily_used_amount`, `daily_limit_amount`, `daily_used_calls`, `daily_limit_calls`, `monthly_used_amount`, `monthly_limit_amount`, `monthly_used_calls`, `monthly_limit_calls`. 사용 금액은 `COALESCE(settled_amount, reserved_amount)`의 합, 호출 수는 reservation 수다.
- 한도는 검증된 운영 설정의 활성 provider와 동일 currency에서 읽는다. 설정에 없는 안전한 provider 이름은 진단에 남기고 예산 전체를 `null`로 닫는다. 자격 증명, API key, DSN은 evidence나 예외에 넣지 않는다.
- `collected_at`이 없거나 잘못되면 DB 두 점검은 `unknown`, 미래 또는 10분 초과이면 `critical`이다.

NTP 증거 파일은 호스트 관리 주체가 다음 계약으로 원자 생성해야 한다. 수집기는 `timedatectl`, `w32tm` 같은 명령을 직접 실행하거나 결과를 추정하지 않는다.

```json
{
  "schema_version": "host-ntp-evidence-v1",
  "observed_at": "2026-09-27T00:00:00+00:00",
  "synchronized": true,
  "offset_ms": 0.5
}
```

## 증거 수집과 판정 예시

DB URL은 CLI 인수에 넣지 않고 환경 변수 **이름**만 전달한다. 해당 계정은 필요한 테이블에 SELECT만 가진 `vlytics_read_api_login` 역할을 사용한다. 운영 설정과 schema는 검토·승인한 동일 revision을 사용한다.

```powershell
python -m vlytics.ops.health_evidence `
  --database-url-env VLYTICS_HEALTH_DATABASE_URL `
  --operational-config C:\ProgramData\Vlytics\operational.toml `
  --operational-schema D:\Vlytics\contracts\config.schema.json `
  --heartbeat C:\ProgramData\Vlytics\health\worker-heartbeat.json `
  --ntp-evidence C:\ProgramData\Vlytics\health\ntp.json `
  --output C:\ProgramData\Vlytics\health\evidence.json

python -m vlytics.ops.health `
  --evidence C:\ProgramData\Vlytics\health\evidence.json `
  --backup-manifest D:\VlyticsBackups\manifests `
  --backup-artifacts D:\VlyticsBackups\dumps
```

```sh
python -m vlytics.ops.health_evidence \
  --database-url-env VLYTICS_HEALTH_DATABASE_URL \
  --operational-config /etc/vlytics/operational.toml \
  --operational-schema /opt/vlytics/contracts/config.schema.json \
  --heartbeat /var/lib/vlytics/health/worker-heartbeat.json \
  --ntp-evidence /var/lib/vlytics/health/ntp.json \
  --output /var/lib/vlytics/health/evidence.json

python -m vlytics.ops.health \
  --evidence /var/lib/vlytics/health/evidence.json \
  --backup-manifest /var/lib/vlytics/backups/manifests \
  --backup-artifacts /var/lib/vlytics/backups/dumps
```

백업 판정기는 최신 `*.manifest.json`에서 `.manifest.json`을 제거한 이름의 dump를 `--backup-artifacts` 디렉터리에서 찾는다. 이 옵션을 생략하면 manifest와 같은 디렉터리에서 찾는다. 최신 manifest가 있어도 dump가 없거나 크기·SHA-256이 다르면 `backup_integrity=critical` 및 종료 코드 3이다. 실제 외부 백업/PITR과 복구 drill 증거는 이 로컬 파일 검사와 별개로 확보한다.

수집 CLI는 일부 producer가 실패해도 `null`과 비밀 없는 진단을 포함한 evidence를 원자 출력하고 `0`으로 종료한다. evidence 파일 자체를 쓰지 못하면 고정된 오류만 출력하고 `3`으로 종료한다. 따라서 수집 직후 판정 CLI를 실행해 `unknown`을 실패로 전파해야 한다. 판정 CLI는 기본적으로 evidence의 `expected_providers`를 사용한다. `--expected-provider`를 하나 이상 주면 명시한 목록을 우선한다.

`--now`은 합성 검증 전용으로 고정 시각을 주입할 때 사용할 수 있다. 운영에서는 생략하여 현재 UTC를 사용한다. 임계값 변경은 CLI 옵션으로 명시하고 변경 승인 기록과 함께 관리한다.

## 예약 템플릿

예약 주기는 5분을 권장한다. 아래는 검토용 템플릿이며 저장소 설치나 문서 추가만으로 호스트에 등록되지 않는다.

### systemd

`/etc/systemd/system/vlytics-health.service` 템플릿:

```ini
[Unit]
Description=Vlytics read-only operations health evaluation
After=network-online.target

[Service]
Type=oneshot
User=vlytics-monitor
WorkingDirectory=/opt/vlytics/backend
ExecStart=/bin/sh -c '/opt/vlytics/backend/.venv/bin/python -m vlytics.ops.health_evidence --database-url-env VLYTICS_HEALTH_DATABASE_URL --operational-config /etc/vlytics/operational.toml --operational-schema /opt/vlytics/contracts/config.schema.json --heartbeat /var/lib/vlytics/health/worker-heartbeat.json --ntp-evidence /var/lib/vlytics/health/ntp.json --output /var/lib/vlytics/health/evidence.json && /opt/vlytics/backend/.venv/bin/python -m vlytics.ops.health --evidence /var/lib/vlytics/health/evidence.json --backup-manifest /var/lib/vlytics/backups/manifests --backup-artifacts /var/lib/vlytics/backups/dumps'
StandardOutput=append:/var/log/vlytics/health.jsonl
StandardError=append:/var/log/vlytics/health-error.log
```

`/etc/systemd/system/vlytics-health.timer` 템플릿:

```ini
[Unit]
Description=Evaluate Vlytics operations health every five minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

등록과 활성화는 운영 변경 승인 후 호스트 관리자가 수행하고, `systemctl list-timers`, 최근 journal, 생성된 JSON의 시각을 운영 증거로 남긴다.

### Windows Task Scheduler

| 항목 | 템플릿 값 |
| --- | --- |
| 프로그램 | `D:\Vlytics\backend\.venv\Scripts\python.exe` |
| 인수 | 승인된 wrapper에서 위 `health_evidence` 명령 성공 직후 `health` 명령 실행 |
| 시작 위치 | `D:\Vlytics\backend` |
| 트리거 | 시작 후 5분, 이후 5분마다 무기한 반복 |
| 계정 | DB/입력 파일은 읽기 전용, evidence/report/dispatch state 디렉터리에만 쓰기 권한이 있는 monitor 계정 |

등록은 운영 변경 승인 후 관리자가 수행한다. 작업의 최근 실행 시각, 마지막 종료 코드, 표준 출력 보존 위치를 운영 증거로 남긴다.

## 합성 dry-run

배포 전에는 실제 provider나 알림 endpoint를 호출하지 않고 임시 evidence, manifest와 대응하는 dump 파일로 CLI를 실행한다. 모든 점검이 최신인 fixture는 종료 코드 `0`과 `notification_dispatched: false`를 반환해야 한다. heartbeat 시각을 임계값 밖으로 옮긴 fixture는 종료 코드 `3`, `worker_heartbeat = critical`, `notification_required: true`, `notification_dispatched: false`를 반환해야 한다.

## 컨테이너 heartbeat 전달

worker 설정의 `VLYTICS_WORKER_HEARTBEAT_PATH`와 export의 `--container-path`를 일치시킨다. 기본 경로는 `/tmp/vlytics-worker-heartbeat.json`이다. 운영 host에서 기존 container runtime 실행 권한을 가진 작업 주체가 아래 명령을 실행한다. monitor 계정에 Docker socket 접근 권한을 추가하는 절차가 아니다.

```sh
python -m vlytics.ops.heartbeat_export \
  --container vlytics-worker-1 \
  --output /var/lib/vlytics/health/worker-heartbeat.json
```

실제 컨테이너 이름은 해당 host에서 확인해 지정한다. Podman은 `--runtime podman`을 사용한다. 도구는 shell 없이 컨테이너의 지정 파일을 최대 16KiB로 읽고 schema·시각·정수 필드를 검사한다. 성공 시 허용 필드만 원자적으로 저장하고 종료 코드 0을 반환한다. 읽기 실패/잘못된 입력은 기존 파일을 unavailable marker로 교체하고 3을 반환하므로 collector가 이전 정상 파일을 계속 읽는 것을 방지한다. 출력 경로 자체에 쓸 수 없는 경우에는 marker도 갱신할 수 없으므로 작업 종료 코드 3을 별도로 감시해야 한다.

CI는 개발 Compose에도 출력 경로를 명시하고, worker 재시작 완료 이후 `completed_at`의 파일을 실제 export한다. 단순히 180초 이내인 재시작 전 파일을 성공으로 인정하지 않는다. host 실운영 증거와 CI 증거는 구분한다.

## 선택적 HTTPS 알림 전달

`health_dispatch`는 `operations-health-report-v1` 파일을 읽는다. 기본값은 dry-run으로 목적지 환경변수를 읽거나 SQLite 상태를 만들거나 HTTP를 보내지 않는다.

```sh
python -m vlytics.ops.health_dispatch \
  --report /var/lib/vlytics/health/report.json \
  --destination-env VLYTICS_ALERT_DESTINATION
```

실제 발송은 `--send`, `--operational-config`, `--state-db`를 함께 지정해야 한다. 운영 설정의 `[alerting]`이 활성화되어 있고 `channel = "generic_webhook_v1"`, `destination_env`가 CLI의 환경변수 이름과 정확히 일치해야 한다. 비활성 설정은 `mode: disabled`로 미전송한다. 예시 production 설정의 placeholder/false를 문서 작성만으로 변경하지 않는다.

목적지 값은 환경변수에만 보관하며 HTTPS URL이어야 한다. TLS 검증을 사용하고 redirect와 ambient proxy 환경변수를 사용하지 않는다. payload는 `operations-health-webhook-v1`의 channel/report 상태/평가 시각/check ID·상태·허용된 evidence 시각/멱등키만 포함한다. 원본 evidence·summary·URL·credential은 전송하지 않는다. 이 JSON 계약을 받는 endpoint가 필요하며 Slack/Teams 등의 다른 webhook payload와 직접 호환된다고 가정하지 않는다.

- 정상 check는 발송하지 않으며 복구 상태를 저장한다. 비정상 check별 발송이므로 한 보고서에서 최대 6회 요청한다.
- SQLite state는 목적지 URL hash와 check ID로 분리한다. 상태 디렉터리는 monitor 전용 로컬 영속 저장소에 두고 프로세스 재시작 때 유지한다.
- 같은 장애 회차의 재전송은 같은 `Idempotency-Key`를 쓴다. 복구 후 재발과 stable observation 변경은 새로운 generation/키로 구분한다. A→B→A 상태 변화도 이전 키를 재사용하지 않는다. 오래된 보고서는 최신 상태를 덮어쓰지 않는다.
- HTTP 2xx만 성공으로 기록한다. 전송 실패는 종료 코드 3이며 health 보고서를 정상으로 바꾸지 않는다.
- 원격 2xx 직후 로컬 기록 실패나 timeout은 수신 여부가 불확실할 수 있다. 재전송을 수신처에서도 키로 중복 제거해야 하며 exactly-once 전달을 보장하지 않는다.

### 평가 실패 코드와 발송 실행의 연결

비정상 health는 종료 코드 2/3이므로 `health && health_dispatch`로 연결하면 정작 필요한 알림이 실행되지 않는다. 수집 성공 후 평가 JSON을 별도 파일로 보존하고, 평가 코드 0/2/3 모두 dispatcher에 전달한 뒤 두 종료 코드를 각각 기록한다. 아래는 Bash wrapper의 평가/발송 구간 예시다. 실제 목적지로의 실행은 승인한 운영 host 설정으로만 수행한다.

```sh
health_status=0
python -m vlytics.ops.health \
  --evidence "$health_dir/evidence.json" \
  --backup-manifest "$backup_manifest_dir" \
  --backup-artifacts "$backup_artifact_dir" > "$health_dir/report.next.json" || health_status=$?
case "$health_status" in 0|2|3) ;; *) exit "$health_status" ;; esac
mv -- "$health_dir/report.next.json" "$health_dir/report.json"
dispatch_status=0
python -m vlytics.ops.health_dispatch \
  --report "$health_dir/report.json" \
  --destination-env VLYTICS_ALERT_DESTINATION \
  --operational-config "$operational_config" \
  --state-db "$health_dir/dispatch.sqlite3" \
  --send > "$health_dir/dispatch-result.json" || dispatch_status=$?
if test "$dispatch_status" -ne 0; then exit "$dispatch_status"; fi
exit "$health_status"
```

host wrapper에는 중복 실행 잠금과 실행별 임시 파일을 적용한다. 위 systemd/Task Scheduler 템플릿은 기존 수집·판정용이며 실제 발송을 켜려면 검토한 wrapper를 등록해야 한다. Windows PowerShell 5.1의 기본 `>` 출력은 UTF-16일 수 있으므로 report 파일은 명시적인 UTF-8로 기록한다. dispatcher 성공 코드 0은 전송 작업의 성공일 뿐 원래 health 상태가 정상이라는 뜻이 아니다.

합성 검증은 실제 collector/evaluator가 만든 unknown 보고서를 dispatcher dry-run과 MockTransport로 소비하고 원본 보고서 불변을 확인한다. 호스트 NTP evidence producer, 예약 등록, 실제 알림 수신, 외부 backup/PITR 증거는 아직 별도 운영 작업이다.
