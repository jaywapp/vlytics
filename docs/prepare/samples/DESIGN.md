---
name: Vlytics 경기 브리핑
description: 동일한 경기 입력에서 모델별 확률, 근거, 불확실성과 기록 출처를 차분하게 읽는 운영 화면
colors:
  background: "#edf2f8"
  surface: "#ffffff"
  ink: "#182d4b"
  muted: "#50647d"
  line: "#ccd9e8"
  accent: "#2456a0"
  wash: "#e1ebf8"
  warning-text: "#704619"
  warning-background: "#f9ecd9"
  dark-background: "#172023"
  dark-surface: "#202c30"
  dark-ink: "#eef4f5"
  dark-muted: "#b4c7cc"
  dark-line: "#43585e"
  dark-accent: "#9abffc"
  dark-wash: "#2c4045"
  dark-warning-text: "#ffe0ad"
  dark-warning-background: "#4e3e27"
typography:
  display:
    fontFamily: "Segoe UI, Malgun Gothic, sans-serif"
    fontSize: "clamp(28px, 3vw, 44px)"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "Segoe UI, Malgun Gothic, sans-serif"
    fontSize: "clamp(28px, 3vw, 42px)"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Segoe UI, Malgun Gothic, sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Segoe UI, Malgun Gothic, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
  label:
    fontFamily: "Segoe UI, Malgun Gothic, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.6
    letterSpacing: "normal"
  numeric:
    fontFamily: "Consolas, Malgun Gothic, monospace"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
rounded:
  chart-bar: "3px 3px 0 0"
  status: "5px"
  control: "6px"
  container: "12px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "18px"
  xl: "24px"
  section: "32px"
  layout: "42px"
components:
  match-button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "18px"
  match-button-active:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "18px"
  select:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  status:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.status}"
    padding: "3px 9px"
  warning-status:
    backgroundColor: "{colors.warning-background}"
    textColor: "{colors.warning-text}"
    typography: "{typography.label}"
    rounded: "{rounded.status}"
    padding: "3px 9px"
---

# Design System: Vlytics 경기 브리핑

## Overview

**Creative North Star: "차분한 경기 판독지"**

Vlytics는 한 경기의 확률에서 근거, 불확실성, 입력 출처까지 순서대로 읽는 운영 도구다. 넓고 옅은 청색 바탕 위에 짙은 남청색 정보와 선명한 청색 선택 상태를 배치해, 많은 수치가 있어도 판단의 흐름을 잃지 않게 한다.

이 기록은 사용자가 선택한 UC-008 B, sample2 경기 분석 방향을 기준으로 한다. 현재 시각 근거는 정적 HTML/CSS/JavaScript 프로토타입이며, 제품 구현은 React/TypeScript로 옮기되 같은 정보 위계와 토큰을 유지한다. sample1의 운영 데스크와 sample3의 실험 워크벤치는 비교 이력이며 이 디자인 시스템에 색상이나 구성 규칙을 공급하지 않는다.

별도 이미지 자산은 필요하지 않다. 이 화면의 핵심은 경기 데이터, 확률 분포, 상태, 근거와 provenance를 의미 구조로 보여 주는 것이며 장식 이미지가 판단을 돕지 않는다.

**Key Characteristics:**

- 한 경기씩 깊게 읽는 좁은 경기 레일과 넓은 브리핑 본문
- 옅은 청색 면, 짙은 남청색 본문, 절제된 선명 청색 상호작용
- 확률·Snapshot·시간에 탭형 숫자를 적용한 감사 가능한 데이터 표현
- 명단 미확정, Market 미수신, 모델 실패를 숨기지 않는 상태 언어
- 700px 이하에서 세로 흐름으로 전환되는 모바일 우선 판독 구조

## Colors

차갑고 낮은 채도의 청색 계열을 사용해 분석 화면의 집중도를 유지하며, 경고만 제한된 황갈색 면으로 분리한다. 다크 모드는 동일한 역할을 어두운 청록 회색 면과 밝은 청색 강조로 치환한다.

### Primary

- **판독 청색:** 선택된 경기, 현재 탐색 위치, 확률 막대와 포커스 윤곽에 사용한다.

### Secondary

- **브리핑 워시:** 안내 띠, 표 머리글과 상태 배지처럼 본문보다 낮은 강조가 필요한 영역에 사용한다.

### Neutral

- **차가운 종이:** 화면 배경으로 사용해 흰 표면과 읽기 영역을 구분한다.
- **깨끗한 표면:** 컨트롤과 독립된 콘텐츠 표면에 사용한다.
- **짙은 남청 잉크:** 제목과 핵심 데이터에 사용한다.
- **회청 보조 잉크:** 설명, 캡션, 메타데이터에 사용한다.
- **옅은 청회 경계:** 표 행, 섹션과 컨트롤의 구분선에 사용한다.
- **절제된 경고:** 시간 초과와 부분 성공처럼 주의가 필요하지만 화면 전체를 압도하면 안 되는 상태에만 사용한다.

### Named Rules

**The 한 화면 한 강조색 Rule.** sample2의 판독 청색만 주 강조색으로 사용하며 sample1의 녹색이나 sample3의 보라색을 섞지 않는다.

**The 상태는 색만으로 말하지 않는다 Rule.** 경고와 성공 여부는 항상 상태 문구와 함께 표시한다.

## Typography

**Display Font:** Segoe UI (Malgun Gothic, sans-serif 대체)
**Body Font:** Segoe UI (Malgun Gothic, sans-serif 대체)
**Label/Mono Font:** Consolas (Malgun Gothic, monospace 대체)

**Character:** 상시 사용하는 Operate 화면이므로 운영체제에 자연스럽고 한국어 가독성이 안정적인 시스템 글꼴을 사용한다. 숫자 전용 서체와 탭형 숫자는 확률, 시간, Snapshot 식별자를 행과 모델 사이에서 빠르게 비교하게 한다.

### Hierarchy

- **Display** (700, 반응형 28–44px, 1.25): 화면 제목에 사용한다.
- **Headline** (700, 반응형 28–42px, 1.4): 선택 경기의 핵심 판독 문장에 사용한다.
- **Title** (700, 22px, 1.4): 주요 섹션 제목에 사용한다.
- **Body** (400, 15px, 1.6): 설명과 근거에 사용하며 문단 폭은 최대 72ch로 제한한다.
- **Label** (600, 12px): 상태 배지와 작은 분류 정보에 사용한다.
- **Numeric** (400, 15px, 1.6): 확률, 시간, 지표와 식별자에 사용하며 탭형 숫자를 적용한다.

### Named Rules

**The 비교 숫자 정렬 Rule.** 행별 비교가 필요한 모든 수치는 탭형 숫자와 숫자 서체를 사용한다.

## Layout

콘텐츠는 최대 1400px 영역 안에 배치한다. sample2의 핵심 레이아웃은 240px 경기 선택 레일과 최소 폭이 유연한 본문 사이 42px 간격이며, 본문 판독 폭은 최대 860px이다. 브리핑 안의 확률과 근거는 1.15:1 두 열, 예측 시점은 세 개의 같은 열로 구성한다.

1000px 이하에서는 화면 머리 영역을 세로로 쌓는다. 700px 이하에서는 경기 레일, 확률·근거, 타임라인을 한 열로 바꾸고 경기 버튼은 가로 스크롤 가능한 210px 최소 폭 항목으로 전환한다. 이때 외곽 여백은 좌우 20px, 상단 28px, 하단 40px을 사용한다.

섹션 간격은 32px을 기준으로 하며, 큰 구조 간격은 42px, 컨트롤과 작은 묶음은 6–18px 범위의 실제 간격 토큰을 사용한다.

## Elevation & Depth

그림자를 사용하지 않는다. 배경, 흰 표면, 워시 면의 명도 차이와 1px 경계선으로 깊이를 구분하며, 선택 상태는 청색 면 전환으로 표현한다.

### Named Rules

**The 평면 판독 Rule.** 데이터의 계층은 그림자나 부유 효과가 아니라 면 색, 경계선, 간격과 제목 위계로 만든다.

## Shapes

형태는 작고 실용적인 곡률을 따른다. 컨트롤은 6px, 상태 배지는 5px, 큰 독립 컨테이너와 빈 상태는 12px을 사용한다. 확률 막대는 위쪽 모서리만 3px로 둥글게 처리해 기준선에 정확히 닿게 한다. 모든 컨트롤은 최소 44px 높이를 확보한다.

## Components

### Buttons

- **Shape:** 작고 실용적인 곡률과 1px 경계선을 사용한다.
- **Primary:** 선택된 경기 버튼은 판독 청색 면과 흰 글자를 사용한다.
- **Hover / Focus:** hover는 글자를 판독 청색으로 바꾸되 선택된 경기 버튼은 흰 글자를 유지한다. 키보드 focus는 3px 판독 청색 외곽선과 4px 간격으로 표시한다.
- **Disabled:** 투명도를 낮추고 커서를 비활성 상태로 바꾼다.

### Chips

- **Style:** 워시 면, 짙은 본문색, 5px 곡률의 작은 상태 배지다.
- **State:** 경고 상태만 황갈색 배경과 진한 갈색 글자로 바꾸며 반드시 문구를 유지한다.

### Cards / Containers

- **Corner Style:** 큰 독립 표면이 필요한 경우에만 12px 곡률을 사용한다.
- **Background:** 기본 배경 위의 흰 표면 또는 워시 면을 사용한다.
- **Shadow Strategy:** 그림자를 사용하지 않는다.
- **Border:** 1px 청회색 선으로 구조를 구분한다.
- **Internal Padding:** 큰 컨테이너는 24px, 모바일은 20px을 사용한다.

### Inputs / Fields

- **Style:** 흰 배경, 1px 청회색 경계, 6px 곡률, 최소 44px 높이를 사용한다.
- **Focus:** 3px 판독 청색 외곽선과 4px 간격을 사용한다.
- **Disabled:** 투명도를 낮추고 기본 동작이 불가능함을 유지한다.

### Navigation

상단 탐색은 보조 잉크의 14px 링크를 사용하고 현재 위치만 판독 청색과 굵은 글자로 표시한다. 경기 탐색은 데스크톱에서 세로 레일, 모바일에서 가로 스크롤 목록으로 전환하며 선택 상태를 면 색과 `aria-pressed`로 함께 전달한다.

### Probability Distribution

여섯 개 세트스코어 확률을 같은 폭의 열로 배치하고, 막대 위에 수치와 아래에 결과 레이블을 둔다. 막대는 판독 청색만 사용하며 차트 전체에는 동일한 내용을 담은 접근 가능한 텍스트 대체를 제공한다.

### Tables and Disclosure

표 머리글은 워시 면과 보조 잉크를 사용하고 각 행은 1px 선으로 구분한다. 좁은 화면에서는 표 자체를 축소하지 않고 가로 스크롤을 허용한다. provenance와 지원 범위는 최소 36px 높이의 disclosure 요약으로 접어 두되 키보드로 열 수 있어야 한다.

## Do's and Don'ts

### Do:

- **Do** 한 경기의 선택, 핵심 판독, 모델 비교, 확률, 근거, 시점과 provenance 순서를 유지한다.
- **Do** 확률과 모델 상태를 동일 Snapshot 기준으로 비교하고 실패를 계산값으로 채우지 않는다.
- **Do** 다크 모드에서 문서에 기록된 역할별 다크 토큰을 사용한다.
- **Do** 모바일에서 경기 선택을 가로 스크롤로 바꾸고 본문은 한 열로 유지한다.
- **Do** 합성 데이터와 실제 운영 데이터를 명시적으로 구분한다.

### Don't:

- **Don't** sample1의 녹색이나 sample3의 보라색 팔레트를 sample2 방향에 섞지 않는다.
- **Don't** 그림자, 장식 이미지, 그라데이션으로 데이터 위계를 대신하지 않는다.
- **Don't** 색상만으로 성공, 실패, 미수신, 미확정 상태를 전달하지 않는다.
- **Don't** 장기 성능 워크벤치나 운영 데스크의 정보 밀도를 경기 브리핑 첫 화면에 그대로 합치지 않는다.
- **Don't** 정적 프로토타입의 합성 수치와 경기명을 실제 제품 데이터처럼 취급하지 않는다.
