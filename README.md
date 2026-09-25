# Vlytics

Vlytics는 V리그 관측 데이터, 경기 전 예측, 사후 평가를 한 흐름에서 감사하기 위한 개인용 실험 플랫폼이다. 백엔드는 하나의 Python artifact를 API와 worker 두 프로세스로 실행하고, PostgreSQL과 React SPA를 함께 사용한다.

## 요구 버전

- Python 3.12.14
- uv 0.12.5
- Node.js 22.23.2 LTS와 npm 10.9.8
- Docker Engine과 Compose v2 (로컬 PostgreSQL 및 전체 스택 실행 시)

## 백엔드

```powershell
Set-Location backend
uv sync --frozen
uv run vlytics-migrate
uv run vlytics-api
```

`vlytics-migrate`는 애플리케이션 계정이 아닌 `VLYTICS_MIGRATION_DATABASE_URL`의
`vlytics_migrator` 로그인으로 실행한다. 마이그레이션은 checksum과 advisory lock으로
한 번만 적용된다. 실행 전에 `VLYTICS_COLLECTOR_DATABASE_PASSWORD`,
`VLYTICS_ENGINE_DATABASE_PASSWORD`, `VLYTICS_MARKET_INGEST_DATABASE_PASSWORD`,
`VLYTICS_READ_API_DATABASE_PASSWORD`를 서로 다른 외부 secret으로 주입해야 한다.

별도 터미널에서 worker를 실행한다.

```powershell
Set-Location backend
uv run vlytics-worker
```

API와 worker는 시작할 때 저장소의 `config/example.toml`과
`contracts/config.schema.json`을 읽고 Draft 2020-12 및 교차 필드 규칙을 검증한다.
다른 설정 파일을 사용할 때는 `VLYTICS_OPERATIONAL_CONFIG_PATH`와
`VLYTICS_OPERATIONAL_SCHEMA_PATH`에 경로를 지정한다. TOML에는 secret 값 대신
환경변수 이름만 기록한다.

검증 명령은 다음과 같다.

```powershell
Set-Location backend
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv build
```

## 프런트엔드

```powershell
Set-Location frontend
npm ci
npm run dev
```

검증 명령은 다음과 같다.

```powershell
npm run lint
npm run typecheck
npm run build
```

개발 서버는 `/api` 요청을 `http://localhost:8000`으로 전달한다.

## 로컬 PostgreSQL 또는 전체 스택

PostgreSQL만 실행하려면 다음 명령을 사용한다.

```powershell
docker compose -f infra/compose.yaml up -d postgres
```

API와 worker까지 같은 백엔드 이미지로 실행하려면 다음 명령을 사용한다.

```powershell
docker compose -f infra/compose.yaml up --build
```

전체 스택에서는 `migrate` one-shot 컨테이너가 PostgreSQL healthcheck 이후 먼저 끝나야
API와 worker가 시작된다. PostgreSQL host port는 기본적으로 `127.0.0.1`에만 bind된다.

DB 역할 경계는 다음과 같다.

| 로그인 | 용도 | 변경 가능한 데이터 |
|---|---|---|
| `vlytics_migrator` | schema migration과 역할 관리 | schema 객체 |
| `vlytics_collector_login` | 원천 receipt와 Mirror fact 적재 | append-only `mirror` 행 삽입 |
| `vlytics_engine_login` | feature·예측·평가·작업 처리 | append-only 산출물 삽입, job lease와 projection 갱신 |
| `vlytics_market_ingest_login` | 외부 Market snapshot 적재 | append-only Market snapshot 삽입 |
| `vlytics_read_api_login` | 조회 API | 읽기 전용 |

Compose 실행 전 아래 값을 외부 환경 또는 secret 저장소에서 모두 주입해야 한다. 파일에는
기본 암호가 없으며, 하나라도 빠지면 Compose가 즉시 실패한다.

- `MIGRATOR_DATABASE_PASSWORD`: PostgreSQL bootstrap 로그인 암호
- `MIGRATOR_DATABASE_URL`: `vlytics_migrator`의 컨테이너 내부 접속 URL
- `COLLECTOR_DATABASE_PASSWORD`, `ENGINE_DATABASE_PASSWORD`,
  `MARKET_INGEST_DATABASE_PASSWORD`, `READ_API_DATABASE_PASSWORD`: migration이 설정할
  네 애플리케이션 로그인 암호
- `READ_API_DATABASE_URL`: API가 사용할 `vlytics_read_api_login` 접속 URL
- `ENGINE_DATABASE_URL`: worker가 사용할 `vlytics_engine_login` 접속 URL

URL에 포함되는 암호는 URL encoding해야 한다. collector와 Market ingest 프로세스를 별도로
실행할 때도 해당 최소 권한 로그인으로 URL을 구성한다. `.env`, 원천 raw, DB dump, 모델
원문 응답은 Git 추적 대상에서 제외된다.

직접·전이 의존성은 `uv.lock`과 `package-lock.json`으로 고정한다. CI와 로컬 설치는 lockfile 변경을 허용하지 않는 명령을 사용한다.
