# 공동 점수 분포 기준선

작성일: 2026-09-20 · 구현 버전: `conditional-rally-score-v1`, `joint-score-v1`, `joint-score-metrics-v1`

## 모델과 규칙 계약

공동 점수 모델은 TASK-009의 6범주 세트 결과 분포와 1~4세트 홈 승률, 5세트 홈 승률을 입력으로 받는다. 출력은 홈 관점의 `P(최종 세트스코어, 홈 총득점, 원정 총득점)`이며 승패, 정확 세트스코어, 점수 O/U, 점수 핸디캡을 모두 이 분포에서 주변화한다. 점수 핸디캡은 총점 분포로 추정하지 않는다.

현재 확인된 규칙 ID는 `kovo-rally-best-of-five-v1`이다. 1~4세트는 25점, 5세트는 15점이 목표이며 모두 2점 차로 끝난다. 예측 호출은 mirror에서 검증된 시즌·부문별 규칙 identity를 요구한다. artifact에는 규칙 ID뿐 아니라 시즌, 부문, mirror 규칙 artifact SHA-256, 검증 시각을 저장한다. 새 시즌에서 목표 점수나 종료 규칙이 바뀌거나 특례가 발견되면 기존 ID의 뜻을 바꾸지 않고 새 규칙을 등록해야 한다. 확인되지 않은 시즌을 현재 규칙으로 자동 대체하지 않는다.

세트 홈 승률 `p_set`을 만족하는 홈 랠리 승률 `r`은 단조 방정식을 이분법으로 역산한다. 목표 점수가 `T`일 때 듀스 전 홈 승리 스코어 `T:k`, `0 ≤ k ≤ T-2`의 확률은 다음과 같다.

```text
C(T+k-1, k) r^T (1-r)^k
```

`T-1:T-1`에 도달한 뒤에는 두 랠리 단위로 승패가 결정된다. 홈의 듀스 이후 `T+1+j:T-1+j` 확률은 도달 확률에 `(2r(1-r))^j r²`을 곱한다. 원정도 대칭이다. 이 무한 꼬리는 설정된 `max_deuce_cycles`에서 자르고 남은 조건부 분포를 정규화한다. 각 세트 승자별 누락 꼬리 질량, 경기 전체 누락 질량 상한, 최대 조건부 꼬리 질량을 artifact에 기록한다.

최종 세트스코어별로 필요한 일반 세트 홈승·원정승 개수와 5세트 승자를 고정한 뒤 조건부 세트 점수 분포를 합성곱한다. 예를 들어 `3:2`는 일반 세트 홈승 2개, 일반 세트 원정승 2개, 홈이 이긴 5세트 1개다. 기본 `exact` 방식은 이 합성곱을 직접 계산하므로 6범주 세트 주변분포가 입력과 부동소수 허용오차 안에서 일치한다.

`monte_carlo` 방식은 더 큰 향후 support를 위한 선택지다. Python RNG에 고정 seed를 사용하고 역누적 표본 추출 순서를 `inverse-cdf-python-random-v1`로 버전 관리한다. `random_seed`, 설정 표본 수, 실제 `sample_count`, sampler version과 최악의 이항 95% 표준오차 상한 `0.98 / sqrt(n)`을 저장한다. 같은 입력·설정·seed는 byte-equivalent artifact를 만든다. Monte Carlo의 표본오차와 듀스 truncation 오차는 별도 항목이다. 생성 후 세트 주변분포를 원천 세트 예측과 다시 비교하며, 명시된 최대 주변오차를 넘으면 point capability를 발행하지 않는다.

## 계약과 네 예측

[`joint-score-v1.schema.json`](../../contracts/joint-score-v1.schema.json)은 규칙, 생성 설정, 오차 진단, 원천 세트분포, 공동 확률 원자와 네 종류 주변분포를 저장한다. 원천 세트 예측의 match·schedule revision·cutoff와 소유 variant/snapshot, upstream Elo prediction/model/config, P5 fitted artifact·training cohort/as-of/result manifest도 함께 저장한다. 이 전체 문서가 canonical distribution ID의 입력이므로 원천 계보가 달라지면 ID도 달라진다. JSON Schema가 표현하지 못하는 확률 합, 주변분포 재계산, 배구 점수 도달 가능성, canonical hash는 서버 의미 검증 대상임을 `$comment`에 명시했다.

`prediction-v1`의 기존 세트 모델 lineage 필드는 유지한다. 공동 점수 예측은 네 capability를 모두 선언하고 서버 소유 `joint_score_distribution_ref`와 target별 provenance를 제공한다. contract의 variant와 snapshot은 저장된 공동분포에서 파생하며 호출자가 다른 값을 넣으면 거부한다. point capability가 있으면 해당 provenance와 공동분포 참조가 반드시 있어야 한다.

라인 계산은 공동분포의 정수 support를 직접 세 갈래로 나눈다.

- 점수 O/U: `home_points + away_points`가 line보다 큰지, 같은지, 작은지 계산한다.
- 홈 점수 핸디캡: `home_points - away_points + home_handicap`이 0보다 큰지, 같은지, 작은지 계산한다.
- 정수 line에는 push 질량이 존재할 수 있다. 0.5 line의 push는 0이다.

## 순차 holdout과 보고

`run_joint_score_holdout`은 기존 `TemporalSplit`과 동일한 시간 경계를 사용한다. split 밖의 row는 제외하지 않고 오류로 보고한다. validation 예측은 train 종료 시각과 그 이전 결과 manifest를, test 예측은 validation 종료 시각과 train+validation 결과 manifest를 사용해야 한다. 각 기간의 set model/config/P5 artifact/upstream Elo model·config 계보가 동결됐는지 검사하며 match, schedule revision, cutoff, cohort 불일치와 cutoff 이후 학습을 거부한다. 설정 증거에는 당시 사용 가능한 development match ID와 canonical manifest, 검증 규칙 artifact를 저장한다. `run_joint_score_holdout_suite`는 남녀부와 availability policy를 별도 cohort로 유지한다.

보고 항목은 다음과 같다.

- 공동분포 Log Loss와 실제 공동 원자의 평균 확률
- 총점 분포와 득실차 분포의 discrete CRPS
- 예정·예측·결과 있음·평가 표본 수, coverage와 결측 사유
- 관측 결과가 finite support 밖에 있는 횟수
- 예측 support의 규칙 위반 수
- 평균 듀스 꼬리 오차 상한과 최대 원천 세트 주변오차

공개 저장소에는 실제 KOVO 세트별 점수 holdout이 아직 없다. 단위 테스트는 남녀부를 각각 train 2경기, validation 2경기, test 2경기로 분리한 합성 fixture를 사용한다. 각 부문 test `n=2`, coverage 1, 규칙 위반 0과 고정 설정을 검증한다. 이 합성 결과는 실제 예측 성능 주장이 아니다. 실제 세트별 점수가 적재되면 남녀부·strict/reconstruction·대회별 `n`, Log Loss, 두 CRPS와 out-of-support 비율을 같은 보고서에 기록해야 한다.

## 한계

현재 조건부 점수 모델은 일반 세트와 5세트 각각에서 랠리 승률이 일정하다고 가정한다. 로테이션, 서브권, 선수 교체, 세트 진행 중 피로와 전술 변화는 반영하지 않는다. 최종 세트 결과에 조건부로 점수를 생성하므로 TASK-009의 세트 결과 구조는 보존하지만 실제 점수와 세트 승패 사이의 팀별 상관을 모두 학습한 모델은 아니다.

듀스 꼬리는 확률 상한을 기록한 뒤 finite support로 정규화한다. 관측 점수가 support 밖이면 확률을 임의로 채우지 않고 out-of-support로 보고한다. 실제 자료가 준비되면 `max_deuce_cycles`가 관측 최대점을 덮는지 확인하고, 남녀부별 랠리·점수 보정 후보를 새로운 미래 holdout에서 비교한다.
