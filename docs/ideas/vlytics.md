- Status: Idea
- Version: 0.1
- Created: 2026-09-19
- Updated: 2026-09-19
- Tags: vlytics, volleyball, v-league, kovo, analytics, prediction, ai, sports-data

# Vlytics

## 1. 한 줄 요약

KOVO V리그 남자부·여자부의 경기·팀·선수 기록을 장기간 미러링하고, 통계 모델과 여러 AI 모델로 매일 경기를 분석·예측한 뒤 실제 결과로 성능을 지속 검증하는 개인용 V리그 데이터·예측 플랫폼이다.

궁극적으로는 예측 성능을 충분히 검증·개선한 뒤 공개 서비스로 확장하는 것을 목표로 한다.

## 2. 배경 및 문제

V리그는 경기, 세트, 선수 단위로 정형화된 기록이 풍부해 통계 분석과 예측 실험에 적합하다. KOVO DB Bank에는 경기 결과, 세트별 점수, 팀 기록, 선수별 경기 및 세트 기록 등 분석 기반으로 활용할 수 있는 데이터가 존재한다.

하지만 KOVO DB Bank를 분석 시점마다 직접 조회하면 장기간 데이터 축적, 반복적인 파생 통계 계산, 예측 당시 데이터 보존, 모델별 성능 비교 등에 제약이 있다.

따라서 원천 데이터를 자체 DB에 지속 축적하고, 분석/예측 계층과 표현 계층을 분리하여 장기간 실험 가능한 플랫폼을 구축한다.

## 3. 목표

- V리그 남자부·여자부의 과거 및 현재 데이터를 자체 데이터 자산으로 축적한다.
- 경기·팀·선수·세트 데이터를 기반으로 재사용 가능한 통계 Feature를 생성한다.
- 통계 모델과 AI 모델을 이용해 예정 경기를 자동 분석·예측한다.
- 승패, 세트스코어, 핸디캡, 언더/오버 예측을 기록한다.
- 예측 당시 입력과 결과를 변경 불가능한 Snapshot으로 보존한다.
- 실제 경기 종료 후 예측 결과를 자동 평가한다.
- 모델, 프롬프트, Feature 변경에 따른 성능 변화를 장기간 측정한다.
- 개인용으로 검증을 시작하고, 충분한 예측 성능과 안정성이 확보되면 공개 서비스를 검토한다.

핵심 성공 기준은 단순히 AI 분석을 제공하는 것이 아니라 **실제 경기 결과를 통해 예측 성능이 지속적으로 측정되고 개선되는 것**이다.

## 4. 대상 사용자

### MVP
- 운영자 본인
- V리그 경기 데이터와 AI/통계 예측 모델을 실험하고 성능을 개선하는 용도

### Vision
- V리그 통계와 경기 분석에 관심 있는 사용자
- 데이터 기반 경기 Preview와 예측 결과를 확인하려는 사용자

공개 서비스의 구체적인 대상 사용자와 제공 범위는 MVP 검증 후 결정한다.

## 5. 핵심 아이디어

Vlytics는 다음 3계층을 중심으로 구성한다.

### V-Mirror

KOVO 데이터를 자체 DB에 미러링하는 데이터 계층이다.

책임:
- 과거 시즌 Backfill
- 현재 시즌 지속 동기화
- 경기/팀/선수/세트 기록 저장
- 원본 데이터 Snapshot 보존
- 변경 및 정정 데이터 동기화

V-Mirror에는 가능한 한 KOVO에서 관측된 사실 데이터만 저장하며 분석 결과나 AI 판단을 섞지 않는다.

### V-Engine

V-Mirror와 별도로 제공되는 Market 데이터를 입력으로 사용하는 분석/예측 계층이다.

구성:
1. Feature Engine
2. Statistical Predictor
3. AI Analysis / Prediction
4. Market Evaluator
5. Result Evaluator

AI는 통계 계산을 대신하지 않는다. 먼저 코드/통계 모델이 구조화된 Feature를 계산하고, AI에는 정제된 Context를 제공한다.

### Vlytics Web

V-Mirror와 V-Engine의 결과를 사용자에게 보여주는 표현 계층이다.

MVP에서는 당일 경기 분석과 예측 기록/성능 확인을 중심으로 구성한다.

## 6. 주요 사용 흐름

### 당일 경기 준비

1. V-Mirror가 KOVO 일정에서 당일 V리그 경기를 확인한다.
2. 시스템은 고정된 경기 수를 가정하지 않고 실제 등록된 일정을 기준으로 동작한다.
3. 최신 경기·선수·팀 데이터를 동기화한다.
4. V-Engine에서 필요한 Feature를 준비한다.

### 경기 시작 1시간 전

1. V-Mirror 데이터를 최신화한다.
2. 가능한 선수/출전 관련 정보를 반영한다.
3. 외부에서 적재된 Market Snapshot을 조회한다.
4. 해당 시점의 Feature Snapshot을 고정한다.
5. Statistical Predictor를 실행한다.
6. 설정된 AI Provider를 실행한다.
7. 승패/세트스코어/핸디캡/O-U 예측을 저장한다.
8. 분석 결과를 Vlytics Web에 표시한다.

경기 1시간 전 선수 명단이 확정되지 않은 경우에도 분석은 실행하며, 해당 상태를 명시한다.

### 경기 종료 후

1. V-Mirror가 최종 경기 결과와 기록을 동기화한다.
2. Result Evaluator가 각 Prediction과 실제 결과를 비교한다.
3. 모델별 성능 지표를 갱신한다.
4. Prediction History와 Performance Dashboard에 반영한다.

## 7. 핵심 기능

### 7.1 KOVO 데이터 미러링

V리그 남자부·여자부 데이터를 가능한 과거 시즌까지 Backfill하고 이후 신규 경기를 지속 동기화한다.

초기 후보 데이터:
- 시즌
- 팀
- 선수
- 시즌별 로스터
- 경기
- 세트
- 경기별 선수 기록
- 세트별 선수 기록
- 경기별 팀 기록
- Raw Snapshot

초기 테이블 후보:
- seasons
- teams
- players
- team_rosters
- matches
- match_sets
- player_match_stats
- player_set_stats
- team_match_stats
- raw_snapshots

실제 스키마는 KOVO 데이터 수집 구조를 추가 조사한 후 확정한다.

### 7.2 Feature Engine

원천 기록으로부터 분석에 필요한 파생 Feature를 계산한다.

초기 후보:
- 시즌 성적
- 최근 5/10경기
- 최근 N세트
- 홈/원정 성적
- 상대전적
- 세트 승률
- 공격 성공률/효율
- 블로킹
- 서브
- 리시브
- 범실
- 선수 공격 점유율
- 선수 최근 Form
- 시즌 평균 대비 최근 변화량

최근 성적과 시즌 평균의 차이처럼 절대값뿐 아니라 변화량을 주요 Feature로 사용한다.

### 7.3 Statistical Prediction

AI와 독립된 Baseline 모델을 운영한다.

예측 범위:
- 경기 승패 및 확률
- 예상 세트스코어
- 핸디캡
- 언더/오버

초기에는 설명 가능하고 검증하기 쉬운 Baseline부터 시작하며 향후 ML 모델로 확장할 수 있다.

### 7.4 Multi-AI Provider

AI 모델은 특정 업체에 종속시키지 않는다.

지원 목표:
- OpenAI GPT
- Anthropic Claude
- Google Gemini
- 향후 추가 Provider

공통 PredictionProvider 인터페이스를 두고 설정만으로 Provider/Model을 교체할 수 있게 한다.

단일 모델 실행과 여러 모델 동시 실행을 모두 지원한다.

각 모델은 동일한 Feature Snapshot을 입력받아 독립적인 분석과 예측을 생성하며 모델별 결과를 별도로 저장한다.

AI 출력 후보:
- 예상 승패
- 예상 세트스코어
- 핸디캡 판단
- 언더/오버 판단
- Confidence
- 핵심 근거
- Risk Factors
- 경기 분석 설명

### 7.5 Market Evaluation

Market 데이터의 수집 방법은 Vlytics 범위에서 제외한다.

외부 프로세스에 의해 DB에 다음 정보가 적재되어 있다고 가정한다.
- 승패 기준/배당
- Handicap Line
- O/U Line
- Odds

Stat/AI 모델은 우선 경기 데이터만으로 독립 예측을 수행하고 이후 Market Evaluator에서 모델 결과와 시장 기준을 비교한다.

이를 통해 모델이 시장 데이터를 단순 추종하는 문제를 방지하고 독립적인 예측력을 평가할 수 있도록 한다.

### 7.6 Prediction Snapshot

경기 시작 1시간 전 생성된 예측은 경기 후 수정하지 않는다.

예측마다 다음 정보를 추적할 수 있어야 한다.
- match
- generated_at
- prediction_stage
- provider/model/model version
- prompt version
- feature snapshot
- 승패 예측 및 확률
- 세트스코어
- Handicap 예측
- O/U 예측
- Confidence
- AI 분석
- Token Usage
- Latency
- 당시 Market Snapshot

이를 통해 모델 변경, 프롬프트 변경, Feature 변경의 효과를 사후 비교한다.

### 7.7 Prediction History

매일 생성되는 모든 예측을 누적한다.

필터 후보:
- 기간
- 남자부/여자부
- 팀
- Provider
- Model
- Prediction Type
- Prompt Version

동일 경기에 여러 AI 모델을 실행했다면 각각 독립된 예측 기록으로 관리한다.

### 7.8 Performance Dashboard

Vlytics의 핵심 운영 화면이다.

초기 표시 지표:
- 전체 승패 적중률
- 최근 N경기 적중률
- 남자부/여자부 적중률
- 모델별 적중률
- 핸디캡 적중률
- O/U 적중률
- 세트스코어 적중률
- 예측 수

확률 예측 품질을 평가하기 위해 단순 적중률 외에도 다음 지표 도입을 검토한다.
- Brier Score
- Log Loss
- Calibration

궁극적인 목적은 이 Dashboard를 통해 어떤 모델/Feature/Prompt가 실제 예측 성능 개선에 기여했는지 확인하는 것이다.

## 8. 범위

### MVP

- V리그 남자부·여자부 지원
- 가능한 범위의 과거 시즌 데이터 Backfill
- 현재 시즌 자동 동기화
- V-Mirror 구축
- 팀/선수/세트 기반 기본 Feature
- 경기 시작 1시간 전 자동 분석
- 승패/세트스코어/핸디캡/O-U 예측
- Baseline Statistical Predictor
- GPT/Claude/Gemini 교체 가능한 Provider 구조
- 단일 또는 Multi-AI 실행
- Prediction Snapshot 저장
- 경기 종료 후 자동 평가
- Prediction History
- Performance Dashboard
- 개인용 Vlytics Web

### Later

- 고급 ML Prediction Model
- 모델별 가중 Ensemble
- 상황별 최적 모델 자동 선택
- Feature 자동 실험/평가
- Post-Match AI Report 고도화
- 팀/선수 상세 탐색 페이지 고도화
- 공개 서비스용 사용자/권한/운영 기능
- 충분한 검증 이후 공개 서비스 전환

### Out of Scope

- Market/Odds 데이터 자체 수집 방식
- MVP 단계의 유료 서비스
- AI가 원천 통계 계산을 직접 수행하는 구조
- 검증되지 않은 예측을 성능이 입증된 것처럼 제공하는 것

## 9. 데이터 및 연동

### KOVO

KOVO DB Bank를 주요 원천 데이터로 사용한다.

KOVO 페이지를 실시간 분석 API처럼 직접 소비하기보다 V-Mirror에 적재한 데이터를 내부 분석의 기준으로 사용한다.

자동수집 허용 범위, 데이터 이용조건 및 공개 서비스 시 재배포 가능 범위는 별도 확인이 필요하다.

### Market Data

외부 수집 방식으로 DB에 적재되어 있다고 가정한다.

V-Mirror와 분리된 Source로 관리한다.

### AI Provider

Provider Adapter를 통해 GPT, Claude, Gemini를 연결한다.

Provider와 Model은 설정으로 변경할 수 있고 여러 Provider를 동일 경기에서 병렬 사용 가능하도록 설계한다.

## 10. 결정된 사항

- 프로젝트명은 **Vlytics**다.
- 구조는 **V-Mirror → V-Engine → Vlytics Web**의 3계층을 기본으로 한다.
- KOVO 원천 데이터와 분석/예측 결과를 분리한다.
- 과거 V리그 데이터도 가능한 범위까지 Backfill한다.
- 남자부와 여자부를 모두 지원한다.
- 경기 수/요일을 하드코딩하지 않고 KOVO 실제 일정을 기준으로 처리한다.
- 최종 경기 전 분석은 경기 시작 **1시간 전** 실행한다.
- 예측 범위는 승패 + 세트스코어 + 핸디캡 + 언더/오버다.
- AI도 통계 모델과 별도로 자체 예측을 생성한다.
- Market 데이터 수집 방법은 별도로 해결하며 시스템에서는 DB에 존재한다고 가정한다.
- Market 데이터는 모델의 독립 예측 후 비교/평가 단계에서 사용한다.
- GPT, Claude, Gemini를 포함한 Multi-Provider 구조로 설계한다.
- Provider/Model을 언제든 교체할 수 있어야 한다.
- 여러 AI 모델을 동일 경기에서 동시에 실행할 수 있어야 한다.
- 모든 일일 예측을 저장하고 실제 결과와 비교한다.
- 예측 기록과 모델별 성능을 볼 수 있는 Dashboard를 제공한다.
- MVP는 개인용으로 시작한다.
- 장기적으로 공개 서비스를 염두에 둔다.
- 궁극적인 운영 목표는 실제 검증된 예측 성능을 지속적으로 높이는 것이다.
- 공개 전환 기준의 구체적인 수치는 아직 확정하지 않는다.

## 11. 미결정 사항

- KOVO에서 실제 자동 수집 가능한 과거 시즌 범위
- 랠리 단위 데이터 확보 가능 여부
- KOVO 데이터 이용/재배포 정책
- V-Mirror의 최종 DB 스키마
- 선수 명단/라인업 데이터의 실제 공개 시점 및 확보 방법
- Statistical Baseline Model 선정
- AI 모델별 공통 입력 Context Schema
- AI Prediction의 Structured Output Schema
- Market DB Schema 및 Vlytics 연결 방식
- 분석/예측 결과용 Analytics DB의 물리적 구성
- 경기 종료 후 최종 기록 확정/재동기화 정책
- 공개 서비스 전환을 판단할 최소 경기 수와 성능 기준

## 12. 위험 요소 및 대응

### 데이터 품질

과거 기록 누락이나 KOVO 페이지 구조 변경이 발생할 수 있다.

대응:
- Raw Snapshot 보존
- 수집 검증
- Parser 버전 관리
- 재처리 가능한 구조 유지

### 데이터 누수

경기 이후 정보를 경기 전 Prediction에 포함하면 모델 성능 평가가 무의미해진다.

대응:
- T-1h Feature Snapshot 고정
- Prediction 변경 금지
- 입력 데이터 시점 기록

### AI 비교의 공정성

모델마다 다른 입력 데이터를 사용하면 Provider 성능 비교가 왜곡된다.

대응:
- 동일 Feature Snapshot 사용
- Prompt Version 기록
- Model Version 기록

### 적중률 과대평가

적은 경기 수에서 높은 적중률이 나올 수 있으며 단순 Accuracy만으로 확률 모델의 품질을 판단하기 어렵다.

대응:
- 표본 수 함께 표시
- Brier Score/Log Loss/Calibration 도입
- 기간별 성능 추적

### 공개 서비스 전환

개인 분석과 공개 서비스는 데이터 이용권한, 운영 안정성, 비용 요구가 달라진다.

대응:
- MVP에서는 개인용에 집중
- 공개 전환 전 별도 검증 단계 수행

## 13. 다음 작업

1. KOVO DB Bank의 시즌/경기/선수/세트 데이터 요청 구조를 상세 조사한다.
2. Backfill 가능한 시즌 범위와 요청량을 확인한다.
3. V-Mirror MVP 스키마를 확정한다.
4. KOVO Collector Prototype으로 한 시즌 일부를 실제 적재해 데이터 품질을 검증한다.
5. V-Engine Feature Schema를 정의한다.
6. Statistical Baseline Model 후보를 비교한다.
7. Multi-AI PredictionProvider 인터페이스와 Structured Output Schema를 설계한다.
8. Prediction/Evaluation DB Schema를 설계한다.
9. Prediction History 및 Performance Dashboard 화면 구조를 설계한다.
10. 충분한 과거 데이터로 Backtest를 수행해 MVP 예측 기준선을 확보한다.
