---
name: localize-spt-food-textures
description: Safely analyze, translate, generate, review, validate, and package Korean SPT/Escape from Tarkov food and drink textures. Use for any work in this repository involving OCR or visual transcription of package text, Korean translation, bitmap generation or editing, Diffuse/Normal/Gloss alignment, UV seams, mipmaps, UnityFS bundles, release validation, or texture deployment. Do not use for unrelated game assets or generic image editing outside this repository.
---

# SPT 음식 텍스처 현지화

원본은 Codex가 원본 해상도에서 먼저 시각 판독하고, 모호한 대상 영역에만 원본 OCR을 보조로
사용하라. 기본 현지화 범위는 정상 게임 화면에서 식별되는 제품명·브랜드·짧은 핵심 문구다.
작은 원재료·법정표시·주소·영양·인증·바코드·날짜·장식성 미세 인쇄는 원본 픽셀로 보호하라.
한글은 원문의 글꼴 인상·스타일·크기·실루엣·배치·방향을 거의 같게 유지하고, 결과 OCR과
최종 시각 비교가 모두 통과하지 않으면 다음 단계로 진행하지 마라.

## 필수 자료

- 시작할 때 `profiles/food/collection.json`과 `docs/architecture-v2.md`를 읽어라.
- 실제 작업 전 [workflow.md](references/workflow.md)를 끝까지 읽어라.
- 판독 기록을 만들기 전 [review-record.md](references/review-record.md)를 읽어라.
- 번역 전 [translation-rules.md](references/translation-rules.md)를 읽어라.
- 이미지 생성 전 [ai-lettering.md](references/ai-lettering.md)를 끝까지 읽어라.
- 이미지 승인·보조맵·번들 작업 전 [quality-gates.md](references/quality-gates.md)를 읽어라.

## 실행 원칙

- `workspace/`의 추출 원본을 불변 기준으로 사용하라. 라이브 게임 파일을 편집 기준으로
  사용하거나 게임 자산을 Git에 추가하지 마라.
- 게임 텍스처에 실제로 인쇄된 문구를 정본으로 삼아라. 실물 상품·회사 정보는 시각 참고일
  뿐이며 게임 문구를 외부 정보로 교정하지 마라.
- 별도 요청이 없으면 profile의 `exact_text`를 큰 핵심 문구의 편집 허용 목록으로 유지하라.
  사용자가 전체 라벨 번역을 명시한 품목만 작은 설명 문구를 목록에 포함하라.
- 목록 밖의 읽을 수 있는 외국어 미세 인쇄는 오류가 아니다. 위치와 종류를 보존 영역으로
  기록하고 후보에서 픽셀이 바뀌지 않았음을 검사하라.
- 품목 하나를 하나의 검증 단위로 처리하라. 여러 품목을 한 생성 호출에서 편집하지 마라.
- `<python>`은 현재 환경에서 동작하는 Python 3.11 이상 실행기로 바꿔라.
- 기존 작업 기록은 덮어쓰지 말고 이어서 사용하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py init <target-id>
```

- 증거와 판정을 `workspace/reviews/<target-id>/review.json`에 기록하라.
- 상태는 `pending`, `pass`, `block`, `review`, `error`만 사용하라. 자동 다음 단계는 `pass`만
  허용하고 `review`는 현재 산출물 해시에 묶인 사람 승인 뒤에만 `pass`로 바꿔라.
- `--allow-resize`, 승인용 `--allow-partial` 또는 검증 우회 옵션을 사용하지 마라.

## 필수 순서

1. 원본·Texture2D·실제 Material 연결과 D/N/G 공유 관계를 확인하라. Renderer→Material→
   Mesh와 target submesh 연결을 해석하고 `localize.py uv-review`로 실제 seam guard를 만들어라.
2. 원본 mip 0을 `view_image`의 원본 상세도로 한 번 보고 `exact_text`에 대응하는 대상 글자를
   Codex가 먼저 판독하라. 문자열·bbox·회전·읽는 방향·면·그림 방향과 글꼴 인상, 획, 비례,
   기준선, 정렬, 자간, 외곽선, 그림자, 마모를 `source_visual`에 기록하라. 제외한 미세 인쇄는
   내용을 전사하지 말고 보호할 묶음의 bbox와 종류만 기록하라.
3. 명확히 읽히는 대상 영역에는 원본 OCR을 실행하지 마라. 확대해도 대상 문자가 모호하거나
   누락 가능성이 있는 영역만 `needs_ocr_fallback: true`로 표시하고 그 영역에 한해 OCR과
   교차검증을 실행하라. 보호 미세 인쇄를 해독하려고 OCR을 실행하지 마라. 대상 영역의 OCR과
   시각 판독이 충돌하면 생성하지 마라.
4. 각 대상 영역의 원문, 뜻, 확정 한국어, 횟수, 위치, 크기, 회전과 방향을 확정하라.
5. `editable`, `old_text`, `new_text`, `protected`, `seam_guard` 마스크를 원본 크기로 확정하라.
   제외한 미세 인쇄 전체를 `protected`에 포함하고 `editable`과 겹치지 않게 하라.
6. 분석 게이트를 통과시킨 뒤에만 이미지를 생성하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through analysis
```

7. 이미지 생성 도구에는 원본 전체와 한 번에 이해할 수 있는 연결된 라벨 면을 제공하라. 같은
   면의 여러 문구는 한 호출에서 함께 바꾸고, 글자마다 별도 호출하거나 최소 복수 후보를
   의무 생성하지 마라. 첫 결과부터 OCR과 시각 검사를 실행하고 확정 한국어의 철자·횟수가
   모두 맞을 때까지 실패 원인 하나씩만 보정해 생성·합성·OCR을 반복하라. OCR 통과 전에는
   실패 결과를 최종 미리보기나 후보로 보고하지 마라.
8. 생성 프롬프트에서 정확한 한국어만 바꾸고 배경·그림·로고 도형·재질·오염·주름·UV 경계는
   그대로 두라고 명시하라. 고정 한글 폰트를 얹거나 전체 Texture2D를 다시 그리지 마라.
9. 각 영역의 한글은 원문과 다음 특성을 거의 같게 만들어라.
   - 글꼴 인상과 스타일: serif/sans/display 계열, 획 굵기·대비·끝 모양, 장평·기울기
   - 크기와 모양: ink bbox 높이·폭·영역 점유율 차이 각각 10% 이내
   - 배치: 기준선·정렬·자간·행간·곡선 흐름과 여백이 시각적으로 일치
   - 방향: 읽는 방향 완전 일치, 원문 대비 회전 차이 2° 이내
   - 효과: 채움·외곽선·그림자·입체감·마모·인쇄 질감과 레이어 순서 일치
   한글 음절을 읽기 어렵게 찌그러뜨리지는 말고, 같은 시각적 무게와 실루엣을 유지하라.
10. 생성 결과에서 승인한 문자 픽셀만 원본에 합성하라. 비문자 픽셀은 원본을 유지하고,
    반복 로고는 승인한 같은 패치·마스크·변환을 재사용하라. 결과 전체 리사이즈를 금지한다.
11. 결과 OCR로 확정 한국어의 철자·횟수와 `editable` 안의 금지 외국어 잔상만 검사하라.
    `protected`에 원본 그대로 남은 외국어 미세 인쇄는 허용한다. 이어서 Codex가
    원본·결과를 같은 배율로 비교해 글꼴 인상·스타일·크기·모양·방향·효과와 비문자 보존을
    영역별로 판정하라. OCR만으로 시각 품질을 통과시키지 마라.
12. 후보 게이트 통과 후에만 승인본으로 stage하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through candidate
<python> localize.py stage <target-id> <candidate.png>
```

13. 파일명 추정이 아니라 실제 Material의 Texture PPtr를 따라 D/N/G를 매핑하라. 평면 인쇄면
    N/G를 보존하고 기존 외국어 요철·광택이 있을 때만 재질 전용 `old_text` 마스크 안에서
    제거하라. Diffuse의 넓은 복원 마스크를 Normal·Gloss에 자동 재사용하지 마라.
14. 공유 보조맵의 모든 소비자를 확정하지 못했거나 서로 다른 디자인이 하나의 맵을 요구하면
    자동 파생하지 마라.
15. 재질 게이트 통과 후에만 임시 번들을 만들고, 모든 밉·압축 왕복·실제 렌더를 검사한 뒤
    release 해시를 고정하라.

## 중단 조건

- Codex 시각 판독에 읽지 못한 현지화 대상 문구가 남았는데 원본 OCR·교차검증을 하지 않았다.
- 원본 OCR과 시각 판독이 충돌하거나 번역 의미·횟수·방향이 확정되지 않았다.
- 후보 크기·비율·알파가 원본과 다르거나 리사이즈 이력이 있다.
- editable 밖 RGB, protected 또는 seam guard가 변경되었다.
- 일반 한글 폰트 조판을 최종 레터링으로 사용했다.
- 원본 스타일 참조, 생성 패널, 선택 패치·마스크 또는 모델 서명이 누락됐다.
- 글꼴 인상·스타일·실루엣·크기·점유율·기준선·정렬·방향·효과 중 하나라도 허용 오차를
  벗어나거나 원문 로고 실루엣이 남았다.
- 결과 OCR과 최종 Codex 시각 비교 중 하나라도 누락되거나 현재 후보 SHA와 묶이지 않았다.
- D/N/G 실제 연결·공유 소비자·좌표 정렬, 필수 밉·압축·번들·실제 렌더를 증명하지 못했다.
- 현재 코드가 필수 게이트를 구현하지 않았다. 약한 검사를 대신 통과로 간주하지 마라.

## 결과 보고

- 품목별 현재 단계, `pass/block/review/error`, 미해결 항목과 다음 행동을 먼저 보고하라.
- 원본, 시각 판독, 필요한 경우의 OCR·교차검증, 번역, 마스크, 후보, 결과 OCR, 비교 시트와
  D/N/G·밉·렌더·번들 증거의 프로젝트 내부 경로를 알려라.
- 설치하지 않았다면 명확히 밝히고, 실제 설치는 사용자가 명시적으로 요청했을 때만 수행하라.
