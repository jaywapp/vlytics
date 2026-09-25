# 결과 평가와 공정한 코호트 집계

작성일: 2026-09-20 · 구현 버전: `result-evaluator-v1`, `performance-cohort-v1`, `performance-v1`

## 불변 평가 단위

평가는 `prediction_id + result_revision_id + evaluator_version + cohort_policy_version`마다 한 행을 추가한다. 같은 조합의 재실행은 저장된 지표와 정산 내용이 동일할 때 기존 행을 반환하고, 내용이 다르면 충돌로 중단한다. 결과 정정은 기존 평가를 수정하지 않고 `corrected` result revision을 가리키는 새 평가를 만든다.

단일 경기 평가에는 원본 홈 승률, 실제 홈 승패, binary Brier, Log Loss, 승패 적중, 세트 RPS와 정확 세트스코어 적중을 저장한다. Log Loss만 `epsilon=1e-12`로 계산 시 제한하며 Brier에는 원본 확률을 사용한다. 정확 세트스코어의 최대 확률이 동률이면 실제 범주가 동률 집합에 포함된 경우 `1 / 동률 범주 수`를 부여한다.

Market 정산은 prediction cutoff에 적격하다고 판정된 정확한 snapshot에만 생성한다. `missing`, `stale`, `late`, `unsupported` 상태는 정산 행을 만들지 않으며 상태와 개수만 남긴다. Market이 없는 경기도 일반 AI·통계 평가는 그대로 계산한다. Market 적중률 분모는 `win + loss`이고 push·void와 가용성 제외 상태는 별도 개수로 보고한다.

## 코호트와 짝비교

집계 키는 다음 차원을 모두 포함한다.

- 남자부/여자부
- 대회와 stage(정규리그, 플레이오프, 챔피언결정전, 기타)
- provider, model version, prompt version, feature version
- `live_prospective`, `historical_point_in_time`, `historical_reconstruction`
- `on_time`, `reconstructed`, `diagnostic`
- 결과 finality(`provisional`, `final`, `corrected`, `void`)

모델별 `scheduled`, `predicted`, `result_available`, `evaluated`, 결측 예측·결과와 실패 사유를 각각 센다. `coverage = evaluated / scheduled`이며 빈 코호트의 coverage는 0, 계산할 수 없는 성능 지표는 `null`이다. Calibration은 폭 0.2의 고정 구간과 `fixed-width-0.2-wilson-95-v1` Wilson 95% 구간을 사용하고 빈 구간은 생략한다.

AI−baseline 비교는 match, schedule revision, feature snapshot, cutoff가 모두 같은 교집합에서만 계산하며 양쪽 평가가 같은 result revision을 참조해야 한다. 경기별 손실 차이의 평균을 보고하고 음수는 AI의 손실이 더 작다는 뜻이다. 표준오차는 표본표준편차를 사용한 `SD / sqrt(n)`이다. `n=0`이면 평균과 표준오차가 모두 `null`, `n=1`이면 평균만 제공한다. 각 모델의 개별 `n`과 paired `n`, 양쪽 결측 및 result revision 불일치 수를 함께 노출한다.

## 검증 범위와 한계

단위 테스트는 손계산 가능한 확률로 Brier·Log Loss·RPS·정확 적중·Calibration을 검산하고, 확률 0/1, n=0/n=1, push/void 분모, 결과 정정, 재실행 멱등성, 개별 표본과 짝표본 차이를 확인한다. `performance-v1` JSON Schema는 서버가 제공하는 버전·코호트·coverage·지표·불확실성·짝비교 형태를 고정한다.

정규 95% CI는 경기 독립을 가정한 참고치다. 실제 표본이 축적되면 사전 지정한 날짜/라운드 block bootstrap을 별도 버전으로 추가해야 한다. 현재 합성 fixture 결과는 실제 모델 성능 근거가 아니다.
