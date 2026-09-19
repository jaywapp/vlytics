# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

제품 기술 스택은 미확정이다(UC-003). 요청된 비교 시안은 설치 없이 열 수 있는 HTML/CSS/JavaScript로 한정한다. 이 선택이 제품 스택을 확정하지 않는다.

## Users

운영자 한 명이 V리그 남자부·여자부 데이터와 예측을 검증한다. 공개 서비스는 장기 목표이며 이번 준비 작업의 구현 대상은 아니다.

## Product Purpose

V-Mirror에서 관측한 사실을 V-Engine의 시점 고정 입력으로 만들고, 독립 예측과 실제 결과를 Vlytics Web에서 비교한다. 예측의 근거·버전·입력과 실패까지 추적하는 것이 핵심이다.

## Operating Context

일정에 따라 경기 시작 한 시간 전에 분석하고 경기 종료 후 평가한다. 경기 수는 고정하지 않는다. 명단 미확정과 Market 누락을 정상적인 데이터 상태로 다룬다.

## Capabilities and Constraints

원문 확정 사항은 [design.md](design.md)에 정리한다. 검토자의 제안은 사용자 승인과 구분한다. 모든 시안 데이터는 합성 데이터이며 실제 일정·적중률·선수 상태를 주장하지 않는다. 시안 선택은 UC-008 Pending이다.

## Brand Commitments

제품명 Vlytics. 한국어 운영 화면. 확정된 로고·색상·폰트는 없다.

## Evidence on Hand

[원안](../ideas/vlytics.md), [검토 의견](../ideas/vlytics-claude-feedback.md) 전체를 읽었다. 기존 코드·토큰·화면·실측 데이터 파일은 없다. 이번 작업은 문서 및 비교 프로토타입 제작이다.

## Product Principles

- 원천 사실, 독립 예측, 시장 비교를 분리한다.
- 예측 결과를 사후 수정하지 않는다.
- 성능에는 표본 수와 평가 대상을 함께 표시한다.
- 미결정 사항을 확정된 기능처럼 취급하지 않는다.
