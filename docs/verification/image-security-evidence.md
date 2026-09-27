# 실제 이미지 보안 검사 증거

## 최초 검사

[CI 36283661022](https://github.com/jaywapp/vlytics/actions/runs/36283661022), commit `fad7146`의 Trivy 0.74.0 검사 결과다. 7개 이미지의 SBOM과 package inventory, image ID 일치 검증은 완료됐으나 6개 이미지에서 HIGH/CRITICAL이 검출되어 전체 CI는 실패했다. 검출 수는 image별 package/CVE 행 수이며 고유 CVE 수나 실제 공격 가능성 판정이 아니다.

| image | HIGH/CRITICAL 행 | 판정 |
|---|---:|---|
| backend | 47 | vulnerabilities_found |
| frontend | 1 | vulnerabilities_found |
| nginx | 1 | vulnerabilities_found |
| node | 8 | vulnerabilities_found |
| postgres | 22 | vulnerabilities_found |
| python | 44 | vulnerabilities_found |
| uv | 0 | passed |

취약점 DB 갱신 시각: `2026-09-26T19:03:57.371914884Z`. 원본 SBOM·scan JSON·hash·image ID는 해당 실행의 `ci-image-evidence` artifact에 있다. uv의 cargo-auditable package inventory는 실제 검사에 포함되었고 0건으로 통과했다.

## 보완 방향과 한계

- Backend 애플리케이션: Starlette 0.47.3의 3건을 해결하는 FastAPI 0.141.1/Starlette 1.3.1로 고정하고 lock을 갱신했다. API/startup/health 59 tests 통과, DB 환경변수 없는 8 tests는 skip으로 남으며 전체 실제 DB 회귀와 image 재검사는 CI에서 판정한다.
- Python slim-trixie: 44행/8개 CVE에 FixedVersion이 없었다. Debian stable의 util-linux·acl·ncurses에 수정판이 없음을 공식 tracker에서 확인했다. 동일 Python 3.12.14의 공식 Alpine 3.24 image로 개발/CI 후보를 변경하고 musl 의존성과 전체 실제 스택을 재검증한다.
- Nginx/frontend: 동일 official stable digest 위 libexpat 2.8.5-r0을 설치한 파생 base를 빌드·재검사한다.
- Node build image: 동일 Node 22 official digest 위 수정된 dependencies를 포함하는 npm 12.1.0을 설치한 파생 base를 빌드·재검사한다.
- PostgreSQL 17.11 image: Go 1.24.6로 빌드된 구성 요소의 22행을 확인했다. DB major 17을 유지하는 보완 후보를 확인한다.

수정 후보 선택을 검사 통과로 간주하지 않는다. `--ignore-unfixed`, 취약점 제외 목록, scanner 실패 무시를 추가하지 않으며 남는 발견은 릴리스 gate 실패로 기록한다. CI 빌드 후보 증거와 게시된 운영 release digest 증거는 별도다.

공식 근거: [Python image 목록](https://raw.githubusercontent.com/docker-library/official-images/master/library/python), [Debian util-linux](https://security-tracker.debian.org/tracker/CVE-2026-76642), [Debian acl](https://security-tracker.debian.org/tracker/CVE-2026-54369), [Debian ncurses](https://security-tracker.debian.org/tracker/CVE-2025-69720).

애플리케이션 의존성 근거: [FastAPI 0.141.1 Starlette 지원 범위](https://raw.githubusercontent.com/fastapi/fastapi/0.141.1/pyproject.toml), [Starlette 1.3.1](https://github.com/Kludex/starlette/releases/tag/1.3.1), [CVE-2025-62727](https://github.com/advisories/GHSA-7f5h-v6xp-fcq8), [CVE-2026-48818](https://github.com/advisories/GHSA-wqp7-x3pw-xc5r), [CVE-2026-54283](https://github.com/advisories/GHSA-82w8-qh3p-5jfq).

B5 포함 commit `eca2129`의 [CI 36283848936](https://github.com/jaywapp/vlytics/actions/runs/36283848936)에서도 기능 검증(backend 390 tests, Windows 34, frontend 24, fixture E2E 9, 실제 browser 1·복구)은 통과했으며 기존 image의 동일 보안 gate에서 실패했다.
