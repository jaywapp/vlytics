# 실제 이미지 보안 검사 증거

## 최신 후속 검사

[CI 36290510898](https://github.com/jaywapp/vlytics/actions/runs/36290510898), 후보 `3038ba4`의 전체 검증이 성공했다. 실제 PR merge revision은 `fea4c2ce923f5d6b90e9fc90a418ea95dbd5f87f`이며 부모 `69585df`/`3038ba4`를 GitHub API에서 확인했다. 8개 image가 모두 HIGH/CRITICAL 0건이고 다운로드한 artifact의 SBOM/취약점 보고서 16개 hash를 manifest와 대조했다. Trivy 0.74.0, 취약점 DB 갱신 시각은 `2026-09-27T00:40:58.172367669Z`다. scope는 `built-ci-images-not-published-release`이며 게시 release 증거로 확대하지 않는다. 앞선 후보 `6280185`의 CI 36289935169도 같은 gate를 통과했다.

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

## 보안 업데이트 재검사

[CI 36284623462](https://github.com/jaywapp/vlytics/actions/runs/36284623462), commit `859f489`에서 FastAPI/Starlette 업데이트·Python Alpine 전환·Node/npm 및 Nginx/libexpat 파생 base를 실제 빌드했다. Backend·frontend·Python·Node·Nginx·uv 모두 package inventory/identity/DB 신선도와 HIGH/CRITICAL 0건 gate를 통과했다. 실제 Compose browser와 복구 흐름도 통과했다. 전체 CI는 PostgreSQL upstream gosu의 22행 때문에 실패 상태다.

PostgreSQL의 [gosu 재빌드 후보](gosu-rebuild.md)는 동일 source commit을 고정한 patched Go compiler로만 다시 빌드한다. CI에서 compiler를 여덟 번째 이미지로 검사하고 binary/source/module/build metadata를 별도 `gosu-build-provenance` artifact에 저장한다. 후보가 실제 빌드·PostgreSQL 초기화·복구·8개 image 검사까지 통과해야 해당 항목을 닫을 수 있다.

## 최종 CI 확인

[CI 36285424504](https://github.com/jaywapp/vlytics/actions/runs/36285424504)가 후보 `90a6e41`에서 전체 성공했다. 실제 checkout·manifest revision은 PR merge commit `c5061442dde43f69f0e0cb70b077211e0eb2dbfd`이며 부모는 main `69585df`와 후보 `90a6e41`이다.

- backend·frontend·PostgreSQL·Python·uv·Node·Nginx·Go compiler **8개 전부 HIGH/CRITICAL 0건**.
- 모든 SBOM·취약점 JSON hash를 다운로드한 manifest와 다시 대조했다. nonempty package inventory, image ID, DB 신선도 gate도 통과했다.
- 취약점 DB 갱신 시각 `2026-09-27T00:40:58.172367669Z`.
- `gosu-build-provenance`의 PostgreSQL image ID와 compiler digest가 scanner manifest의 동일 image/label/build input과 일치한다. Go 1.26.8 및 module 검증 성공 기록도 확인했다.
- backend 400 tests, Windows 34 tests, frontend 24 tests·fixture E2E 9 tests, 실제 Nginx→API→DB browser 1 test와 worker 재시작·새 cluster 복구를 통과했다.

이 결과는 해당 commit의 linux/amd64 CI 후보에 대한 현재 DB 기준 검사다. 운영 배포·registry 게시·실제 release digest 검사는 수행하지 않았다. A03/A08/A12 생성/B1 및 호스트·실제 외부 호출 증거는 별도 미완료다.
