# Elo·홈 승률 기준선 실험 계약

작성일: 2026-09-20 · 구현 버전: `elo-home-win-v2`, `elo-match-v2`, `binary-probability-metrics-v2`

## 목적과 현재 상태

승패 모델은 홈팀 승률 고정 확률과 Elo를 최소 비교 기준으로 사용한다. 과거 조사에서 보고된 63.6%는 재현 대상 후보의 참고값이며 합격값이나 파라미터 선택 기준이 아니다. 실제 KOVO backfill은 OP-001과 OP-006이 해결되지 않아 실행하지 않았고, 현재 검증은 공개 합성 경기의 손계산 결과다.

## 입력과 시점 계약

각 `EloMatch`는 다음 내용을 함께 고정한다.

- 경기와 season, division, competition, stage
- 예측 cutoff와 예정 시작 시각
- schedule revision ID·번호·관측 시각·Raw SHA-256
- 0개 이상의 result revision ID·번호·관측 시각·경기 확정 시각·finality·Raw SHA-256
- availability policy, timing eligibility, finality policy
- 입력 버전과 franchise mapping 버전
- 홈·원정 season team ID와 검증된 franchise identity

Strict historical와 live prospective 입력은 schedule이 cutoff까지 관측됐고 예측 cutoff가 경기 시작보다 늦지 않아야 한다. 결과는 `final`이며 `finalized_at <= 다음 경기 cutoff`와 `observed_at <= 다음 경기 cutoff`를 모두 만족할 때만 이후 Elo에 반영한다. 경기 후 늦게 수집한 결과, 연기 경기의 아직 끝나지 않은 결과, cutoff 뒤 정정 revision은 해당 cutoff의 레이팅을 바꾸지 않는다.

Historical reconstruction은 별도 코호트다. 경기 확정 시각은 반드시 cutoff 이전이어야 하지만 과거 receipt가 없다는 정책의 한계 때문에 result `observed_at`은 제한하지 않는다. Reconstruction 결과를 strict/live 성능과 합치지 않는다.

최신 정정이 알려지거나 오래 지연된 결과가 도착하면 현재 cutoff에서 이용 가능한 최신 revision 집합을 경기 cutoff 순서로 재생한다. 이미 저장된 과거 예측은 바꾸지 않고 이후 예측 상태만 정정한다. 이 방식은 연기·지연·정정을 누락하거나 미래 revision을 앞당겨 쓰지 않는다.

## Elo 상태 API와 수식

`EloPredictor`는 세 단계를 분리한다.

1. `transition(match)`은 팀과 시즌 상태를 명시적으로 준비한다. 같은 입력 반복은 no-op이고 이전 cutoff로 돌아가면 실패한다.
2. `predict(match)`는 준비된 pre-match 상태만 읽는 순수 함수다. 반복 호출은 동일한 값이며 레이팅을 변경하지 않는다. Pre-match fingerprint는 이후 추가되는 result revision을 제외하므로 같은 schedule 입력에 결과 receipt를 붙여도 예측 identity는 바뀌지 않는다.
3. `update_observed_result(match, revision, known_at)`은 해당 시점 정책으로 선택되는 최신 final revision만 한 번 반영한다. 같은 revision 반복은 no-op이며 다른 correction을 기존 상태 위에 중복 적용하려 하면 replay를 요구한다.

남자부와 여자부는 별도 상태를 사용한다. 경기 전 홈 승리 확률과 경기 후 변화량은 다음과 같다.

```text
P(home) = 1 / (1 + 10 ^ ((R_away - R_home - home_advantage) / 400))
delta = K * set_margin_weight * (home_result - P(home))
R_home = R_home + delta
R_away = R_away - delta
```

기본 세트차 가중치는 3:0/0:3에서 1.25, 3:1/1:3에서 1.0, 3:2/2:3에서 0.75다. 시즌 시작 레이팅은 1500이며 검증된 franchise 연속성이 있을 때만 다음 식으로 이월한다.

```text
R_new = 1500 + (1 - season_regression) * (R_old - 1500)
```

franchise ID·mapping version·근거 중 하나라도 없으면 새 시즌은 1500에서 시작한다. 팀 코드나 표시 이름의 유사성으로 이월하지 않는다. 예측에는 적용한 franchise ID, mapping version, 레이팅 출처와 모델 version·parameter hash·seed를 기록한다.

## 코호트

아래 값 전체가 같은 행만 한 번의 파라미터 선택과 지표 계산에 사용한다.

- division
- availability policy
- competition
- stage
- timing eligibility
- result finality policy
- Elo input version
- franchise mapping version

단일 실험이나 지표 함수에 혼합 코호트를 전달하면 실패한다. Suite 함수만 코호트를 키별로 분리해 실행한다.

## 후보와 시간 순 실험

과거 피드백의 `K=32`, 홈 어드밴티지 `+15`, 시즌 회귀 `0.5`는 `reproduction_candidate()`로 제공한다. 기본 탐색은 다음 60개 조합을 동등하게 비교한다.

| 파라미터 | 후보 |
|---|---|
| K | 16, 24, 32, 40, 48 |
| 홈 어드밴티지 | 0, 15, 30, 45 |
| 시즌 회귀 | 0.2, 0.35, 0.5 |

`TemporalSplit`은 train·validation·test의 시작과 종료를 모두 timezone-aware 시각으로 요구하며 세 반개구간은 빈틈 없이 이어져야 한다.

```text
train      = [train_start, train_end)
validation = [validation_start, validation_end)
test       = [test_start, test_end)
```

각 후보는 train부터 validation까지 cutoff 순서로 walk-forward 예측한다. Validation 평가는 test 시작 전에 알려진 final result만 사용한다. Brier가 가장 낮은 후보를 선택하고 동률이면 Log Loss와 안정된 config ID 순서로 결정한다. 선택된 파라미터는 동결한 뒤 train부터 test까지 다시 walk-forward 실행한다. Test 결과는 후보 선택에 되먹이지 않는다.

홈 승률 기준선은 `train_end`까지 정책상 알려진 final result의 `홈 승리 수 / 완료 경기 수`다. 보고서에는 홈 승리 수, 표본 수, 사용한 result revision ID를 기록한다.

## 재현 manifest

실험 보고서는 다음을 포함한다.

- train·validation·test의 전체 시작/종료 시각
- canonical cutoff 순서와 0부터 시작하는 position
- 각 schedule revision ID·번호·관측 시각·Raw hash
- 각 result revision ID·번호·관측/확정 시각·finality·Raw hash
- franchise ID와 mapping version
- 전체 ordered manifest의 canonical JSON SHA-256
- 후보별 validation과 동결 test 지표에 사용한 result revision ID·Raw hash

입력 순서가 달라도 동일한 match 집합과 내용이면 manifest 순서와 hash가 같다. revision, hash, 시각, version 중 하나라도 달라지면 hash가 달라진다.

## 지표와 결측

완료 결과 `y`와 홈 승리 확률 `p`에 대해 다음 값을 평균한다.

```text
Brier = (p - y) ^ 2
Log Loss = -(y * ln(p) + (1-y) * ln(1-p))
```

Architecture 계약에 따라 Log Loss 계산에만 `p`를 `[1e-12, 1-1e-12]`로 제한한다. Brier는 원래 확률을 사용한다. 보조 accuracy는 `p >= 0.5`를 홈 승리로 판정한다.

결과가 없는 일정도 삭제하지 않는다. `scheduled`, `predicted`, `result_available`, `evaluated`, `coverage = evaluated / scheduled`, `missing_predictions`, `missing_results`와 사유별 수를 함께 보고한다.

## 합성 검증

실자료 없이 다음을 단위 테스트로 검증한다.

- 동일 transition과 result update는 멱등이고 `predict()`는 상태를 바꾸지 않는다.
- 홈 어드밴티지 0, 동률 1500의 첫 확률은 0.5다.
- K=32인 3:0 홈 승리의 변화량은 `32 × 1.25 × (1 - 0.5) = 20`이다.
- 1520/1480을 회귀 0.5로 검증된 franchise에 이월하면 1510/1490이며, 미검증 팀은 1500이다.
- Strict는 cutoff 뒤 correction을 사용하지 않고 reconstruction만 별도 정책으로 최신 revision을 선택한다.
- 연기 경기 결과는 확정 전 target에 반영되지 않고, 늦은 correction은 알려진 이후 상태를 replay한다.
- 확률 0과 1의 반대 결과 Log Loss는 `-ln(1e-12)`다.
- test 결과를 바꿔도 validation에서 선택된 config ID는 바뀌지 않는다.
- manifest와 전체 split 경계, revision/hash lineage가 직렬화된다.
- competition, stage, timing, finality, availability, input·mapping version이 다른 코호트는 섞이지 않는다.

실제 데이터 실험은 backfill과 검증된 franchise mapping이 준비된 뒤 같은 API로 실행한다. 63.6%와 다르면 결과를 그대로 보고하고 데이터 범위·revision·식별자·가용성 정책 차이를 조사한다.
