# 한글 D 프롬프트 작성

원본 한 장을 통째로 참고해 포장 인쇄를 교체한다. 생성 전 원본을 확대해 읽고,
위치별 원문·한글·색상·인쇄 느낌·보존 요소를 정리한다. 큰 글자만 남기고 작은 설명을
자동 제외하지 않는다. 육포 작업의 뒷면 작은 글씨 제외는 당시 시안 범위였으며 공통 규칙이 아니다.

아래 형식을 품목에 맞게 채운다. 지시문 언어보다 정확한 한글과 위치·스타일 구분이 중요하다.

```text
Use case: text-localization.
Edit target: the supplied original game diffuse texture atlas, <width> x <height>.
This is a FLAT UV TEXTURE, not a product photo or a 3D mockup.
Keep the same canvas, atlas islands, panel positions, proportions, folds,
seams, photographs, illustrations, borders, wear and baked lighting.

Localize all legible packaging text listed below into the exact Korean phrases.
Match each original's position, orientation, weight, color and ink texture.
Text replacements:
- <location>: "<original>" -> "<exact Korean>"; <specific lettering style>.
- <location of small information panel>: "<original>" -> "<exact Korean>";
  preserve all quantities and units; use readable Korean line breaks within this panel.

Preserve: <item-specific graphics, numeric codes, barcodes, symbols and decorations>.
Leave unchanged: <unreadable text or explicit user exclusions; omit if none>.
Do not invent ingredients, claims, slogans, dates, quantities or unreadable words.
Do not leave original-language remnants inside the replaced text zones.
Do not add captions, borders, watermarks, comparison panels or a presentation background.
Output only one localized flat diffuse texture atlas at the requested dimensions.
Do not generate normal or gloss maps.
```

육포에서 사용한 스타일 지정의 예:

- 원형 로고의 `Tarker` → `타르커`: 원본처럼 흰색의 불규칙하고 둥근 손글씨. 원형 테두리와 주변 장식 유지.
- 검은 제품명 → `쇠고기 육포`: 좁고 곧은 검은 글씨, 원본의 위치와 시각적 비중 유지.
- 회색 `DRIED MEAT` → `말린 고기`: 옅은 회색과 가는 획 유지.

위 문구는 육포 예시다. 다른 품목에 넣거나 원문과 다른 제품 의미를 추가하지 않는다.
회전된 글자는 자연스러운 한글 어구를 조판한 뒤 원문 방향에 맞춘다. 원문 글자 수에 맞춰
한글 음절을 임의로 쌓거나 과도한 자간·늘임을 만들지 않는다.

프롬프트의 보존 요청은 실제 픽셀 보존 검증을 대신하지 않는다. 출력이 원본과 다른 크기라면
실제 크기를 밝히고, 선택 후 `adopt-draft`로 맞춘다. 글자 오탈자·배치 변경·누락이 보이면
구체적인 수정 지시를 제시한다. 같은 실패를 해결하지 못한 채 생성을 반복하지 않는다.
