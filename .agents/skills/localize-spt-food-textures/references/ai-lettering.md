# 비전 우선 한글 레터링

Codex 비전이 원본 전체와 라벨 맥락을 먼저 이해하고 이미지 생성 도구가 연결된 라벨 면을 한
번에 편집하게 한다. 원본 OCR은 모호한 문자를 푸는 보조 수단이고, 결과 OCR은 정확한 한국어를
검사하는 필수 수단이다.

## 원본 typography signature

각 번역 영역에 다음 값을 원본 mip 0 기준으로 기록한다.

- `style_class`: serif, sans, script, condensed display 등 글꼴 인상
- `stroke_character`: 획 굵기·대비·끝 모양·모서리 성격
- `glyph_proportions`: 장평, 기울기와 글자 덩어리의 실루엣
- `ink_bbox`, `ink_width_px`, `ink_height_px`, bbox 점유율
- `alignment`: 기준선·가운데/좌우 정렬·여백
- `spacing`: 자간·행간·곡선 또는 원호 흐름
- `effects`: 채움·외곽선·그림자·입체감과 레이어 순서
- `surface_finish`: 마모·번짐·노이즈·반사·가장자리 선명도
- `slant_deg`, 영역 `rotation_deg`, `direction`

“같은 글꼴”은 라틴·키릴 글리프를 억지로 한글에 복사한다는 뜻이 아니다. 한글로 읽히면서도
원본과 같은 글꼴 계열·획 리듬·시각적 무게·실루엣을 가진 맞춤 레터링을 뜻한다.

## 입력 고정

연결된 라벨 면마다 다음을 해시로 고정한다.

- 원본 전체와 원본 해상도 라벨 crop
- 정확한 한국어 문자열, 줄바꿈, 반복 횟수
- bbox, 회전, 읽는 방향, 면과 artwork 방향
- 원본 typography signature
- `old_text`, `new_text`, `editable`, `protected`, `seam_guard` 마스크
- 제외한 미세 인쇄를 덮는 `protected` 영역과 그 원본 픽셀 SHA

## 조건부 정방향 작업 패널

대상 문구가 화면 기준 정방향이 아니면 연결된 라벨 면을 충분한 여백과 함께 crop하고, 기록한
`rotation_deg`의 역각으로 임시 작업 패널을 정방향화한다. 각도를 90° 배수로 반올림하거나
글자별로 따로 회전하지 않는다. 같은 면의 대상 문구·그림·재질 맥락에는 하나의 변환을 사용한다.

생성과 결과 OCR은 정방향 작업 좌표에서 수행한다. crop 원점·여백·원본 회전각·정방향 변환과
그 정확한 역변환을 생성 패널 증거에 기록한다. 통과한 글자 패치와 `lettering_mask`만 역변환해
원본 좌표·회전·읽는 방향으로 되돌린다. 최종 후보에서는 원본 기준 `rotation_deg`와 `direction`을
검사한다. 임시 패널의 비문자 픽셀을 최종 후보에 복사하거나 원본·최종 Texture2D 전체를
회전·재표본화하지 않는다.

## 한 번에 편집

같은 라벨 면의 대상 문구를 하나의 이미지 편집 호출에서 함께 바꾼다. 프롬프트에는 다음을
명시한다.

```text
Use case: text-localization
Image 1: edit target and immutable visual reference
Replace only the listed source lettering with the exact Korean text.
Preserve all non-text pixels, artwork, label geometry, material, wear, folds, shadows and UV edges.
Preserve every small legal, ingredient, nutrition, address, barcode, certification and date label verbatim.
Match each source region's font character, stroke, proportions, ink size, baseline, spacing,
rotation, reading direction, outline, shadow, layering and print wear as closely as possible.
Render the Korean text verbatim with no extra or missing characters.
```

최소 복수 후보를 만들지 않고 연결된 면의 기본 생성 예산을 2회로 제한한다. 첫 생성 패널은
원본 배율로 글자 인상, 자연스러운 단어 형태, 자간, 라벨 균형과 비문자 흔들림부터 시각
선별한다. 이 단계에서는 패널 OCR이나 공식 합성을 실행하지 않는다. 실패 원인 하나만 지적해
두 번째 결과를 만들 수 있다. 두 번째도 실패하면 자동 생성 반복을 멈추고 `block`한다.
사용자가 명시적으로 추가 시도를 요청한 경우에만 예산을 늘린다.

패널 OCR은 시각적으로 채택한 생성 패널 SHA에만 실행한다. 정확히 일치한 패널에서 승인 문자
패치만 원본에 공식 합성해 보호 픽셀을 복원한다. 합성 후보의 픽셀·시각 사전 검사가 통과한
뒤에만 후보 OCR을 실행한다. 패널 또는 후보 OCR 실패 시 남은 생성 예산 안에서 철자·누락·방향
같은 해당 원인만 보정하고 새 SHA에 다시 실행한다. OCR을 통과한 뒤에도 typography lock이나
보호 검사가 실패하면 `review` 또는 `block`으로 둔다. 패널 OCR 전 결과는 `검증 전 미리보기`로만
표시하며 최종 후보로 승격하지 않는다.

고정 한글 폰트 오버레이, 원문 폭을 맞추기 위한 사후 글리프 변형, 글자별 별도 생성, 후보별
배경 재생성과 전체 Texture2D 재생성을 금지한다.

## typography lock

원본과 결과를 같은 배율로 놓고 각 영역을 판정한다.

- `font_character_matched`: 글꼴 계열, 획 굵기·대비·끝 모양 일치
- `style_matched`: 장평·기울기·실루엣과 시각적 위계 일치
- `size_matched`: ink 높이·폭·bbox 점유율 차이가 각각 0.10 이하
- `alignment_matched`: 기준선·정렬·여백 일치
- `spacing_matched`: 자간·행간·곡선 흐름 일치
- `direction_exact`: 읽는 방향 완전 일치
- `rotation_delta_deg`: 절댓값 2.0 이하
- `effects_matched`: 채움·외곽선·그림자·입체감·레이어 순서 일치
- `surface_matched`: 마모·번짐·노이즈·반사·선명도 일치

한글의 자연스러운 음절 비례와 가독성을 깨뜨리지는 않는다. 글자 수가 짧아도 과도한 장평
왜곡 대신 맞춤 획 설계와 합리적인 자간으로 원문의 전체 시각 무게를 맞춘다.
ink bbox 수치만 맞추려고 음절 사이에 큰 빈 공간을 넣거나, 복사한 장식 획으로 실루엣 측정을
인위적으로 늘리지 않는다. 먼저 원본 배율의 시각 비교를 통과시킨 다음 수치 허용오차를 확인한다.

## 합성과 검사

생성 결과에서 승인한 문자 픽셀을 `lettering_mask`로 분리해 원본에 합성한다. mask 밖 생성
픽셀은 버린다. 결과 OCR로 정확한 한국어와 반복 횟수, `editable` 안의 금지 외국어 잔상을
검사한 뒤 Codex가 원본/결과 비교 시트에서 typography lock과 비문자·보호 미세 인쇄 보존을
최종 확인한다.

승인된 `selected_lettering`의 연속 알파는 downstream 보조맵의 유일한 master geometry다.
영역별 `selected_lettering`과 `lettering_mask` SHA를 함께 고정한다. Normal·Gloss용 글자를 다시
생성·검출하지 말고, 각 해상도에서 같은 알파/SDF를 UV 기준으로 재래스터화한다. 물리적 요철에
채움만 써야 한다면 fill/outline/shadow submask를 분리해 각각 해시 고정한다.

`edit_plan.data.compositor`는 다음 구조를 사용한다.

```json
{
  "mode": "vision-panel-localization",
  "fixed_font_used": false,
  "single_pass_panels": true,
  "regions": [
    {
      "panel_id": "front-panel",
      "region_id": "front-brand-01",
      "exact_text": "한글 제품명",
      "occurrences": 1,
      "bbox": [120, 84, 418, 148],
      "rotation_deg": 0,
      "direction": "left-to-right",
      "model_signature": "image-model-and-settings",
      "generation_attempts": 1,
      "ocr_exact_match": true,
      "panel_ocr": {"path": "...", "sha256": "..."},
      "panel_transform": {
        "coordinate_space": "source-mip0",
        "crop_bbox": [96, 60, 442, 172],
        "padding_px": 24,
        "source_rotation_deg": 0,
        "deskew_rotation_deg": 0,
        "inverse_rotation_deg": 0,
        "selected_lettering_restored_to_source": true,
        "source_texture_resampled": false,
        "final_texture_resampled": false
      },
      "source_typography": {
        "style_class": "condensed high-contrast serif display",
        "stroke_character": "heavy verticals, thin cross strokes, wedge terminals",
        "glyph_proportions": "tall and narrow",
        "ink_bbox": [124, 88, 414, 145],
        "ink_width_px": 290,
        "ink_height_px": 57,
        "alignment": "centered on label baseline",
        "spacing": "tight tracking",
        "effects": "black fill, cream outline, short dark shadow",
        "surface_finish": "worn offset print",
        "slant_deg": 0
      },
      "result_typography": {
        "style_class": "condensed high-contrast serif display",
        "stroke_character": "heavy verticals, thin cross strokes, wedge terminals",
        "glyph_proportions": "tall and narrow Hangul",
        "ink_bbox": [126, 89, 412, 145],
        "ink_width_px": 286,
        "ink_height_px": 56,
        "alignment": "centered on label baseline",
        "spacing": "balanced two-syllable tracking",
        "effects": "black fill, cream outline, short dark shadow",
        "surface_finish": "worn offset print",
        "slant_deg": 0
      },
      "typography_checks": {
        "font_character_matched": true,
        "style_matched": true,
        "size_matched": true,
        "alignment_matched": true,
        "spacing_matched": true,
        "direction_exact": true,
        "effects_matched": true,
        "surface_matched": true,
        "ink_height_delta_ratio": 0.02,
        "ink_width_delta_ratio": 0.02,
        "bbox_coverage_delta_ratio": 0.02,
        "rotation_delta_deg": 0
      },
      "source_style_reference": {"path": "...", "sha256": "..."},
      "generated_panel": {"path": "...", "sha256": "..."},
      "selected_lettering": {"path": "...", "sha256": "..."},
      "lettering_mask": {"path": "...", "sha256": "..."}
    }
  ]
}
```

## 실행 결과 절약

- 생성본마다 OCR을 실행하지 말고 시각 채택 패널과 사전 검사를 통과한 합성 후보에만 실행한다.
- 입력 이미지, crop, 프롬프트, 모델 서명과 설정 SHA가 같으면 기존 생성·OCR 증거를 재사용한다.
- 상세 JSON과 OCR 원문 응답은 보고서 파일에 두고 대화에는 일치 여부, 실패 영역과 경로만
  출력한다.
