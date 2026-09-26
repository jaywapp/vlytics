# 실제 API를 사용하는 브라우저 E2E 검증

기록일: 2026-09-27

## 검증 범위

`frontend/tests/live-e2e/operator-live-flow.spec.ts`는 요청 가로채기 없이 production Nginx frontend에서 FastAPI와 PostgreSQL까지 같은 origin으로 연결하는 운영자 흐름을 검사한다.

1. 합성 KST 일정과 경기 상세 조회
2. 동일 Feature snapshot의 OpenAI·통계 예측 비교
3. 게시된 History 조회
4. DB의 실패 작업에 대한 인증된 재시도
5. 서버가 집계한 평가 결과의 성능 화면 표시

기존 `frontend/tests/e2e`는 route fixture를 사용하는 빠른 UI 상태 검증으로 유지한다.

## 합성 데이터 준비

migration이 끝난 폐기 가능한 CI DB에서 다음 환경변수와 명령을 사용한다. backend 의존성이 설치되어 있어야 한다.

```sh
export VLYTICS_LIVE_E2E_DATABASE_URL='<synthetic migrator database URL>'
uv run --project backend --locked python infra/scripts/seed_browser_smoke.py \
  --run-id '<unique CI run id>' \
  --output '<absolute seed manifest path>'
```

retry 단계가 작업 상태를 바꾸므로 실행마다 고유 run id가 필요하다. helper는 합성 mirror fact, 게시된 예측 2개, final 평가, coverage, 실패한 재시도 가능 작업 1개를 삽입하고 비밀이 아닌 식별자·표시값만 manifest에 기록한다. source 수집·Market adapter·Provider 호출을 활성화하지 않는다.

격리된 PostgreSQL에서 seed를 실행해 실제 read API 역할과 FastAPI serializer로 일정 1개, History 2개, OpenAI 성능 행, 기대한 실패 작업을 확인했다.

## 브라우저 실행

| 환경변수 | 값 |
|---|---|
| `VLYTICS_LIVE_E2E_BASE_URL` | Nginx frontend origin, 예: `http://127.0.0.1:8080` |
| `VLYTICS_LIVE_E2E_OPERATOR_TOKEN` | 이 CI 스택 전용 합성 bearer token |
| `VLYTICS_LIVE_E2E_SEED_MANIFEST` | seed helper가 만든 manifest의 절대 경로 |

frontend 디렉토리에서 `npm run test:e2e:live`를 실행한다. 전용 `playwright.live.config.ts`는 로컬 웹 서버를 시작하지 않으며, retry가 상태를 변경하므로 worker 1개를 사용한다. API mock은 없다. bearer token이나 Authorization header가 trace에 저장되지 않도록 trace·video를 끄고 실패 화면만 캡처한다.

## Compose 및 한계

`infra/scripts/ci-compose-smoke.sh`는 source·Provider가 비활성인 개발 Compose backend/DB와 production frontend image를 연결한다. 기존 복구 행 수 검사 뒤 고유 seed를 삽입하고 위 브라우저 검증을 실행한다. frontend의 Nginx는 해당 Compose network에서 `api:8000`을 해석한다.

운영 token·실제 데이터는 입력하지 않는다. 현재 로컬 환경에는 Docker CLI가 없어 Nginx container를 통한 브라우저 구간을 실행하지 못했다. seed 제약과 실제 API 응답은 검증했고, 브라우저 구간은 image 실행이 가능한 CI에서 확인해야 한다.

이 검증은 production Compose의 network 문제를 해결하지 않는다. frontend origin에 접근할 수 있는 환경에서 Nginx → FastAPI → PostgreSQL 흐름을 검사하며, production ingress·egress·internal network 정책은 변경하지 않는다.
