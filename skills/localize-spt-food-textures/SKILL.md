---
name: localize-spt-food-textures
description: Localize SPT/Escape from Tarkov food and drink Texture2D packaging into natural Korean while preserving UV layout, non-text pixels, alpha, material maps, mipmaps, streamed payload metadata, and UnityFS structure. Use when Codex needs to inventory or extract food bundles, translate visible Latin/Cyrillic package copy, create or review Korean bitmap edits, rebuild SPT bundle overrides, validate localized textures, or prepare a reversible food-texture mod.
---

# SPT 음식 텍스처 현지화

원본 게임 자산을 로컬에서만 읽고, 프로필의 정확한 문구와 검증 기준을 따라 음식 포장을
한국어화하라. 이미지 생성 결과를 바로 번들에 넣지 말고 반드시 승인·합성·검증 단계를
거쳐라.

## 시작 전

- 저장소의 `profiles/food/collection.json`과 `docs/architecture-v2.md`를 읽어라.
- 실제 작업에는 [workflow.md](references/workflow.md)를 읽어라.
- 이미지 승인이나 번들 생성 전에는 [quality-gates.md](references/quality-gates.md)를 읽어라.
- 번역 문구를 작성할 때는 [translation-rules.md](references/translation-rules.md)를 읽어라.
- 게임 번들, 추출 PNG와 생성 이미지를 Git에 추가하지 마라. `workspace/`에만 저장하라.
- 기존 사용자 변경과 라이브 게임 파일을 덮어쓰지 마라.

## 실행 순서

1. `python localize.py --spt-root <SPT 경로> extract`로 전체 프로필을 추출하라.
2. `python localize.py review-sheets`로 작업 대상을 검수하라.
3. 각 대상의 모든 외국어 문구와 정확한 한국어 대응을 프로필에 기록하라.
4. 품목별로 별도의 이미지 편집을 수행하라. 원본을 edit target으로 지정하고
   `text-localization` 용례를 사용하라.
5. 원본 UV와 비문자 영역이 보존된 결과만 `python localize.py stage <id> <이미지>`로 승인하라.
6. `python localize.py validate`를 통과시킨 뒤에만 보조맵과 번들을 만들라.
7. 재패킹 결과를 다시 열어 포맷·밉·stream metadata·UnityFS 레이아웃과 왕복 이미지를 검증하라.
8. 라이브 설치는 사용자가 명시적으로 요청한 경우에만 백업 후 수행하라.

## 이미지 편집 규칙

- 품목마다 하나의 독립 편집 호출을 사용하라.
- `Use case: text-localization`으로 지정하라.
- 바꿀 문구를 따옴표로 정확히 열거하고 한국어 결과를 글자 그대로 요구하라.
- `change only the text`와 보존할 UV·재질·오염·주름·알파 조건을 매번 반복하라.
- 결과의 문구가 틀리면 전체를 재생성하지 말고 한 번에 한 문제만 수정하라.
- 생성 결과가 비문자 영역을 바꾸면 참고본으로만 쓰고 원본에 결정적으로 다시 조판하라.

## 중단 조건

- 대상 Texture2D가 없거나 둘 이상이면 중단하라.
- 원본과 후보 크기가 다르면 자동 리사이즈하지 말고 원인을 확인하라.
- 알파가 달라지면 승인하지 마라.
- 포맷·밉 수·stream payload 크기를 유지할 수 없으면 재패킹하지 마라.
- payload가 압축된 UnityFS 블록에 있어 바이트 범위만 안전하게 교체할 수 없으면 중단하라.
- 번들 payload 밖 바이트가 달라지면 배포하지 마라.

## 결과 보고

- 완료·대기·실패 수를 구분해 보고하라.
- 각 승인 이미지, 검증 보고서와 번들의 프로젝트 내부 경로를 알려라.
- 사용한 최종 이미지 편집 프롬프트를 품목별로 보존하라.
- 설치하지 않았다면 명확히 밝혀라.
