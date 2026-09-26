# MVP 재생 검증 보고서

- 검증일: 2026-09-25 (Asia/Seoul)
- 대상: `codex/implement-vlytics-mvp`
- 범위: `docs/prepare/plan.md`의 TASK-017
- 판정: 합성 MVP 재생과 계약 검증 통과. 외부 운영 의존성은 아래 제한 사항에 별도로 기록한다.

## 검증 환경과 경계

백엔드는 Windows portable PostgreSQL 17.11의 전용 `vlytics_test` 데이터베이스에서 검증했다. 최초 migration부터 네 애플리케이션 역할 인증, 역할 경계, append-only 제약, lease·동시성, SQL repository, 운영 retry, API 조회까지 하나의 clean full-suite 실행으로 확인했다. 테스트 프로세스마다 네 역할 비밀번호를 임의 생성하여 환경 변수로만 주입했고 값은 로그와 저장소에 남기지 않았다.

재생 입력은 가상 clock, 합성 일정, 저장된 synthetic mirror fixture를 사용했다. OP-001의 접근 허용 범위와 요청량 정책이 미확정이므로 TASK-001에서 합법적으로 확인해 문서화한 소량 계약·coverage 외에는 KOVO 네트워크 요청을 새로 보내지 않았고, 승인되지 않은 실제 payload를 저장하지 않았다. 이 경계는 합성 MVP 검증 결과와 분리한다.

프런트엔드는 production build를 Vite preview로 실행하고 Playwright Chromium headless에서 API fixture를 route interception하여 검증했다. 따라서 브라우저 렌더링과 사용자 흐름은 실제 production bundle로 확인했지만, 실제 백엔드와의 CORS·네트워크 배선은 이 TASK의 브라우저 범위에 포함되지 않는다.

## FR-01~12 증거

| 요구사항 | 실행 증거 | 결과 |
| --- | --- | --- |
| FR-01 시즌·리그·일정 동기화 | `test_planner_handles_zero_and_multiple_matches_without_t10_jobs`; source contract와 parser 계약 테스트 | 0/1/다수 일정 및 revision 경계를 통과 |
| FR-02 Raw·정정 이력 보존 | `test_receipts_are_not_deduplicated_but_fact_revisions_are`; correction/mapping PostgreSQL 테스트 | 관측 receipt 보존, 동일 사실 중복 제거, 정정 revision 추가 통과 |
| FR-03 시점 제한 Feature | `test_target_match_roster_is_never_injected_from_after_cutoff`; feature snapshot repository round-trip | cutoff 이후 입력과 대상 경기 결과 유입 0건 |
| FR-04 T-1h 분석 실행 | `test_concurrent_claim_and_expired_lease_restart_recovery`; semantic identity·restart 테스트 | 동시 claim 1건, lease 복구, 대표 성공 중복 0건 |
| FR-05 통계 독립 예측 | `test_joint_artifact_and_four_capability_prediction_validate_against_schemas`; MissingMarketAdapter 테스트 | 시장 입력 없이 통계 예측과 네 capability 계약 통과 |
| FR-06 AI 교체·동시 실행 | `test_replay_freezes_one_snapshot_runs_four_independent_variants_and_never_recalls_success`; Provider contract 16개 | 동일 snapshot의 GPT·Claude·Gemini 독립 실행, 부분 실패 격리 통과 |
| FR-07 네 예측 유형 일관성 | `test_post_prediction_probabilities_cover_winner_handicap_and_totals`; joint score/set distribution 테스트 | 승패·세트 스코어·핸디캡·총점과 push/void 계산 통과 |
| FR-08 예측 불변성 | `test_authenticated_roles_cannot_mutate_append_only_data`; 전체 immutable table guard 테스트 | 운영 역할의 UPDATE/DELETE/TRUNCATE 거부 통과 |
| FR-09 종료 후 평가 | `test_evaluation_repository_is_idempotent_and_corrections_append` | 결과 revision별 평가 추가, 재실행 중복 0건 |
| FR-10 기록 조회 | `test_cursor_boundaries_have_no_duplicates_or_omissions`; Playwright history filter/detail/back | 필터·상세·opaque cursor·뒤로가기 상태 통과 |
| FR-11 성능 비교 | `test_performance_uses_latest_corrected_revision_and_exact_paired_statistics`; Playwright performance | n=0/1/20, paired n, CI, correction, market missing 표시 통과 |
| FR-12 운영 상태·비용 | `test_performance_and_operations_are_server_aggregated_with_revisions`; budget·retry·operations E2E | coverage·실패·예산·deadline·idempotent retry 상태 통과 |

`backend/tests/replay/test_mvp_evidence.py`는 위 12개 항목과 실제 테스트 함수의 연결이 끊어지지 않는지 실행 시 검사한다.

## 합성 재생 결과

| 시나리오 | 핵심 관찰 | 결과 |
| --- | --- | --- |
| 정상 | 한 feature snapshot을 통계·OpenAI(GPT)·Anthropic(Claude)·Google(Gemini)이 공유하고 각각 별도 prediction hash를 생성 | 4/4 성공, snapshot 1개, prediction 4개, 고유 hash 4개 |
| 명단 미확정 | cutoff 뒤 roster를 제외하고 availability policy를 snapshot identity에 포함 | 누수 0건, 명시적 미확정 상태 유지 |
| 원천 오류 | retryable HTTP receipt를 fact 성공 전 커밋하고 다음 성공에서 checkpoint를 전진 | receipt 유실 0건, 중복 fact 0건 |
| Provider 부분 실패 | OpenAI timeout을 강제하면서 Anthropic·Google을 병렬 실행 | 실패 1개가 성공 2개를 취소하지 않음 |
| 연기 | 일정 revision 변경이 기존 lease와 실행 자격을 무효화 | 새 일정으로 재계획, 이전 성공·감사 이력 보존 |
| 취소 | 실행 중 schedule을 cancelled로 변경 | lease 취소, 늦은 Provider 응답의 prediction 삽입 0건 |
| 결과 정정 | 새 result revision으로 평가를 다시 생성 | 이전 평가 보존, 새 평가 추가, 중복 0건 |
| Market missing | 기본 adapter가 `missing`을 반환하고 prediction input에서 market 필드를 제외 | 통계·AI 비교 유지, 오류나 임의 0값으로 대체하지 않음 |

Provider 실패 coverage는 timeout, invalid JSON, 확률 합 불일치, 비유한 확률, refusal, model alias drift, 확인되지 않은 version, 입력 상한 초과, prompt block, 예산 소진을 서로 다른 상태로 검증한다. 시작 시각 이후 완료, 늦은 timeout thread, 재시작, concurrent budget reservation도 별도 검증했다.

## 발견하여 수정한 고위험 결함

1. PostgreSQL migration이 중간에 `vlytics_migrator`를 `NOSUPERUSER`로 강등한 뒤 다른 역할을 변경하여 PostgreSQL 17에서 실패했다. 역할·schema·grant·비밀번호 설정을 마친 최초 migration의 마지막 단계로 자기 강등을 이동했다.
2. migration owner가 schema를 만들기 전에 현재 DB의 `CREATE` 권한이 없어 clean cluster에서 실패했다. role switch 이전에 최소 DB 권한을 부여했다.
3. nullable UUID/text 조건과 JSON literal bind가 PostgreSQL·SQLAlchemy에서 잘못 해석되던 job/prediction SQL을 명시적 cast·jsonb bind로 수정했다.
4. feature snapshot hash가 세션 timezone에 따라 달라질 수 있어 cutoff와 captured 시각을 UTC로 정규화했다.
5. PostgreSQL 17 제약과 달라진 immutability·lease 테스트 fixture, 유효하지 않은 배구 정정 점수 fixture, Provider 응답 schema를 prediction 전체 schema로 검증하지 않던 replay fixture를 수정했다.

6. Market 평가의 `missing`·`stale`·`late`·`unsupported` 상태가 저장·조회 과정에서 압축되거나 NULL snapshot id가 문자열로 변환되던 경로를 수정하고 NULL-safe idempotency를 추가했다.
7. production worker가 OP-004 증빙을 주입받을 경로가 없던 문제를 해결해 bounded JSON loader, exact config hash, 유효기간·미래 시각·Provider 집합 검증을 시작 경로에 연결했다.
8. 결정적 Provider variant ID가 기존의 다른 model metadata 행을 조용히 재사용하던 문제를 fail-closed identity 비교로 수정했다.
9. PostgreSQL image의 initdb superuser와 self-demoting migrator가 같은 역할이어서 clean 설치가 실패하던 문제를 분리했다. bootstrap 역할은 migrator 생성 직후 `NOLOGIN`, migrator는 최초 migration 뒤 `NOSUPERUSER`가 된다.
10. 강등된 migrator의 migration 재실행이 기존 ledger에 불필요한 CREATE 권한을 요구하던 문제를 존재 조회 후 최초에만 생성하도록 수정했다.

최초 migration 뒤 `vlytics_migrator`는 의도대로 자기 강등된다. 이후 애플리케이션 역할 비밀번호 회전은 bootstrap cluster administrator가 수행해야 하며, migration role에 지속적인 `ADMIN OPTION`을 남기지 않는다.

## 프런트엔드 브라우저 검증

Playwright 1.63.0과 Chromium headless로 다음을 production bundle에서 확인했다.

- `/`, `/history`, `/performance`, `/operations` 라우트
- Bearer 인증과 URL·history에 token 미노출
- 키보드 로그인, Tab focus, 주요 링크·버튼 접근
- 390px·1440px viewport에서 수평 overflow 없음
- loading, empty, error, retry, Provider partial failure, market missing 상태
- history filter → detail → browser back 상태 보존
- retry deadline과 idempotency key, performance n=0/1/20 및 서버 제공 CI

이는 핵심 키보드·focus·상태 접근성 검증이며 완전한 WCAG 적합성 감사 결과를 뜻하지 않는다.

## 최종 검사 결과

| 검사 | 결과 |
| --- | --- |
| Backend full pytest + clean PostgreSQL 17.11 | 297 passed, 0 failed, 0 errors, 0 skipped, 0 xfailed |
| CI PostgreSQL bootstrap 재검증 | 운영 init SQL을 격리 DB에 적용, bootstrap `NOLOGIN`·migrator `NOSUPERUSER` 확인 |
| CI 합성 Compose smoke | backend 이미지 빌드, PostgreSQL·migration·API·worker 기동, 인증 401/200, 합성 source job 격리 후 worker 재시작 중복 0, migration 재실행, custom dump와 격리 복구 통과 ([CI 실행](https://github.com/jaywapp/vlytics/actions/runs/36088207723)) |
| CI frontend image smoke | digest로 확인한 Node·Nginx base에서 frontend image 빌드, 비루트·읽기 전용 컨테이너 기동, `/healthz`, `/history` fallback, CSP, same-origin `/api` 익명 401·readonly 403·operator 200 확인 ([CI 실행](https://github.com/jaywapp/vlytics/actions/runs/36270882620)) |
| Backend replay registry 재확인 | 2 passed (위 297개의 부분집합) |
| Ruff lint / format | 통과, 103 files formatted |
| mypy | 통과, 73 source files |
| Backend build | sdist와 wheel 생성 성공 |
| OpenAPI | 반복 생성 결과 동일, `contracts/openapi.json`과 semantic equality 통과 |
| Frontend ESLint / TypeScript | 통과 |
| Frontend Vitest | 3 files, 20 passed |
| Frontend Playwright Chromium | 9 passed |
| Frontend production build | 47 modules, build 성공 |
| 민감정보 scan | private-key/API-key 패턴 및 추적 대상 `.env` 후보 0개 |

백엔드에는 Starlette가 사용하는 AnyIO alias의 upstream deprecation warning 1건이 남지만 기능 실패는 없다.

## 외부 운영 제한

CI 스모크는 외부 호출을 끈 개발용 Compose에서 실행했다. frontend production Dockerfile의 Nginx 이미지는 CI에서 운영과 같은 비루트·읽기 전용 옵션으로 기동했지만, production Compose 전체와 운영 host의 private ingress·backup/PITR·알림·시계 및 live T-60은 아직 검증하지 않았다.



- OP-001: KOVO 접근 허용 범위·rate 정책이 미확정이다. live 수집·대량 backfill은 계속 차단하며 합성 수집기와 저장 계약만 검증했다.
- OP-003: 실제 GPT·Claude·Gemini model ID, 유료 호출 권한·예산·secret이 확정되지 않았다. 공급자별 고정 HTTPS transport와 독립 adapter·계약·실패 격리는 offline HTTP mock으로 검증했으며 실제 유료 모델 호출이나 품질을 주장하지 않는다.
- OP-004: worker가 exact config hash·형식·크기·유효기간을 검사하는 증빙 로더와 read-only compose mount를 사용한다. 실제 source와 세 Provider를 연결한 live T-60 dry-run 증빙은 없으므로 활성화는 계속 차단한다.
- OP-005: 실제 Market schema·adapter가 없으므로 기본 `missing` 운영과 합성 line 계약을 검증했다.
- 실제 KOVO payload, 실 Provider 응답, 실 Market 응답은 이 보고서나 fixture에 포함하지 않았다.
