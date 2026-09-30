# 홈 서버 Provider egress 승인·구현 기록

상태: 사용자가 이번 작업에서 OpenAI 연결 구현 진행을 직접 승인했다. generic relay, OpenAI worker proxy, 홈 Compose의 `openai_egress`와 전용 `openai_access` 배선은 **저장소 구현 완료**다. 실제 키 주입·API 호출·홈 서버 배포는 수행하지 않았으며 실서버 검증은 대기 중이다. Anthropic·Gemini는 향후 고려 대상으로만 남고 현재 비활성이다.

## 연결하면 무엇이 달라지는가

홈 서버의 worker가 경기 시작 전에 고정한 Feature snapshot과 예측 지시문을 OpenAI API로 보내고, 독립 예측 응답을 받는다. 목적지는 `https://api.openai.com/v1/responses` 하나다. Anthropic·Gemini·KOVO에는 이 경로를 연결하지 않는다. KOVO 수집은 별도 이용 근거를 확인할 때까지 비활성이다.

전달되는 snapshot에는 경기·시점 정보와 통계 Feature 및 출처 식별 정보가 포함된다. Market 배당, 다른 모델의 예측, 검색·도구 실행은 기존 입력 검증에서 차단한다. OpenAI API 키는 OpenAI 인증에 사용한다. 따라서 이 연결은 경기 분석 입력을 OpenAI가 처리하도록 전송하는 행위다. 사용자 결정인 개인 분석·외부 공개 금지 방침은 그대로 유지한다.

## 연결 구조와 제한

현재 홈 프로필은 `worker → openai_egress → api.openai.com:443` 순서로 연결한다. worker는 `private` 내부망에만 남고, `openai_egress`만 `private`과 전용 `openai_access`에 가입한다. 중계 포트는 호스트에 게시하지 않는다. worker는 relay health 이후에 시작한다.

relay의 전용 외부망은 `gw_priority: 1`로 기본 gateway를 고정한다. 홈 배포에는 Docker Compose 2.33.1 이상이 필요하다. [Docker 공식 gateway priority 문서](https://docs.docker.com/reference/compose-file/services/#gw_priority)에 따라 내부망은 기본 우선순위 0을 사용한다. 구버전 parser의 거부를 무시하거나 이 필드를 제거해 통과시키지 않는다.

중계는 HTTP CONNECT 터널만 만들며 TLS를 해독하지 않는다. OpenAI 인증 헤더와 분석 입력은 worker와 OpenAI 사이의 TLS 암호문으로 통과한다. 중계는 API 키를 환경변수로 받지 않으며 요청·응답 본문을 기록하지 않는다. TLS 인증서 검증과 redirect 금지는 기존 HTTP 클라이언트에서 유지한다.

relay는 `VLYTICS_EGRESS_PROVIDER`를 `openai`, `anthropic`, `google` 중 하나로만 받고 인스턴스 하나가 Provider 하나와 정확한 443 목적지만 허용한다.

| selector | 허용 목적지 | 고정 worker proxy | 전용 외부망 |
|---|---|---|---|
| `openai` | `api.openai.com:443` | `http://openai_egress:8081` | `openai_access` |
| `anthropic` | `api.anthropic.com:443` | `http://anthropic_egress:8081` | `anthropic_access` |
| `google` | `generativelanguage.googleapis.com:443` | `http://google_egress:8081` | `google_access` |

다른 호스트·포트·IP 직접 지정·일반 HTTP 요청은 거부한다. DNS 응답에 사설망·loopback·link-local 등이 섞여도 차단하고, 검증한 IP에 직접 연결해 DNS 재조회에 따른 목적지 변경을 막는다. 헤더 크기, 요청·연결·유휴 시간과 동시 연결 수를 제한한다.

중계 구현은 [provider_egress.py](../../backend/src/vlytics/ops/provider_egress.py), 검증은 [test_provider_egress.py](../../backend/tests/test_provider_egress.py)에서 확인할 수 있다. 테스트는 로컬 검증 서버와 가짜 DNS 응답만 사용한다. 실제 OpenAI 통신 성공을 증명하지는 않는다.

## 적용된 구체 변경

홈 Compose의 worker 환경에 아래 값 하나를 추가한다.

```yaml
VLYTICS_OPENAI_PROXY_URL: http://openai_egress:8081
```

홈 Compose에 아래 내부 중계 서비스와 외부 연결망을 추가한다. 공용 production Compose는 변경하지 않는다.

```yaml
services:
  openai_egress:
    <<: *backend-service
    restart: unless-stopped
    command: ["python", "-m", "vlytics.ops.provider_egress"]
    environment:
      VLYTICS_EGRESS_BIND: 0.0.0.0:8081
      VLYTICS_EGRESS_PROVIDER: openai
      PYTHONDONTWRITEBYTECODE: "1"
      PYTHONUNBUFFERED: "1"
    networks:
      private: {}
      openai_access:
        gw_priority: 1

networks:
  openai_access:
    driver: bridge
```

Provider factory는 Provider별 위 고정 내부 주소만 명시적 HTTPX proxy로 허용한다. 임의 proxy URL과 환경 proxy 자동 사용은 허용하지 않는다. 공용 `ProviderEgress.ps1` 검사는 활성 Provider 집합과 relay service·`VLYTICS_EGRESS_PROVIDER`·API key·proxy·전용 network·worker dependency가 정확히 일치하는지 검사하고, 비활성 Provider의 service/key/proxy를 거부한다. 현재 홈 Compose에는 OpenAI service/key/proxy/network만 선언한다.

향후 Anthropic·Gemini를 전환 또는 병행하려면 새 사용자 결정을 먼저 기록하고 해당 Provider의 설정·key·budget·model/version·registry hash와 fresh exact-subset evidence를 준비한다. 그 뒤 Provider별 relay·전용 network·worker key/proxy/dependency를 추가한다. relay나 transport 기능 코드를 교체할 필요는 없다. 활성 Provider 전체의 registry 총예산은 현재 월 10,000원(KRW) 한도 안에 있어야 한다.

## 구현 후에도 마지막에 확인할 것

연결 코드 적용은 실제 배포·유료 호출과 별개다. 실제 키는 사용자가 마지막에 넣는다. 그때 계정 접근·응답 모델 버전, 가격·환산계수, 최종 Provider 파일 해시와 일치하는 dry-run 증거, 홈 서버의 Docker·시계·SSH·네트워크 격리를 검증한다. 증거가 없으면 운영 활성화는 계속 차단한다.

초기 자동 승인 검토는 외부 전송 범위의 구체적인 사용자 승인 근거가 부족해 배선을 거절했다. 이후 사용자가 OpenAI 연결 진행을 직접 승인해 이 승인 차단은 해소됐고 저장소 배선을 적용했다. 이 승인은 실제 key·호출·배포 성공이나 Anthropic·Gemini 활성화를 뜻하지 않는다.
