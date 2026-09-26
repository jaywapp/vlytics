# 전체 감사 조치 장부

기준: [2026-09-27 전체 프로젝트 감사](project-audit-2026-09-27.md). 현재 변경은 `codex/project-audit-20260927` 작업 브랜치에서 검증 중이며 전체 조치 완료가 아니다. 기존 테스트 통과를 운영 완료로 확대 해석하지 않는다.

| 항목 | 코드·production 연결 | 합성 검증과 남은 조건 |
|---|---|---|
| A01 역할별 secret | 프로세스/배포 전체 검증 분리 | 역할별 운영 설정 회귀 통과, production runtime 필요 |
| A02 KOVO 수집 | transport/factory/worker 및 planner 연결 | 실제 worker + MockTransport·승인 scope·global lease/RPM·재시작 DB 회귀 통과 |
| A03 네트워크 | 변경 미적용, 명시 승인 대기 | production runtime 실패 미해결 |
| A04 현재 예측 선택 | 실제 API SQL/UI 수정 | API DB 28 tests 및 UI 회귀 통과 |
| A05 retry deadline | orchestrator 수정 | 영속 dispatch 기록·재시작·창 안/밖 수동 retry·deadline 전용 DB 5 tests 통과. 깨끗한 PostgreSQL 전체 suite 360 tests 통과 |
| A06 경기별 호출 상한 | worker·DB 원장 연결 | 동시 예약 상한 회귀 통과 |
| A07 결과 finality | repository/factory 수정 | finality DB 계약 8 tests 통과 |
| A08 Elo 운영 연결 | 변경 미적용, 명시 승인 대기 | 기존 모듈 검증만 존재 |
| A09 복구 권한 | 별도 관리자·owner/ACL·로그인 검사 연결 | 새 cluster 39 tables/13 migrations/4역할+migrator 및 ACL 통과. 두 공식 DB owner roundtrip 통과. 앞선 복구에서 실제 API 200/401/200 및 worker 1회 통과 |
| A10 페이지 순회 | sync handler 수정 | 페이지·재개 회귀 통과 |
| A11 설정/마운트 일치 | enum 및 preflight 경로/hash 수정 | 경로 일치/불일치 통과, 실제 Compose 확인 필요 |
| A12 Market | API eligibility 수정, 생성 연결 승인 대기 | API 회귀 통과, 생성 경로 미검증 |
| A13 재실패 retry | UI 논리 요청 갱신 | frontend 23 tests 통과 |
| A14 coverage/History | 실제 API/UI 계약 수정 | SQL·UI 회귀 통과 |
| A15 CRLF | 패키지 검사 수정 | LF/CRLF PowerShell 양쪽 통과 |

## 조건부 개선

| ID | 범위 | 상태 |
|---|---|---|
| B1 | 검증된 roster/stats Feature | 생산 입력 연결 변경이 자동 승인 검토에서 거부되어 미적용·명시 승인 대기. 미검증 source 의미는 missing |
| B2 | 실제 browser→Nginx→API→DB | 실제 seed·browser spec·CI hook 추가. seed/실제 API 검증 통과, Nginx browser leg CI 실행 대기 |
| B3 | 장기 조회 성능 | endpoint SQL/keyset, 5,500 합성 경기 benchmark: 반환 99.35% 감소, scoped p50 9.23ms. API DB 28 tests 통과 |
| B4 | TLS·일관 backup snapshot | 옵션 보존 unit 및 exported snapshot restore drill 통과 |
| B5 | backup·alert·clock·heartbeat | 읽기 전용 감시 CLI·합성 12 tests, production poll heartbeat 연결. host 수집·예약·전송 증거 대기 |
| B6 | digest·SBOM·취약점 | .vite ignore·모든 base digest 입력·SBOM/취약점 gate 스크립트 반영. 실제 scan host 증거 대기(Docker/Trivy 로컬 부재) |
| B7 | 완료 장부 | plan 전체 완료 문구 철회, 이 장부에서 검증 범위 분리 |

## plan 재검증 항목

- TASK-006: source transport/worker/planner 연결, finality·pagination 통합.
- TASK-008: 운영 Elo 사용과 모델 버전 일치.
- TASK-012: 시작 자격·retry deadline·경기별 호출 상한.
- TASK-014: 현재 예측·Market·coverage 선택과 endpoint 조회 범위.
- TASK-017: 실제 응답 browser E2E와 새 DB 전체 회귀.
- TASK-018: production 네트워크·새 cluster 복구·예약 운영·릴리스 증거.

## live 검증과 승인

전체 live 검증은 미실행이다. 실제 source 접근·유료 Provider 호출·운영 배포를 이 합성 검증에 포함하지 않는다. 실제 Market adapter 부재는 UC-005의 허용 예외다.

자동 승인 검토가 A03 ingress/egress 추가, A08 운영 Elo 계산식 변경, A12 Market 비교 생성, B1 검증된 roster/stats 입력 연결 변경을 각각 거부했다. 구체적 사용자 승인을 기다리며 해당 변경은 우회 적용하지 않는다. 운영 host·NTP·backup/PITR·실제 alert 및 OP-001~004 증거도 별도로 필요하다.

## 통합 검증 기록

- 별도 SCRAM PostgreSQL cluster의 깨끗한 template 복제 DB에서 백엔드 전체 360 tests 통과. API 교차 검토와 DB owner 보완을 포함한 최종 전체 회귀 결과다.
- 복구 drill은 새 cluster에서 39 tables·13 migrations, 4개 서비스 로그인과 migrator 로그인, database-level ACL·별도 cluster 경계를 통과했다. 실제 API 200/401/200 및 worker 1회 검증은 앞선 복구 실행에서 확인했다.
- 로컬 Docker/Trivy가 없어 Nginx를 포함한 Compose browser 경로와 image SBOM/취약점 scan은 실행하지 못했다. CI hook 또는 스크립트 추가만으로 해당 항목을 통과 처리하지 않는다.
- API 추가 교차 검토 범위: 실패 attempt와 prediction 출처 구분, 평가에 고정된 Market snapshot 연결, lifecycle 변경에 따른 cursor 무효화, History 과거 team 식별, Performance 조회 cohort 제한, model 필터의 variant 선택 순서.

- 최종 프런트엔드 lint/typecheck·단위 23 tests·production build·fixture Playwright 9 tests 통과. Backend Ruff check/format·mypy 78 source files·PowerShell syntax 통과.
