# 운영 설정 계약

작성일: 2026-09-20 · 대상 schema: `config.schema.json` 1.0 · 범위: TASK-002

이 문서는 UC-003·006·007에서 선택한 상시 모놀리스, 독립 GPT/Claude/Gemini, 엄격 T-60 정책을 실제 설정 경계로 옮긴다. 호스트 구매, 유료 모델 활성화, 사용자 예산 결정은 수행하지 않았다. 미결정 값은 `__REQUIRED__`와 0으로 안전하게 표현하며, 해당 기능을 활성화하면 schema와 시작 전 의미 검증에서 거부한다.

실행 설정은 `config/example.toml`을 복사해 외부 배포 저장소에서 관리한다. 저장소의 예시는 의도적으로 실행 불가능하다. `live_operations_enabled = false`인 상태에서는 API 계약·fake Provider·fake clock·합성 fixture만 허용하고, live worker는 시작하지 않는다.

## OP 장부

| ID | 상태 | 현재 값·미결정 항목 | 출처 | 결정·계약일 | 검증 방법 | 활성화 게이트 |
|---|---|---|---|---|---|---|
| OP-002 | 미결정, 실운영 배포 차단 | Python/FastAPI·PostgreSQL·React/TypeScript 상시 모놀리스는 확정. 호스트 종류/업체/지역, 월 비용 상한·통화, 운영자 인증 방식, 백업 주기·보관·RPO/RTO, 알림 채널은 미결정 | UC-003 A, `architecture.md` 기술 스택·외부 의존·보안 | 스택 2026-09-19, 나머지 미결정 | schema 활성화 조건, private 접근 확인, 시계 동기화, 백업 복구 drill, 알림 시험 | 모든 필수값과 secret 참조가 유효하고 복구·알림 시험을 통과하기 전 배포 거부 |
| OP-003 | 미결정, 실제 AI 호출 차단 | OpenAI/Anthropic/Google의 model ID, 버전 고정 방식, 일·월 예산과 통화, 경기당/일/월 호출 한도, 입·출력 token 한도 미결정 | UC-006 A, `architecture.md` 인터페이스·오류 처리 | 독립 3 Provider 2026-09-19, 실제 값 미결정 | schema 활성화 조건, credential 존재 확인, 소량 smoke test에서 requested/resolved ID·usage·비용 기록, alias drift 검사 | 세 Provider 모두 값을 채우고 예산 소유자가 승인하기 전 유료 호출 거부 |
| OP-004 | 계약값 고정, live 통합 검증 대기 | T-60, 초기 시작 허용 30초, 완료 grace 300초, 호출당 timeout 60초, 최대 3회, 5초부터 2배 backoff(최대 30초), T-10 재시도 금지. 결과 5분 polling, 30분 안정화, 6시간 간격으로 7일 정정 재조회 | UC-007 A, `architecture.md` 시점·스케줄 계약, TASK-002 구현 기준 | 2026-09-20 | schema 상수, deadline 산술 검사, TASK-012 fake clock·late completion·재편성, TASK-017 replay | fake clock과 실제 경기 전 dry run 전 live scheduler 활성화 금지 |

OP-001의 이용 범위와 요청 속도는 TASK-001 소유다. 따라서 예시는 bulk 수집을 끄고 `__REQUIRED_BY_OP_001__`, 0 request rate, 0 concurrency를 둔다. 이 값은 TASK-001 결과 없이 추정하거나 활성화하지 않는다.

## 호스트·접근 후보

비용과 공급업체를 확인하지 않았으므로 아래는 선택 가능한 운영 형태다. 특정 상품 구매나 비용 가정을 뜻하지 않는다.

| `host_class` | 구성 | 운영상 확인할 사항 |
|---|---|---|
| `managed_vm_and_database` | 상시 API/worker VM과 관리형 PostgreSQL | 네트워크 격리, 관리형 backup의 실제 RPO/RTO, DB egress와 월 상한 |
| `private_single_vm` | private VM 한 대에 API/worker/PostgreSQL | host 장애가 전체 장애가 되므로 외부 backup, 복구 시간, 디스크 용량과 patch 책임 |
| `dedicated_private_machine` | 전용 상시 장비와 PostgreSQL | 절전 차단, 전원·회선 장애, 원격 복구, off-host backup과 시계 동기화 |

운영 접근은 `loopback` 또는 `private_network`만 schema가 허용한다. 운영자 명령에는 별도 인증을 적용하고, 인증 방식은 private network identity, reverse proxy OIDC, mutual TLS 중 배포 환경에서 검증된 방식을 명시한다. public ingress는 이 계약의 선택지가 아니며 별도 결정 없이는 열지 않는다.

Raw는 초기에는 private PostgreSQL에 원문·hash를 저장한다. 크기 때문에 object storage로 옮길 때도 private immutable key, DB hash, 복구 절차를 함께 검증한다. `.env`, DB dump, Raw JSON, Provider 원문은 Git에 넣지 않는다.

백업 활성화에는 strategy, 간격, 보관 기간, RPO/RTO를 모두 양수로 채워야 한다. 백업 파일 존재만으로 완료 처리하지 않고 격리된 환경에서 복구 후 schema version·row count·대표 hash를 확인한다. 복구 시험 시각과 결과는 운영 audit에 기록한다.

알림 활성화에는 채널과 목적지 secret 참조가 필요하다. 최소 알림 대상은 clock skew, T-60 초기 실행 지연, deadline 만료, Provider 연속 실패·예산 소진, 수집 coverage 저하, final 결과 장기 대기, backup/복구 실패다.

## Provider·비용 계약

UC-006 A에 따라 OpenAI, Anthropic, Google은 같은 snapshot으로 독립 실행한다. 한 Provider의 실패 때문에 성공한 Provider를 다시 호출하지 않는다. model ID와 version policy는 Provider별로 채우며 alias만 제공되는 경우 `verify_resolved_model_id`를 사용해 매 호출의 resolved ID를 저장한다. 기대 version과 달라지면 결과를 같은 cohort에 넣지 않고 `version_unverified` 또는 alias drift 오류로 격리한다.

활성 Provider에는 다음 값이 모두 필요하다.

- 실제 `model_id`와 `pinned_model_version`
- `immutable_model_id` 또는 `verify_resolved_model_id` version policy
- 일·월 비용 상한과 ISO 4217 통화 코드
- 경기당, 일, 월 호출 한도
- 호출별 최대 input/output token
- API key 값이 들어 있는 환경변수 이름

일 예산은 월 예산보다 클 수 없고, 일 호출 한도는 월 호출 한도보다 클 수 없다. 이는 JSON Schema로 표현하기 어려운 교차 필드 조건이므로 시작 전 의미 검증에서 확인한다. 설정 누락은 프로세스 시작 실패다. 실행 중 예산을 소진하면 해당 시도를 `budget_skipped`로 남기고 통계 예측과 다른 Provider는 계속한다.

Production 전송은 공급자별 공식 HTTPS endpoint에 고정한다. OpenAI는 Responses API `https://api.openai.com/v1/responses`, Anthropic은 Messages API `https://api.anthropic.com/v1/messages`, Google은 `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`만 사용한다. Google key는 URL query가 아니라 `x-goog-api-key` 헤더로 전달한다. endpoint override와 redirect는 허용하지 않으며, 환경 proxy도 읽지 않는다. configured model ID는 OpenAI·Anthropic 요청 본문과 일치해야 하고 Google model은 하나의 URL path segment로 encode한다.

모든 응답은 1 MiB 기본 상한 안에서 streaming read하며 호출 wall timeout이 connect/read timeout의 최종 상한이다. 429와 다른 비정상 status에서는 status, 안전한 error code, request ID, `Retry-After`만 구조화하고 provider message·본문·prompt·key를 예외나 로그에 보존하지 않는다. `build_live_prediction_providers`는 검증된 `LiveProviderPlan`과 환경 secret으로 세 고정 transport만 만든다. `FakeProviderTransport`는 계약·replay 테스트 전용이며 production factory에 주입할 수 없다.

구현 계약의 기준 문서는 [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses/create), [Anthropic Messages API](https://docs.anthropic.com/en/api/messages), [Gemini generateContent](https://ai.google.dev/api/generate-content), [Gemini API 인증](https://ai.google.dev/api)이다. 이 구현은 offline HTTP mock으로 검증한다. OP-003의 model/version·가격·예산·secret 확정과 소량 유료 smoke test가 끝나기 전에는 실제 요청을 계속 차단한다.

## T-60과 deadline

예정 시작을 `T`, `cutoff_at = T - 60분`으로 둔다. input snapshot은 항상 이 cutoff를 기록하며 재시도해도 바꾸지 않는다.

초기 시도는 `cutoff_at`부터 30초 이내에 시작해야 주 평가 후보가 된다. 이후 재시도는 같은 snapshot으로만 수행하며, 호출당 timeout은 60초이고 총 3회까지 허용한다. 재시도 전 대기는 5초, 10초이며 다음 값은 20초지만 세 번째 시도 뒤 추가 재시도는 없다. 이 구성의 정적 최대 요청·대기 시간은 `3 × 60 + 5 + 10 = 195초`로 300초 grace보다 105초 작다. 직렬화·DB 처리·스케줄 지연을 포함한 실제 완료가 deadline을 넘으면 이 계산과 무관하게 거부한다.

각 job의 deadline은 다음 식으로 계산한다.

```text
deadline_at = min(
  cutoff_at + 300 seconds,
  scheduled_start_at,
  actual_start_at if known
)
```

정상 일정에서는 deadline이 T-55이므로 T-10까지 재시도하는 경로가 없다. 완료 적격 조건은 `completed_at < deadline_at`이다. `completed_at >= scheduled_start_at` 또는 실제 시작이 확인된 경우 `completed_at >= actual_start_at`인 응답은 요청 시작 시각과 관계없이 `late_rejected`다. 실제 시작을 나중에 확인했는데 더 빨랐다면 기존 prediction 본문은 수정하지 않고 lifecycle event로 적격성을 철회한다. 지연 시작 사실로 이미 정한 deadline을 연장하지 않는다.

초기 시도가 30초를 넘었거나 grace 밖에서 끝났지만 경기 전인 응답도 진단용 attempt로만 보존하고 주 성능 cohort에서 제외한다. timeout 응답이 뒤늦게 도착해도 대표 prediction을 새로 채택하지 않는다. 일정 변경은 새 schedule revision과 새 T-60 job을 만들며 과거 cutoff를 소급 생성하지 않는다.

의미 검증기는 다음을 추가로 강제한다.

1. `target_cutoff_minutes == 60`, `allow_t10_retry == false`.
2. `initial_start_tolerance_seconds < completion_grace_seconds < target_cutoff_minutes × 60`.
3. 모든 timeout과 retry delay의 정적 상한이 completion grace 이내다.
4. deadline 식이 scheduled start와 알려진 actual start보다 항상 이르다.
5. 동일 schedule revision·snapshot·variant의 대표 prediction은 하나뿐이다.

## 결과 안정화와 정정

경기가 종료되면 5분 간격으로 provisional 결과를 조회한다. 세트 합·누적 점수·승자·원문 상태 검증을 통과한 동일 결과가 30분 동안 변하지 않아야 안정화 후보가 된다. 원천이 명시적으로 final을 제공하더라도 새 immutable result revision으로 저장한다.

첫 final 관측 뒤 7일 동안 6시간 간격으로 정정을 재조회한다. 정정 발견 시 기존 result와 evaluation을 바꾸지 않고 새 revision과 evaluation을 생성한다. 7일 이후에는 정기 reconciliation 또는 수동 조사에서 발견한 정정도 새 revision으로 수용하지만, OP-004 자동 재조회 SLA에는 포함하지 않는다.

## Secret 주입

TOML에는 secret 값 대신 환경변수 이름만 둔다. 예시는 다음 참조만 제공한다.

| 용도 | 환경변수 이름 |
|---|---|
| PostgreSQL 연결 | `VLYTICS_DATABASE_URL` |
| 운영자 인증 secret | `VLYTICS_OPERATOR_AUTH_SECRET` |
| backup 자격 증명 | `VLYTICS_BACKUP_CREDENTIAL` |
| 알림 목적지/자격 증명 | `VLYTICS_ALERT_DESTINATION` |
| Provider key | `VLYTICS_OPENAI_API_KEY`, `VLYTICS_ANTHROPIC_API_KEY`, `VLYTICS_GOOGLE_API_KEY` |

값은 배포 환경의 secret store에서 런타임에 주입한다. 시작 전 존재 여부만 확인하고 값 자체를 로그, config hash, 오류 메시지, audit event에 기록하지 않는다. config hash는 secret을 resolve하기 전의 비밀값 없는 정규화 설정으로 계산한다.

## 검증과 fail-fast

1. TOML parser로 문법과 중복 key를 검사한다.
2. 파싱된 객체를 `contracts/config.schema.json` draft 2020-12로 검증한다.
3. 위의 교차 필드 예산·retry·deadline 규칙을 의미 검증한다.
4. 활성 기능이 참조하는 환경변수의 존재를 검사하되 값을 출력하지 않는다.
5. live 시작 전에 DB 연결/권한, UTC clock, backup 복구, 알림 전송, Provider 소량 smoke test를 실행한다.

backend의 `vlytics.config.load_operational_config`가 1~4를 API 생성과 worker 시작 전에
실행한다. 기본 경로는 저장소의 예시와 schema이며, 배포에서는
`VLYTICS_OPERATIONAL_CONFIG_PATH`와 `VLYTICS_OPERATIONAL_SCHEMA_PATH`로 읽기 전용 mount를
지정한다. `example`/`development` 환경의 비활성 설정은 합성 개발을 위해 기동할 수 있지만,
`staging`/`production`은 `live_operations_enabled = true`가 아니면 API와 worker 모두 시작을
거부한다.

다음 상태에서는 해당 live 프로세스를 즉시 종료한다: 활성 구역의 placeholder 또는 0 한도, 미설정 host/auth/비용, 누락 secret, 유효하지 않은 budget 관계, 계산 불가능하거나 경기 시작을 넘는 deadline, OP-001 없이 켠 bulk 수집. 설정 오류를 기본값으로 보정하지 않는다.

합성 검증은 `live_operations_enabled = false`, 모든 유료/외부 기능 disabled 상태에서 계속 가능하다. 이 게이트는 프로젝트 scaffold, 계약 테스트, fake Provider, fake clock 작업을 막지 않는다.
