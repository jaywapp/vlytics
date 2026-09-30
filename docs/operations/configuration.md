# 운영 설정 계약

작성일: 2026-09-20 · 대상 schema: `config.schema.json` 1.0 · 범위: TASK-002

이 문서는 UC-003·006·007에서 선택한 상시 모놀리스, Provider adapter, 엄격 T-60 정책을 실제 설정 경계로 옮긴다. 2026-09-30 사용자는 `deployment.profile = "personal_home"`, `host_class = "dedicated_private_machine"`, OpenAI만 월 10,000원(KRW), 무기한 데이터 보관, backup/PITR·외부 알림 없음으로 운영 시작을 승인했다. 미결정 값은 `__REQUIRED__`와 0으로 안전하게 표현하며, 활성 기능에 필요한 값만 schema와 시작 전 의미 검증에서 요구한다.

표준 설정은 `config/example.toml`과 기존 production 산출물을 유지한다. 홈 운영은 별도 `infra/operational.home.example.toml`, `infra/.env.home.example`, `infra/compose.home.yaml`, `config/variants.home.toml`을 사용한다. 저장소의 예시는 의도적으로 실행 불가능하다. `live_operations_enabled = false`인 상태에서는 API 계약·fake Provider·fake clock·합성 fixture만 허용하고, live worker는 시작하지 않는다.

## OP 장부

| ID | 상태 | 현재 값·미결정 항목 | 출처 | 결정·계약일 | 검증 방법 | 활성화 게이트 |
|---|---|---|---|---|---|---|
| OP-002 | 사용자 결정 완료, 홈 서버 실측 대기 | `personal_home` + `dedicated_private_machine`, LAN/SSH 전용, 유료 호스트 없음, 데이터 무기한 보관, backup/PITR·외부 알림 비활성 및 데이터 유실 수용 | UC-003 A, 2026-09-30 사용자 결정 | 2026-09-30 | private 접근, 시계 동기화, Docker/Compose, 디스크, 재시작, 인증 경계 실측 | 홈 프로필 필수값·secret과 실제 host smoke가 유효해야 함. backup/PITR·알림 시험은 이 프로필의 게이트가 아님 |
| OP-003 | 사용자 범위·예산 결정 완료, 실제 값 검증 대기 | OpenAI만 활성화, 월 10,000원(KRW). Anthropic·Google 비활성. 실제 계정 접근·응답 model ID·운영 시점 가격·환율과 호출/token 한도는 소량 검증으로 확정 | UC-006 A, 2026-09-30 사용자 결정 | 2026-09-30 | 선택 Provider credential, requested/resolved ID·usage·원 통화 가격·환율·KRW 환산 비용, alias drift 검사 | 활성 Provider만 필수. 월 KRW cap과 명시적 통화 환산이 유효하고 소량 smoke를 통과해야 실제 호출 허용 |
| OP-004 | 계약값 고정, live 통합 검증 대기 | T-60, 초기 시작 허용 30초, 완료 grace 300초, 호출당 timeout 60초, 최대 3회, 5초부터 2배 backoff(최대 30초), T-10 재시도 금지. 결과 5분 polling, 30분 안정화, 6시간 간격으로 7일 정정 재조회 | UC-007 A, `architecture.md` 시점·스케줄 계약, TASK-002 구현 기준 | 2026-09-20 | schema 상수, deadline 산술 검사, TASK-012 fake clock·late completion·재편성, TASK-017 replay | fake clock과 실제 경기 전 dry run 전 live scheduler 활성화 금지 |

OP-001의 이용 범위와 요청 속도는 TASK-001 소유다. 따라서 예시는 bulk 수집을 끄고 `__REQUIRED_BY_OP_001__`, 0 request rate, 0 concurrency를 둔다. 이 값은 TASK-001 결과 없이 추정하거나 활성화하지 않는다.

## 호스트·접근 후보

아래 형태 중 홈 운영에는 `dedicated_private_machine`을 선택했다. 나머지는 향후 표준/공개 배포를 검토할 때의 후보로 보존한다.

| `host_class` | 구성 | 운영상 확인할 사항 |
|---|---|---|
| `managed_vm_and_database` | 상시 API/worker VM과 관리형 PostgreSQL | 네트워크 격리, 관리형 backup의 실제 RPO/RTO, DB egress와 월 상한 |
| `private_single_vm` | private VM 한 대에 API/worker/PostgreSQL | host 장애가 전체 장애가 되므로 외부 backup, 복구 시간, 디스크 용량과 patch 책임 |
| `dedicated_private_machine` | 전용 상시 장비와 PostgreSQL | **홈 프로필 선택값.** 절전 차단, 전원·회선 장애, LAN/SSH 격리, 디스크와 시계 동기화. 사용자가 유실 위험을 수용했으므로 off-host backup은 선택 사항 |

### 단일 호스트 비용·운영 판단 자료 (2026-09-27)

현재 production Compose는 PostgreSQL을 같은 호스트에서 실행하므로 `private_single_vm`과 `dedicated_private_machine`을 호스트 구성 관점에서 비교할 수 있다. 두 형태 모두 실제 배포 적합성은 미검증이다. 아래 가격은 공급업체의 공개 목록 가격이며, 용량 적합성이나 월 총비용을 검증한 값은 아니다.

| 후보 | 확인된 비용 | 운영 전 확인할 조건 |
|---|---|---|
| 보유한 경우의 전용 상시 장비 | **홈 프로필 선택값.** 추가 VM 임대료 없음. 전력·회선·장비 비용과 가용성은 미확인 | 24시간 전원·회선, Docker/Compose, UTC 시계 동기화, LAN/SSH 격리와 디스크. backup/PITR 없음에 따른 유실 위험은 사용자 수용 |
| AWS Lightsail Linux VM, 서울 `ap-northeast-2`, public IPv4, 2 vCPU·4 GB RAM·80 GB SSD | [목록 가격 월 USD 24](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html). 서울 [리전 지원](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html) 확인 | 4 GB 부하·디스크 시험, firewall과 loopback/private 접근 검증, OS patch, 외부 암호화 backup과 WAL/PITR 복구 시험. Snapshot·backup 목적지·초과 전송·세금·유료 Provider 비용은 별도 확인 |

VM snapshot만으로 PostgreSQL의 목표 시각 복구를 통과한 것으로 간주하지 않는다. 관리형 DB는 [Lightsail의 최근 7일 PITR](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-creating-a-database-from-point-in-time-backup.html)이 후보지만, 현재 Compose의 로컬 PostgreSQL을 교체해야 하고 DB 버전·권한·마이그레이션 호환성을 아직 검증하지 않았다. 이 문단은 2026-09-27 후보 조사 기록이다. 2026-09-30 결정으로 홈 프로필의 사용자 선택은 닫혔고, 실제 전용 장비의 LAN/SSH·시계·Docker/Compose·용량·인증 검증만 남았다. 표준/공개 프로필에서는 별도로 host 비용, 외부 backup, RPO/RTO, restore drill과 알림 목적지를 결정한다.

운영 접근은 `loopback` 또는 `private_network`만 schema가 허용한다. 운영자 명령에는 별도 인증을 적용하고, 인증 방식은 private network identity, reverse proxy OIDC, mutual TLS 중 배포 환경에서 검증된 방식을 명시한다. public ingress는 이 계약의 선택지가 아니며 별도 결정 없이는 열지 않는다.

### 선택된 운영자 접속 경로 (2026-09-28)

사용자 결정에 따라 운영자 PC의 SSH 로컬 포트 전달을 사용한다. production Compose의 전용 `operator_ingress`만 호스트 `127.0.0.1:${WEB_PORT:-8080}`에 게시하며 내부 `frontend`로 전달한다. `frontend`·API·DB·worker는 `private` 내부망에만 연결한다. 실제 서버·SSH 계정과 키 발급/폐기 절차가 정해지면 아래 자리표시자를 실제 값으로 교체하고, 운영자 인증 401/403/200과 연결 종료 후 접속 차단을 운영 호스트에서 검증한다.

```text
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 <ssh-user>@<host>
```

`worker`는 `private` 내부망에만 남고 Provider별 relay만 전용 외부망에 가입한다. 사용자의 직접 승인에 따라 [Provider egress 구현 기록](openai-egress-proposal.md)의 OpenAI 배선이 홈 Compose에 적용됐다. 현재 활성 집합은 OpenAI 하나이며 worker는 `VLYTICS_OPENAI_PROXY_URL=http://openai_egress:8081`, `openai_egress`는 `VLYTICS_EGRESS_PROVIDER=openai`, 전용 `openai_access`를 사용한다. relay는 TLS-opaque CONNECT로 정확한 `api.openai.com:443`만 허용하고 host port를 게시하지 않는다. 저장소 구현 완료는 실제 키·호출·홈 서버 배포 성공 증거가 아니다. KOVO egress는 계속 없다.

generic relay와 transport는 향후 `anthropic_egress`/`anthropic_access`/`VLYTICS_ANTHROPIC_PROXY_URL`, `google_egress`/`google_access`/`VLYTICS_GOOGLE_PROXY_URL`을 지원하지만 현재 선언·활성화하지 않는다. 공용 `ProviderEgress.ps1`은 config enabled set과 relay selector·service·key·고정 proxy·전용 network·worker health dependency의 exact subset을 강제하며 비활성 Provider 흔적을 거부한다. Anthropic·Gemini 전환 또는 병행에는 새 사용자 결정, 해당 key·budget·model/version·registry hash·fresh evidence가 필요하고 활성 Provider 전체 registry 예산 합계는 월 10,000원(KRW) 안에 있어야 한다.


Raw는 초기에는 private PostgreSQL에 원문·hash를 저장한다. 크기 때문에 object storage로 옮길 때도 private immutable key, DB hash, 복구 절차를 함께 검증한다. `.env`, DB dump, Raw JSON, Provider 원문은 Git에 넣지 않는다.

홈 프로필은 운영 데이터의 삭제 정책을 두지 않아 무기한 보관을 의도하지만, backup/PITR를 활성화하지 않으며 장비 장애 시 일부 또는 전체 데이터가 유실될 수 있다. backup이 비활성인 홈 프로필에는 strategy·RPO/RTO·backup credential·복구 시험을 요구하지 않는다. backup을 켜거나 표준/공개 프로필로 전환하면 strategy, 간격, 보관 기간, RPO/RTO를 모두 채우고 격리 복구를 검증한다.

홈 프로필은 외부 알림을 활성화하지 않고 운영자가 operations 화면과 로그를 직접 확인한다. 따라서 alert destination은 홈 프로필 필수값이 아니다. 알림을 켜거나 표준/공개 프로필로 전환하면 채널과 목적지 secret 참조를 요구하고 clock skew, deadline, Provider·예산, coverage, final 결과, 활성화된 backup/복구 상태를 시험한다.

## Provider·비용 계약

Provider adapter는 OpenAI, Anthropic, Google을 지원한다. 공용 standard/공개 프로필은 기존 세 Provider 요구사항을 유지한다. `personal_home` 분기에서는 명시적으로 선택한 Provider 집합을 허용하며 현재 선택은 OpenAI 하나다. 한 Provider의 실패 때문에 성공한 Provider를 다시 호출하지 않는다. model ID와 version policy는 활성 Provider별로 채우며 alias만 제공되는 경우 `verify_resolved_model_id`를 사용해 매 호출의 resolved ID를 저장한다. 기대 version과 달라지면 결과를 같은 cohort에 넣지 않고 `version_unverified` 또는 alias drift 오류로 격리한다.

활성 Provider에는 다음 값이 모두 필요하다.

- 실제 `model_id`와 `pinned_model_version`
- `immutable_model_id` 또는 `verify_resolved_model_id` version policy
- 일·월 비용 상한과 ISO 4217 통화 코드
- `ai.budget_currency = "KRW"`와 가격 통화를 KRW에 연결하는 양수 `ai.pricing_to_budget_rate`
- 경기당, 일, 월 호출 한도
- 호출별 최대 input/output token
- API key 값이 들어 있는 환경변수 이름

홈 예시는 실제 smoke 전 `pinned_model_version = "__REQUIRED_AFTER_MODEL_SMOKE__"`, `registry.openai.op003_resolved = false`로 둔다. `verify_resolved_model_id` 정책에서 요청 alias `gpt-6-sol`과 응답 model ID가 동일하면 고정된 하위 version을 검증한 것이 아니므로 결과를 `VERSION_UNVERIFIED`로 격리한다. 문서나 설정 작성자가 임의 version을 만들어 채우지 않는다. 실제 smoke에서도 검증 가능한 resolved version을 얻지 못하면 OpenAI 결과를 strict cohort에 넣지 않고 OP-003을 미해결로 유지한다.

`activation.provider_registry_sha256`은 최종 `config/variants.home.toml`의 **원문 bytes SHA-256**이어야 한다. 홈 live plan은 로드한 registry 원문의 hash와 이 값이 정확히 일치할 때만 생성한다. 예시는 주석 placeholder `<reviewed_registry_sha256>`만 제공한다. 가격, 전역 budget/call/token cap 또는 variant 내용을 바꾸면 registry 파일 hash와 정규화 config hash가 달라지므로 기존 dry-run evidence를 폐기하고 새 config hash 기반 evidence를 생성한다.

일 예산은 월 예산보다 클 수 없고, 일 호출 한도는 월 호출 한도보다 클 수 없다. 공급자 가격 통화가 KRW가 아니면 비용 예약·정산 전에 양수 `ai.pricing_to_budget_rate`로 KRW를 계산한다. 홈 예제의 실제 환율은 임의로 정하지 않고 마지막 준비 항목으로 남긴다. 환산 계수가 없거나 0 이하이면 호출을 fail-closed한다. 이는 JSON Schema로 표현하기 어려운 교차 필드 조건이므로 시작 전 의미 검증에서 확인한다. 활성 Provider 설정 누락은 프로세스 시작 실패다. 실행 중 예산을 소진하면 해당 시도를 `budget_skipped`로 남기고 통계 예측은 계속한다.

Production 전송은 공급자별 공식 HTTPS endpoint에 고정한다. OpenAI는 Responses API `https://api.openai.com/v1/responses`, Anthropic은 Messages API `https://api.anthropic.com/v1/messages`, Google은 `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`만 사용한다. Google key는 URL query가 아니라 `x-goog-api-key` 헤더로 전달한다. endpoint override와 redirect는 허용하지 않으며, 환경 proxy도 읽지 않는다. configured model ID는 OpenAI·Anthropic 요청 본문과 일치해야 하고 Google model은 하나의 URL path segment로 encode한다.

모든 응답은 1 MiB 기본 상한 안에서 streaming read하며 호출 wall timeout이 connect/read timeout의 최종 상한이다. 429와 다른 비정상 status에서는 status, 안전한 error code, request ID, `Retry-After`만 구조화하고 provider message·본문·prompt·key를 예외나 로그에 보존하지 않는다. `build_live_prediction_providers`는 검증된 `LiveProviderPlan`과 환경 secret으로 선택된 transport만 만든다. `FakeProviderTransport`는 계약·replay 테스트 전용이며 production factory에 주입할 수 없다.

구현 계약의 기준 문서는 [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses/create), [Anthropic Messages API](https://docs.anthropic.com/en/api/messages), [Gemini generateContent](https://ai.google.dev/api/generate-content), [Gemini API 인증](https://ai.google.dev/api)이다. 이 구현은 offline HTTP mock으로 검증한다. OP-003의 model/version·가격·예산·secret 확정과 소량 유료 smoke test가 끝나기 전에는 실제 요청을 계속 차단한다.

## 2026-09-27 OP-003 모델·비용 결정 후보

아래 표는 2026-09-27에 작성된 **과거 후보 기록**이며 활성 설정이나 현재 운영 계정의 검증 증거가 아니다. 홈 프로필은 OpenAI만 선택했지만 실제 계정 접근·응답 model ID·적용 환율은 아직 확인되지 않았다. OpenAI 행의 링크와 표기 가격은 공식 문서 조사값이며 운영 직전 다시 확인한다. Anthropic·Google 행은 향후 선택을 위한 감사 기록으로 남기며 현재 key나 smoke를 요구하지 않는다.

| Provider | 후보 API model ID | 버전 확인 방침 | Standard 입력 / 출력 가격 (USD, 100만 token당) | 공식 근거 |
|---|---|---|---|---|
| OpenAI | `gpt-6-sol` | `verify_resolved_model_id`; 공개 모델 페이지에는 이 ID를 사용하도록 안내하며 별도 날짜형 snapshot은 제시하지 않는다 | $2 / $10 | [모델](https://developers.openai.com/api/docs/models/gpt-6-sol), [가격](https://developers.openai.com/api/docs/pricing) |
| Anthropic | `claude-sonnet-5` | `immutable_model_id`; 4.6 이후 정식 model ID는 고정 snapshot이라는 공급자 계약 | $2 / $10 | [모델](https://platform.claude.com/docs/en/models/overview), [ID와 버전](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions) |
| Google | `gemini-3.8-flash` | `verify_resolved_model_id`; 특정 stable ID를 쓰되 응답 ID를 확인 | $0.75 / $3.75 (2026-12-31까지), 이후 $1.50 / $7.50 | [모델](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash), [가격](https://ai.google.dev/gemini-api/docs/pricing) |

비용 감각을 위한 계산 예시: 경기당 각 Provider를 1회 호출하고 각 호출의 과금 입력이 4,000 token, 과금 출력이 1,000 token이면 OpenAI $0.018, Anthropic $0.018, Google $0.00675로 합계 **$0.04275/경기**다. 같은 가정으로 252경기를 처리하면 **$10.773**이다. 이 수치는 환율·세금·재시도·thinking token 증가·긴 context 추가 요금·가격 변경을 포함하지 않으며, 실제 프롬프트의 token 측정값도 아니다.

### 2026-09-30 홈 프로필 비용 예약 후보

현재 OpenAI 공식 문서 조사값은 `gpt-6-sol` Standard 입력 USD 2, cache write 입력 USD 2.50, 출력 USD 10/100만 token이다. 홈 variant는 보수적으로 모든 입력 token을 USD 2.50으로 예약한다. 호출당 입력 4,000·출력 1,000 token 상한이면 최대 예약은 `(4,000 × 2.50 + 1,000 × 10) / 1,000,000 = USD 0.020`이다. KRW 예약액은 이 값에 운영 직전 검토한 양수 `ai.pricing_to_budget_rate`를 곱한다.

이 계산은 설정 후보이며 실제 계정의 모델 접근, 응답 model ID, 과금 분류와 가격을 확인한 증거가 아니다. 월 10,000원은 Vlytics 내부 KRW ledger cap이다. 세금, 환율 변동, 다른 앱·API 사용량과 공급자 청구 총액까지 10,000원 이하로 보장하지 않으므로 공급자 계정 청구와 가격도 별도로 최종 확인한다.

운영 전에는 선택된 OpenAI 계정의 접근 가능 여부, 1회 소량 호출의 요청/응답 model ID와 검증 가능한 resolved version, token 사용량, USD 비용, 적용한 `ai.pricing_to_budget_rate`와 KRW 환산 비용을 기록한다. 그 결과로 경기당·일·월 호출/비용 한도와 입력/출력 token 상한을 확정한다. 확인 전 홈 운영 예시의 version·OP-003·환율·registry hash·secret placeholder 및 live 차단을 유지한다.

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
| backup 자격 증명 | backup 활성화 시에만 `VLYTICS_BACKUP_CREDENTIAL` |
| 알림 목적지/자격 증명 | 알림 활성화 시에만 `VLYTICS_ALERT_DESTINATION` |
| Provider key | 활성 Provider만 필요. 홈 프로필은 `VLYTICS_OPENAI_API_KEY`만 사용 |

값은 배포 환경의 secret store에서 런타임에 주입한다. 시작 전 존재 여부만 확인하고 값 자체를 로그, config hash, 오류 메시지, audit event에 기록하지 않는다. config hash는 secret을 resolve하기 전의 비밀값 없는 정규화 설정으로 계산한다.

## 검증과 fail-fast

1. TOML parser로 문법과 중복 key를 검사한다.
2. 파싱된 객체를 `contracts/config.schema.json` draft 2020-12로 검증한다.
3. 위의 교차 필드 예산·retry·deadline 규칙을 의미 검증한다.
4. 활성 기능이 참조하는 환경변수의 존재를 검사하되 값을 출력하지 않는다.
5. live 시작 전에 DB 연결/권한, UTC clock, LAN/SSH 격리, 활성 Provider 소량 smoke test를 실행한다. backup 복구와 알림 전송은 해당 기능을 활성화한 프로필에서만 실행한다.

backend의 `vlytics.config.load_operational_config`가 1~4를 API 생성과 worker 시작 전에
실행한다. 기본 경로는 저장소의 예시와 schema이며, 배포에서는
`VLYTICS_OPERATIONAL_CONFIG_PATH`와 `VLYTICS_OPERATIONAL_SCHEMA_PATH`로 읽기 전용 mount를
지정한다. `example`/`development` 환경의 비활성 설정은 합성 개발을 위해 기동할 수 있지만,
`staging`/`production`은 `live_operations_enabled = true`가 아니면 API와 worker 모두 시작을
거부한다.

다음 상태에서는 해당 live 프로세스를 즉시 종료한다: 활성 구역의 placeholder 또는 0 한도, 미설정 host/auth/비용, 누락 secret, 유효하지 않은 budget 관계, 계산 불가능하거나 경기 시작을 넘는 deadline, OP-001 없이 켠 bulk 수집. 설정 오류를 기본값으로 보정하지 않는다.

합성 검증은 `live_operations_enabled = false`, 모든 유료/외부 기능 disabled 상태에서 계속 가능하다. 이 게이트는 프로젝트 scaffold, 계약 테스트, fake Provider, fake clock 작업을 막지 않는다.
