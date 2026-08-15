---
name: localize-spt-food-textures
description: Safely analyze, translate, generate, review, validate, and package Korean SPT/Escape from Tarkov food and drink textures. Use for any work in this repository involving OCR or visual transcription of package text, Korean translation, bitmap generation or editing, Diffuse/Normal/Gloss alignment, UV seams, mipmaps, UnityFS bundles, release validation, or texture deployment. Do not use for unrelated game assets or generic image editing outside this repository.
---

# SPT 음식 텍스처 현지화

원본 포장의 모든 보이는 문자를 두 번 판독하고 교차검증한 뒤, 허용한 영역만 수정하라.
검증을 실행하지 않았거나 결과가 불일치하면 다음 단계로 진행하지 마라.

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
- 품목 하나를 하나의 검증 단위로 처리하라. 여러 품목을 한 생성 호출에서 편집하지 마라.
- `<python>`은 현재 환경에서 동작하는 Python 3.11 이상 실행기로 바꿔라. 작업 기록 도구는
  표준 라이브러리만 사용하므로 Python 3에서도 실행할 수 있다.
- 작업 시작 시 다음 명령으로 기록을 생성하라. 기존 기록이 있으면 덮어쓰지 말고 이어서
  사용하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py init <target-id>
```

- 각 단계의 증거와 판정을 `workspace/reviews/<target-id>/review.json`에 기록하라.
- 상태는 `pending`, `pass`, `block`, `review`, `error`만 사용하라. 자동 다음 단계는 `pass`만
  허용하라. `review`는 해당 산출물 해시에 묶인 사람 승인이 있을 때만 `pass`로 바꿔라.
- `--allow-resize`, 승인용 `--allow-partial` 또는 검증 우회 옵션을 사용하지 마라.

## 필수 순서

1. 원본·Texture2D·실제 Material 연결과 D/N/G 공유 관계를 확인하라.
   `localize.py extract`가 기록한 Renderer→Material→Mesh와 target submesh 연결이 모두
   해석됐는지 확인하고 `localize.py uv-review`로 실제 UV seam guard를 만들어라.
2. OCR로 보이는 텍스트의 문자열, bbox, 회전, 읽는 방향, 신뢰도와 엔진 버전을 기록하라.
3. OCR 문구를 정답으로 복사하지 말고 원본을 원본 해상도로 다시 시각 판독하라. 문자뿐
   아니라 면·UV 섬·그림 방향·절취선·로고와 주변 질감도 기록하라. 저장소 도구를 쓸 때는
   `localize.py visual-sheets`의 OCR 문자열이 숨겨진 crop을 먼저 판독하라.
4. 두 판독을 교차검증하라. 누락·오인식·방향·의미 충돌이 하나라도 남으면 생성하지 마라.
5. 각 영역의 원문, 뜻, 최종 한국어, 횟수, 위치, 크기, 회전과 읽는 방향을 확정하라.
6. `editable`, `old_text`, `new_text`, `protected`, `seam_guard` 마스크를 원본 크기로 확정하라.
7. 분석 게이트를 통과시킨 뒤에만 이미지를 생성하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through analysis
```

8. 이미지 생성은 서로 독립된 두 단계로 수행하라. 먼저 AI로 `old_text` 안의 원문만 지워
   배경을 복구하고 해시로 고정하라. 그 배경을 다시 생성하지 말고, 각 문자 영역에 원본
   확대 crop·고정 배경·확정 한국어·bbox·방향을 함께 제공해 AI가 맞춤 한글 레터링을 만들게
   하라. 고정 한글 폰트를 선택하거나 일반 글꼴을 얹은 결과를 최종 후보로 사용하지 마라.
9. AI가 글자의 형태·크기·장평·기울기·자간·곡선·외곽선·그림자·색·마모·인쇄 질감을 원본
   참조에서 결정하게 하라. 정확한 한국어 문자열만 불변 제약으로 주고, 영역별 최소 2개
   후보를 한 번에 생성해 OCR 정확성과 시각 유사도로 선별하라. AI가 오탈자를 반복하면
   고정 폰트로 대체하지 말고 `review` 또는 `block`으로 중단하라.
10. 선택한 AI 레터링의 픽셀만 고정 배경에 합성하라. AI가 바꾼 비문자 픽셀은 채택하지 말고,
    반복 로고는 승인된 같은 패치·마스크·변환을 재사용하라. 원본 전체 재생성, 임의 리사이즈와
    후보별 배경 재생성을 금지한다.
11. 생성 후 OCR을 다시 실행하고, 별도로 원본·결과를 같은 배율로 시각 비교하라. 문구
    누락·오자·중복, 외국어 잔상뿐 아니라 글자 실루엣·크기·방향·자간·효과·표면 질감,
    기존 원문 로고의 외곽선·그림자 잔상을 영역별로 확인하라.
12. 후보 게이트 통과 후에만 승인본으로 stage하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through candidate
<python> localize.py stage <target-id> <candidate.png>
```

13. 파일명 추정이 아니라 실제 Material의 Texture PPtr를 따라 D/N/G를 매핑하라. 동일 글자
    효과는 같은 확정 `new_text` 마스크와 좌표계를 사용하라. 평면 인쇄면 N/G를 보존하고,
    기존 외국어 요철·광택이 있을 때만 각 보조맵에 명시한 재질 전용 `old_text` 마스크 안에서
    제거하라. Diffuse의 넓은 복원 마스크를 Normal·Gloss에 자동 재사용하지 마라.
14. 공유 보조맵의 모든 소비자를 확정하지 못했거나 서로 다른 디자인이 하나의 맵을 요구하면
    자동 파생하지 마라.
15. 재질 게이트 통과 후에만 임시 번들을 만들라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through material
```

16. 맵 역할별로 밉을 검사하고 압축 왕복 결과를 모든 밉에서 확인하라. 실제 게임 또는 동일
    셰이더 캡처로 정면·사광·그림자·근거리·원거리 결과를 검증하라.
17. release 게이트가 통과된 해시 고정 산출물만 배포 후보로 인정하라.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through release
```

## 중단 조건

- OCR과 시각 판독의 충돌 또는 읽지 못한 문구가 남아 있다.
- 후보 크기·비율·알파가 원본과 다르거나 리사이즈 이력이 있다.
- 허용 영역 밖 RGB, 보호 영역 또는 seam guard가 변경되었다.
- 번역 의미·횟수·방향·그림 방향이 확정되지 않았다.
- 고정 한글 폰트 또는 일반 글꼴 조판을 최종 레터링으로 사용했다.
- 영역별 원본 스타일 참조·고정 배경·AI 후보·선택 패치·마스크·모델 서명이 누락됐다.
- 한글 실루엣·크기·방향·자간·외곽선·그림자·표면 질감이 원본과 맞지 않거나 원문 로고의
  외곽선·그림자 실루엣이 남았다.
- D/N/G의 실제 연결·공유 소비자·좌표 정렬을 증명하지 못했다.
- normal/gloss에서 기존 외국어가 사광·그림자·광택으로 나타난다.
- 필수 밉, 압축 왕복, 번들 레이아웃 또는 실제 렌더 검증이 실행되지 않았다.
- 현재 코드가 필수 게이트를 구현하지 않았다. 이 경우 약한 기존 검사를 대신 통과로
  간주하지 말고 `error`로 기록하라.

## 결과 보고

- 품목별 현재 단계, `pass/block/review/error`, 미해결 항목과 다음 행동을 먼저 보고하라.
- 원본, OCR, 독립 판독, 교차검증, 번역·배치, 마스크, 후보, D/N/G, 밉, 렌더와 번들 증거의
  프로젝트 내부 경로를 알려라.
- 설치하지 않았다면 명확히 밝히고, 실제 설치는 사용자가 명시적으로 요청했을 때만 수행하라.
