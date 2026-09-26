# 운영 상태 감시 자동화 템플릿

이 문서는 `python -m vlytics.ops.health`를 주기적으로 실행하기 위한 운영 설계와 템플릿을 정의한다. CLI는 이미 수집된 JSON 증거와 백업 manifest를 **읽고 판정만** 한다. production worker heartbeat는 별도 `vlytics.ops.heartbeat` 모듈로 기록·검사한다. 알림 전송, 데이터베이스 갱신, worker heartbeat 기록, 호스트 예약 등록은 수행하지 않는다.

## 판정 계약

입력 증거는 `operations-health-evidence-v1`이어야 한다. 모든 시각은 시간대가 포함된 ISO 8601 값이어야 한다.

```json
{
  "schema_version": "operations-health-evidence-v1",
  "worker_heartbeat_at": null,
  "ntp": null,
  "running_jobs": null,
  "provider_budgets": null
}
```

`null`은 해당 증거 수집 경로가 아직 연결되지 않았다는 뜻이다. 빈 배열은 수집이 성공했고 현재 행이 없다는 뜻이므로 서로 바꾸어 쓰지 않는다. CLI는 누락되거나 형식이 잘못된 증거를 `unknown`으로 판정하고 실패 상태로 종료한다.

기본 임계값은 다음과 같다.

| 점검 | 정상 기준 | 비정상 판정 |
| --- | --- | --- |
| worker heartbeat | 3분 이내 | 누락 `unknown`, 미래 또는 3분 초과 `critical` |
| 백업 manifest | 25시간 이내, schema `2.0`, owner/ACL 보존 및 무결성 필드 존재 | 누락·불완전 `unknown`, 미래·노후·ACL 미보존 `critical` |
| NTP | 관측 10분 이내, 동기화됨, 절대 offset 1,000 ms 이하 | 누락 `unknown`, 미래·노후·비동기·offset 초과 `critical` |
| 실행 중 job | 시작 10분 이내이고 lease가 유효함 | 누락 `unknown`, 행 불완전 `unknown`, 노후·만료 `critical` |
| provider 예산 | 모든 구성 provider의 일/월 금액 및 호출 수가 존재함 | 누락 `unknown`, 80% 이상 `warning`, 100% 이상 `critical` |

전체 상태 우선순위는 `critical`, `unknown`, `warning`, `ok` 순이다. 종료 코드는 정상 `0`, 경고 `2`, 위험 또는 증거 불명 `3`이다. 출력의 `notification_required`는 외부 알림 주체가 참고할 신호이고, `notification_dispatched`는 항상 `false`다.

## 증거 수집 책임

상태 평가기를 배치하기 전에 아래 생산 경로와 실행 증거를 채워야 한다. 현재 저장소의 구현 상태를 설정 파일 존재만으로 완료로 간주하지 않는다.

| 증거 | 제안 생산 주체 | 현재 운영 연결 증거 |
| --- | --- | --- |
| `worker_heartbeat_at` | worker가 별도 heartbeat 저장소에 기록하고 읽기 전용 collector가 조회 | production worker가 성공한 poll 뒤 `/tmp/vlytics-worker-heartbeat.json`에 원자 기록. 컨테이너 healthcheck는 300초 경과를 실패로 판정. 호스트 collector 연결은 대기 |
| 최신 백업 manifest | 승인된 백업 작업이 원자적으로 생성한 `*.manifest.json` | 수동 백업 코드 존재, 호스트 예약 실행 증거 없음 |
| `ntp` | 호스트의 `timedatectl` 또는 Windows Time 상태를 읽는 collector | 미연결 — 예약 실행 및 최근 산출물 없음 |
| `running_jobs` | 읽기 전용 DB 역할로 `ops.jobs`의 `state = 'running'` 조회 | collector 미연결 |
| `provider_budgets` | 읽기 전용 DB 집계와 승인된 provider 한도 설정 결합 | collector 미연결 |
| 알림 발송 | 종료 코드와 JSON을 소비하는 별도 dispatcher | 미연결 — 이 CLI는 발송하지 않음 |

DB collector는 최소한 다음 의미를 보존해야 한다.

- `running_jobs`: `job_id`, `lease_started_at`, `lease_until`. 조회 성공 시 결과가 없어도 `[]`을 쓴다.
- `provider_budgets`: provider별 `daily_used_amount`, `daily_limit_amount`, `daily_used_calls`, `daily_limit_calls`, `monthly_used_amount`, `monthly_limit_amount`, `monthly_used_calls`, `monthly_limit_calls`. 사용 금액은 `COALESCE(settled_amount, reserved_amount)`의 합, 호출 수는 reservation 수다.
- 한도는 현재 활성 provider registry의 동일 currency 설정에서 읽는다. 증거 JSON에 자격 증명, API key, DSN을 넣지 않는다.

## 실행 예시

backend 가상환경에서 다음과 같이 판정한다.

```powershell
python -m vlytics.ops.health `
  --evidence C:\ProgramData\Vlytics\health\evidence.json `
  --backup-manifest D:\VlyticsBackups\manifests `
  --expected-provider openai `
  --expected-provider anthropic `
  --expected-provider google
```

```sh
python -m vlytics.ops.health \
  --evidence /var/lib/vlytics/health/evidence.json \
  --backup-manifest /var/lib/vlytics/backups/manifests \
  --expected-provider openai \
  --expected-provider anthropic \
  --expected-provider google
```

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
ExecStart=/opt/vlytics/backend/.venv/bin/python -m vlytics.ops.health --evidence /var/lib/vlytics/health/evidence.json --backup-manifest /var/lib/vlytics/backups/manifests --expected-provider openai --expected-provider anthropic --expected-provider google
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
| 인수 | `-m vlytics.ops.health --evidence C:\ProgramData\Vlytics\health\evidence.json --backup-manifest D:\VlyticsBackups\manifests --expected-provider openai --expected-provider anthropic --expected-provider google` |
| 시작 위치 | `D:\Vlytics\backend` |
| 트리거 | 시작 후 5분, 이후 5분마다 무기한 반복 |
| 계정 | DB와 파일에 읽기 권한만 있는 전용 monitor 계정 |

등록은 운영 변경 승인 후 관리자가 수행한다. 작업의 최근 실행 시각, 마지막 종료 코드, 표준 출력 보존 위치를 운영 증거로 남긴다.

## 합성 dry-run

배포 전에는 실제 provider나 알림 endpoint를 호출하지 않고 임시 evidence와 manifest로 CLI를 실행한다. 모든 점검이 최신인 fixture는 종료 코드 `0`과 `notification_dispatched: false`를 반환해야 한다. heartbeat 시각을 임계값 밖으로 옮긴 fixture는 종료 코드 `3`, `worker_heartbeat = critical`, `notification_required: true`, `notification_dispatched: false`를 반환해야 한다.

별도 dispatcher를 연결할 때는 CLI JSON을 stdin 또는 파일로 소비하게 하고, 합성 endpoint로 다음을 확인한다.

1. `0`에서는 발송하지 않는다.
2. `2`와 `3`에서만 한 번 발송한다.
3. 같은 check와 evidence 시각은 중복 억제한다.
4. dispatcher 실패가 health JSON을 정상으로 바꾸지 않는다.
5. 합성 검증이 끝날 때까지 실제 수신자와 운영 webhook은 비워 둔다.

현재 이 저장소에는 dispatcher와 호스트 예약 등록이 없으므로 실제 알림 도달과 주기 실행은 검증되지 않은 운영 증거로 남는다.
