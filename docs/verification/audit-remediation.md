# 전체 감사 조치 장부

기준: [2026-09-27 전체 프로젝트 감사](project-audit-2026-09-27.md). 현재 변경은 `codex/project-audit-20260927` 작업 브랜치에서 검증 중이며 전체 조치 완료가 아니다. A01~A16은 최초 감사 15개와 실제 브라우저 후속 발견 1개이며, B1~B7은 조건부 개선이다. 기존 테스트 통과를 운영 완료로 확대 해석하지 않는다.

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
| A11 설정/마운트 일치 | enum 및 preflight 경로/hash 수정 | 경로 일치/불일치 통과. 개발 Compose 실제 CI 성공, production 전체 접근 검증은 A03 해결 후 필요 |
| A12 Market | API eligibility 수정, 생성 연결 승인 대기 | API 회귀 통과, 생성 경로 미검증 |
| A13 재실패 retry | UI 논리 요청 갱신 | frontend 24 tests 통과 |
| A14 coverage/History | 실제 API/UI 계약 수정 | SQL·UI 회귀 통과 |
| A15 CRLF | 패키지 검사 수정 | LF/CRLF PowerShell 양쪽 통과 |
| A16 동일 경기 재선택 | 실제 browser 후속 검증에서 발견 | 동일 ID 상세 유지 수정. pending/완료 후 재클릭 회귀·실제 worker 포함 browser 1 test 통과 |

## 조건부 개선

| ID | 범위 | 상태 |
|---|---|---|
| B1 | 검증된 roster/stats Feature | 생산 입력 연결 변경이 자동 승인 검토에서 거부되어 미적용·명시 승인 대기. 미검증 source 의미는 missing |
| B2 | 실제 browser→Nginx→API→DB | 실제 seed·browser spec·CI hook 추가. seed/실제 API 검증 통과, Nginx browser leg CI 실행 36282222862 통과 |
| B3 | 장기 조회 성능 | endpoint SQL/keyset, 5,500 합성 경기 benchmark: 반환 99.35% 감소, scoped p50 9.23ms. API DB 28 tests 통과 |
| B4 | TLS·일관 backup snapshot | 옵션 보존 unit 및 exported snapshot restore drill 통과 |
| B5 | backup·alert·clock·heartbeat | 읽기 전용 DB·파일 evidence collector와 감시 CLI 연결. health/evidence 단위 25 tests·실제 read_api DB 통합 1 test 통과. host NTP/heartbeat 전달·예약·전송 증거 대기 |
| B6 | digest·SBOM·취약점 | .vite ignore·모든 base digest 입력·SBOM/취약점 gate 스크립트 반영. CI 빌드 후보의 immutable image ID별 SBOM/취약점 gate 연결·scanner 회귀 14 tests·Windows wrapper 포함 6 tests 통과, [실제 CI scan](image-security-evidence.md) 실행: 초기 취약점 보완 및 gosu 동일 소스 재빌드 후 CI 36285424504에서 compiler 포함 8개 image HIGH/CRITICAL 0건 통과, 게시 release digest 증거 별도 필요 |
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
- 로컬 Docker/Trivy는 없다. Nginx를 포함한 Compose browser 경로는 CI 36282222862에서 통과했고 image SBOM/취약점 scan은 미실행이다. 스크립트 추가만으로 release scan을 통과 처리하지 않는다.
- API 추가 교차 검토 범위: 실패 attempt와 prediction 출처 구분, 평가에 고정된 Market snapshot 연결, lifecycle 변경에 따른 cursor 무효화, History 과거 team 식별, Performance 조회 cohort 제한, model 필터의 variant 선택 순서.

- 최종 프런트엔드 lint/typecheck·단위 24 tests·production build·fixture Playwright 9 tests 통과. Backend Ruff check/format·mypy 78 source files·PowerShell syntax 통과.

## 실제 CI에서 추가 확인한 배선

- Windows 검증 job의 Python 3.12.14 배포본 부재를 확인하여 공식 Windows 배포본이 있는 3.12.10으로 고정했다. 수정 후 Windows 32 tests 통과.
- image 안의 Provider registry는 `/workspace/config/variants.toml`에 있으나 기본 탐색이 `/workspace/backend/config/variants.toml`을 가리키던 시작 오류를 image 환경변수로 수정했다. production Compose의 명시 경로 override는 유지한다.
- [CI 실행 36280349964](https://github.com/jaywapp/vlytics/actions/runs/36280349964)에서 실제 Nginx 인증 401/403/200, worker 재시작, 새 cluster owner/ACL 복구와 SCRAM 5로그인 경계를 통과했다. 마지막 브라우저 흐름은 경기 확률 제목의 기대값에서 실패해 후속 수정·실행으로 판정한다. 이 실행 전체를 성공으로 표시하지 않는다.

- 후속 live spec은 지정 경기 클릭·통계 58%/GPT 64% 전환·History·retry·고유 performance cohort 평가값을 검증한다. 실제 PostgreSQL/FastAPI/Vite proxy 전체 흐름 1 test 통과. Nginx 경로는 수정 후 CI로 최종 확인한다.

- 후속 fresh DB CI에서 이미 선택된 경기 재클릭의 loading 고착(A16)을 확인했다. 여러 경기가 있는 로컬 검증과 데이터 조건이 달랐으며, 실제 UI 수정·단일 경기 회귀로 처리한다.

- A16 수정 후 실제 worker(source/Provider 비활성) + PostgreSQL + FastAPI + Vite browser 전체 1 test, 관련 실제 DB API 28 tests, frontend 24 tests 통과. seed는 production ResultEvaluator/cohort를 사용하며 worker 후 단일 평가 행 n=1을 확인했다.

## 코드 검증 최종 결과

[CI 36282222862](https://github.com/jaywapp/vlytics/actions/runs/36282222862)가 코드 commit `9f43bab`에서 전체 성공했다. Linux backend·frontend·Windows deployment 검사, 실제 Nginx→FastAPI→PostgreSQL browser 전체 흐름, worker 재시작, owner/ACL 보존 복구와 SCRAM 서비스 4개+migrator 로그인 검증을 포함한다. 문서 기록 commit `c200fc1`의 [CI 36282540490](https://github.com/jaywapp/vlytics/actions/runs/36282540490)도 성공했다. 아래 B5/B6 후속 변경은 별도 검증한다.

A03·A08·A12 생성·B1의 자동 승인 거부와 운영 호스트·실제 source/Provider·PITR·알림·release scan 증거는 여전히 미완료다. CI 성공을 production 활성화 승인이나 전체 목표 완료로 해석하지 않는다. 수정은 [초안 PR #12](https://github.com/jaywapp/vlytics/pull/12)에 있으며 병합하지 않았다.

## B5/B6 후속 검토

- 상태 수집기와 health 평가기를 연결하면서 수집 시각, 비활성 Provider, 잘못된 입력 행 처리를 함께 보완했다. 실제 DB 읽기 권한 검증과 합성 회귀는 운영 host 예약·NTP 수집·알림 발송 증거를 대체하지 않는다.
- 이미지 검사의 빈 SBOM/scan 결과 수용, SBOM 내부 image ID 누락, 취약점 DB provenance 누락, 전역 도구 오류의 artifact 누락을 후속 검토에서 발견하여 보완했다. CI와 release는 같은 Python collector를 사용하고 release는 게시 digest와 로컬 image identity 연결을 추가 확인한다.
- uv 0.12.5의 최종 image는 scratch 기반이며 cargo-auditable binary를 포함한다. OS package가 없다는 이유로 검사를 생략하지 않고 검출 가능한 언어 package inventory의 검사 범위를 실제 CI에서 확인한다.

B6 공용 collector는 기존 출력 경로를 거부해 과거 manifest를 보존하고, 실패 시 이미 수집한 image 기록과 sanitized error code를 남긴다. SBOM·취약점 보고서의 image ID, package inventory, 취약점 DB metadata와 48시간 신선도를 검증한다. CI scanner 이전 단계의 설치 실패는 scan artifact가 없음을 그대로 표시한다.

B5 검증: 별도 합성 PostgreSQL에서 실제 `vlytics_read_api_login`으로 실행 중 job lease와 Provider 예산 조회 1 test 통과. health/evidence 25 tests 및 이미지/Windows wrapper를 포함한 로컬 회귀 45 tests 통과. Ruff 전체, mypy 79 source files 통과. 테스트용 PostgreSQL은 종료했다.

최초 실제 image scan에서 SBOM·보고서 identity/DB 신선도 검증은 동작했으며 6개 image의 보안 발견으로 gate가 실패했다. 이 결과를 도구 오류나 성공으로 바꾸지 않고 [상세 증거](image-security-evidence.md)에 기록한다.

보안 업데이트 `859f489`의 CI 36284623462에서 6/7 이미지의 실제 HIGH/CRITICAL 0건과 Alpine 실제 스택 회귀를 확인했다. PostgreSQL gosu의 22행 때문에 전체 CI는 실패이며 새 compiler 고정 재빌드 후보를 별도로 검증한다.

## 후속 보완 최종 상태

[CI 36285424504](https://github.com/jaywapp/vlytics/actions/runs/36285424504) 전체 성공: backend 400 tests, Windows 34, frontend 24·fixture E2E 9, 실제 browser 1·worker 재시작·새 cluster 복구, 8개 image SBOM/취약점 gate를 통과했다. 후보 head는 `90a6e41`, 검사한 PR merge commit은 `c506144`다. 원본 artifact hash와 gosu source/compiler/image ID 관계를 직접 확인했다. [이미지 상세 증거](image-security-evidence.md)를 따른다.

B5의 DB/파일 collector와 평가기, B6의 실제 CI image 검사 보완은 검증됐다. **전체 감사 조치 완료는 아니다.** A03 ingress/egress, A08 운영 Elo, A12 Market 생성, B1 roster/stats 입력의 자동 승인 거부는 그대로이며 운영 host NTP/heartbeat 전달·예약 backup/PITR·알림·source/유료 Provider smoke·게시 release digest 증거가 남아 있다. PR #12는 초안으로 유지하고 병합하지 않았다.
