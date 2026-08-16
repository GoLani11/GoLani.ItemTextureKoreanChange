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

최소 복수 후보를 만들지 않는다. 첫 결과를 바로 최종 크기로 검사한다. 철자나 한 가지
typography 항목만 실패하면 그 항목만 지적해 한 번 재시도할 수 있다. 여러 항목이 흔들리거나
비문자 영역이 바뀌면 반복 생성하지 말고 `review` 또는 `block`으로 둔다.

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

## 합성과 검사

생성 결과에서 승인한 문자 픽셀을 `lettering_mask`로 분리해 원본에 합성한다. mask 밖 생성
픽셀은 버린다. 결과 OCR로 정확한 한국어와 반복 횟수, `editable` 안의 금지 외국어 잔상을
검사한 뒤 Codex가 원본/결과 비교 시트에서 typography lock과 비문자·보호 미세 인쇄 보존을
최종 확인한다.

`edit_plan.data.compositor`는 다음 구조를 사용한다.

```json
{
  "mode": "vision-panel-localization",
  "fixed_font_used": false,
  "single_pass_panels": true,
  "regions": [
    {
      "region_id": "front-brand-01",
      "exact_text": "한글 제품명",
      "bbox": [120, 84, 418, 148],
      "rotation_deg": 0,
      "direction": "left-to-right",
      "model_signature": "image-model-and-settings",
      "generation_attempts": 1,
      "ocr_exact_match": true,
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
