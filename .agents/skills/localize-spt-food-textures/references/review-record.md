# 판독·검증 기록

품목별 기록은 `workspace/reviews/<target-id>/review.json`으로 관리한다. `workspace/`는 Git에
추가하지 않는다.

## 생성과 검사

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py init <target-id>
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through analysis
```

`init`은 profile을 복사하고 모든 단계를 `pending`으로 만든다. 기존 파일은 덮어쓰지 않는다.
단계는 `stage --status ... --data ... --evidence ...`로 현재 증거 SHA와 함께 기록한다.

`check`의 게이트는 다음과 같다.

- `analysis`: Codex 원본 시각 판독과 번역. 모호한 영역이 있을 때만 원본 OCR·교차검증 추가
- `candidate`: 마스크·비전 패널 편집·픽셀 검사·결과 OCR·최종 시각 비교
- `material`: 실제 D/N/G 연결·공유 관계·좌표 정렬
- `release`: 모든 밉, 압축·번들, 실제 렌더와 release 해시

요구 단계가 `pass`가 아니거나 `unresolved`가 남아 있으면 실패한다.

## 좌표 규칙

- bbox는 원본 mip 0의 `[x0, y0, x1, y1]`이며 원점은 좌측 상단이다.
- 회전은 화면에서 시계 방향 각도(`rotation_deg`)다.
- `direction`은 `left-to-right`, `right-to-left`, `top-to-bottom`, `bottom-to-top` 중 하나다.
- `face`와 `artwork_direction`에는 확인한 면과 그림 기준 방향을 기록한다.

## 비전 우선 판독 영역

`source_visual.data`에는 `vision_first: true`, `ocr_fallback_required: boolean`과 `regions`를 둔다.
각 region은 기본 좌표 정보 외에 `needs_ocr_fallback`과 `typography`를 가진다.

```json
{
  "region_id": "front-brand-01",
  "text": "SOURCE",
  "script": "latin",
  "bbox": [120, 84, 418, 148],
  "rotation_deg": 0,
  "direction": "left-to-right",
  "face": "front",
  "artwork_direction": "라벨 위쪽을 향함",
  "needs_ocr_fallback": false,
  "typography": {
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
  }
}
```

`needs_ocr_fallback: true`가 있으면 `ocr_fallback_required`도 `true`여야 하며 `source_ocr`과
`cross_validation`이 `pass`여야 한다. OCR detection에는 engine·model signature·confidence를
추가하고, 해당 시각 영역은 conflict 없이 교차검증해야 한다. fallback이 없으면 두 단계는
`pending`이어도 된다.

기본 현지화 범위에서 제외한 미세 인쇄는 `source_visual.data.preserved_regions`에 bbox, 면,
`kind`만 기록한다. 그 내용을 전사하거나 OCR로 해독하지 않는다.

## 번역 영역

`translation.data.regions`에는 `region_id`, `source_text`, `meaning_ko`, `final_text_ko`,
`occurrences`, bbox·회전·방향·면·시각적 역할·그림 방향을 기록한다. profile의 `exact_text`가
모두 포함돼야 한다. `translation.data.scope`는 기본 `prominent`, 사용자가 명시한 전체 라벨은
`full-label`로 기록한다. `protected_text_kinds`에는 제외한 미세 인쇄 종류를 기록한다.

## 증거와 마스크

모든 stage는 `status`, `evidence`, `data`를 사용한다. evidence는 프로젝트 상대 경로와 현재
SHA-256을 가진다. 검사 미실행은 빈 `pass`가 아니라 `error`, 확률형 애매함은 현재 후보 SHA에
묶인 `review`로 기록한다.

`edit_plan.data.masks`에는 원본 크기의 `old_text`, `new_text`, `editable`, `protected`,
`seam_guard` 경로·SHA·크기를 둔다.

## 비전 패널 편집 기록

`edit_plan.data.compositor`에는 다음을 기록한다.

- `mode: "vision-panel-localization"`
- `fixed_font_used: false`
- `single_pass_panels: true`
- 영역별 확정 문자열, bbox, 회전, 방향, 모델 서명과 1 이상의 `generation_attempts`
- 연결 면 `panel_id`, 생성 패널 NFC 완전일치 `panel_ocr`, 원본 좌표 복원을 증명하는
  `panel_transform`
- 원본/결과 typography signature와 `typography_checks`
- 원본 스타일 crop, 생성 패널, 선택 글자 패치와 마스크의 경로·SHA

`typography_checks`에는 `font_character_matched`, `style_matched`, `size_matched`,
`alignment_matched`, `spacing_matched`, `direction_exact`, `effects_matched`, `surface_matched`를
모두 `true`로 둔다. `ink_height_delta_ratio`, `ink_width_delta_ratio`,
`bbox_coverage_delta_ratio`는 각각 0~0.10, `rotation_delta_deg`의 절댓값은 2.0 이하여야 한다.

## 후보와 생성 후 검사

`candidate_validation.data`에는 최소한 다음 값을 둔다.

- `resized: false`
- `alpha_equal: true`
- `changed_outside_editable: 0`
- `changed_inside_protected: 0`
- `changed_inside_seam_guard: 0`
- 원본·후보 크기, 색 모드와 SHA-256

`post_ocr.data`에는 `candidate_sha256`, `engine_signature`,
`forbidden_foreign_detected: false`, `expected_text_matched: true`,
`duplicate_text_detected: false`, `match_mode: "nfc-literal"`,
`oriented_region_ocr_complete: true`를 둔다. 공백·줄바꿈·구두점·숫자·단위를 버리는 부분일치나
전체 Texture2D OCR 결과를 후보 통과 근거로 사용하지 않는다.

`forbidden_foreign_detected`는 `editable`과 겹치는 검출만 뜻한다. `protected`에서 원본과 픽셀이
같은 외국어 미세 인쇄는 허용하며, 후보 픽셀 검사로 보존을 증명한다.

`post_visual.data`에는 같은 후보 SHA와 번역·방향·그림·색·선명도·seam 판정에 더해
`font_character_matched`, `lettering_style_matched`, `lettering_shape_matched`,
`lettering_size_matched`, `lettering_bbox_coverage_matched`, `lettering_alignment_matched`,
`lettering_spacing_matched`, `lettering_rotation_matched`, `lettering_direction_matched`,
`lettering_effects_matched`, `surface_integration_matched`, `old_logo_silhouette_absent`를 모두
`true`로 둔다.

## 재질·release 측정값

`material_validation.data`에는 derive 전 serialized assets file과 path ID를 포함한 실제
binding, 공유 소비자, D/N/G별 정책, 공통 글리프 마스크 SHA와 입력 증거를 둔다.
`graph_scope`는 `resolved`여야 한다. derive 뒤의 변경 픽셀과
채널 보존은 `workspace/derived.json`의 실측값으로 검증한다. 새 효과 producer가 생기면 좌표
오차도 이 manifest에서 검증한다. 모든 binding과 shared consumer에는 texture identity와 함께
슬롯 `scale`·`offset`을 기록한다.

`auxiliary_contract`는 `schema_version: 1`,
`mode: "source-base+master-lettering-alpha-v1"`과 다음을 포함한다.

- `master_geometry: "selected-lettering-continuous-alpha"`
- `whole_map_generation_used: false`, `binary_new_text_resampled: false`
- `source_maps_immutable_outside_effect_masks: true`
- `master_lettering`: region ID별 candidate의 `selected_lettering_sha256`과
  `lettering_mask_sha256`
- `maps`: `policies`와 같은 key의 bundle/path ID/texture/role/크기/포맷/UV ST, source-map
  경로·SHA, policy, 전체 생성 여부와 공유 호환성
- 수정 맵의 channel semantics 확인 방식·증거 SHA, packing·사용 채널·linear 처리와
  neutralization signature
- 맵별 old-effect mask SHA와 neutralized-base fingerprint
- 절차 파생 `derivation` v1의 master/target 크기·동일한 양수 UV scale과 동일 offset·U/V
  Repeat·V축·texel-center·정수 area resampling, 전체 대상 region, 효과 측정·파라미터,
  허용오차와 역할별 producer signature

전체 예시와 정책별 요구사항은 [auxiliary-maps.md](auxiliary-maps.md)를 따른다. material 단계는
derive 입력과 계약을 검증하고, derive manifest가 실제 변경 픽셀과 output SHA를 기록한다.
v1 범위 밖 정책을 빈 증거 또는 수동 생성 맵으로 `pass`시키지 않는다.

- `mip_validation.data`: `checked_mips`, `missing_mips`
- `bundle_validation.data`: `layout_equal`, `bytes_equal_outside_payloads`, `roundtrip_passed`
- `runtime_validation.data`: `capture_matrix`, `foreign_text_detected`, `alignment_passed`,
  `seam_passed`
- `release_validation.data`: `input_hashes`, `report_hashes`, `bundle_hashes`
