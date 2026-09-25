# 6범주 세트 결과 기준선

작성일: 2026-09-20 · 구현 버전: `independent-sets-v2`, `set-score-six-v1`, `set-outcome-metrics-v2`

## 모델 계약

세트 결과 기준선은 Elo 승패 예측 산출물을 입력으로 받는다. 입력은 단순 확률이 아니라 경기 ID, 일정 revision, 예측 cutoff, 전체 cohort, Elo 모델 버전과 설정 ID를 포함한 `EloPrediction`이어야 한다. 세트 모델은 이를 경기와 대조하고 불일치하거나 cutoff 뒤 정보를 사용한 산출물을 거부한다. 순차 holdout은 같은 검사를 거친 Elo 산출물을 내부에서 walk-forward 방식으로 생성한다.

남자부와 여자부를 포함한 전체 `CohortKey` 차원을 서로 섞지 않는다. 차원은 division, availability policy, competition, stage, timing eligibility, result finality policy, input version, franchise mapping version이다. 한 번의 적합에는 하나의 완전한 cohort만 허용한다. 학습 구간의 5세트 경기에서 홈팀의 5세트 승률 `p5`를 Beta(1, 1) 사전분포로 평활하며, 5세트 경기가 없으면 `p5=0.5`다. 1~4세트의 공통 홈 승률 `p`는 아래 6범주 분포의 홈 승 주변확률이 입력 Elo 승률과 일치하도록 단조 방정식을 이분법으로 역산한다.

홈 관점 출력 순서는 항상 `3:0, 3:1, 3:2, 2:3, 1:3, 0:3`이다.

```text
P(3:0) = p³
P(3:1) = 3p³(1-p)
P(3:2) = 6p²(1-p)²p5
P(2:3) = 6p²(1-p)²(1-p5)
P(1:3) = 3(1-p)³p
P(0:3) = (1-p)³
```

각 예측은 Elo prediction ID·모델 버전·설정 ID와 적합된 `p5` artifact ID·학습 cohort·as-of 시각·결과 revision manifest 해시를 보존한다. 이 계보와 기반 설정은 세트 예측 `config_id`에도 포함되므로 상위 Elo 산출물이나 학습 결과가 달라지면 식별자가 달라진다. `winner`와 `set_score` target provenance는 동일한 통계 계보를 공유한다. 공동 점수 예측도 이 계보를 이어받는다.

`prediction-v1`은 JSON Schema 검증 뒤 서버 의미 검증을 거친다. 의미 검증은 확률 합을 허용오차 `1e-6` 이내로 확인하고, 승패 주변확률 일치, 고정된 여섯 결과 순서, capability·필드·provenance 대응, producer·snapshot 일치, 통계 계보 일치를 검사한다. 공동 점수 capability가 있으면 resolver hook을 통해 소유 산출물, snapshot, 세트 주변분포 참조까지 대조한다.

## 순차 holdout과 지표

무작위 분할은 사용하지 않는다. train 결과로 validation용 `p5`를 적합하고, train+validation 결과로 test용 `p5`를 다시 적합한 뒤 test 전체에서 고정한다. 각 적합 artifact는 해당 경계의 as-of 시각 이전에 알려진 최종 결과 revision만 manifest에 포함한다. Elo 확률도 경기 순서대로 상태를 갱신해 생성하며, 외부의 위치 기반 확률 배열은 받지 않는다. 전체 cohort가 다른 행은 별도 보고서로 나눈다.

보고 항목은 다음과 같다.

- Set RPS: 약한 홈 결과부터 강한 홈 결과 순서인 `[0:3, 1:3, 2:3, 3:2, 3:1, 3:0]`에서 다섯 누적 경계의 제곱 오차 평균
- 세트스코어 적중률: 최대 확률 범주가 여러 개면 실제 범주가 그 집합에 속할 때 `1 / 동률 범주 수`를 부여하는 `fractional-tied-maxima-v1`
- 승패 calibration: 고정 0.2 폭 구간별 평균 예측 승률, 실제 홈 승률, 표본 수와 고정 95% Wilson 구간인 `wilson-95-v1`
- 표본과 coverage: `scheduled`, `predicted`, `result_available`, `evaluated`, 결측 예측·결과와 사유
- 독립 가정 오차: 각 세트 결과의 `관측 빈도 - 평균 예측확률`, 여섯 절대 잔차의 평균과 최댓값

현재 공개 저장소에는 실제 KOVO backfill이 없으므로 실성능을 주장하지 않는다. 단위 테스트의 합성 순차 fixture는 RPS·calibration·세트스코어 적중률·표본 수·coverage, 학습 경계의 동결, 전체 cohort 분리, Elo 식별자 및 계보, 동률 점수와 Wilson 구간을 검증한다.

## 독립 세트 가정의 한계

이 기준선은 1~4세트가 동일한 `p`로 조건부 독립이라고 가정한다. 실제 경기는 로테이션, 선발·교체, 피로, 부상, 전술 변경, 세트별 홈 효과와 경기 중 상태에 따라 세트 확률이 달라지고 연속 세트 결과가 상관될 수 있다. `p5`도 현재는 팀 전력 대신 cohort별 홈 승률 하나만 사용하므로 5세트 표본이 적을 때 개별 경기 차이를 설명하지 못한다.

범주별 빈도 잔차는 이 근사에서 생긴 전체 적합 오차를 드러내지만 독립 가정만의 인과적 오차를 분리하지는 않는다. 실제 자료가 준비되면 cohort별로 잔차와 RPS를 보고하고, 세트 순서·팀 전력·로테이션을 반영한 후보와 동일한 미래 holdout에서 비교한다. 그 검증 전에는 합성 결과를 실제 예측력 근거로 사용하지 않는다.
