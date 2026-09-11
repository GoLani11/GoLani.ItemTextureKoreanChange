# 품목별 이미지 편집

`prepare`가 만드는 `source.json`에는 실제 Material에서 찾은 컬러·Normal·Gloss의 ID와 원본이 있습니다.
파일명 접미사로 연결을 추정하지 않습니다. 원본은 수정하지 않고 `candidate.png`와 `editable.png`만 갱신합니다.
`job.json`의 `exact_text`와 `notes`는 이번 품목에 확정할 문구를 기록하는 곳입니다.
현재 파일 검사는 OCR을 실행하거나 그 문구의 정확성을 자동 승인하지 않습니다.

## 이미지 생성과 합성

원본 라벨을 이미지 편집 도구에 참고 이미지로 제공하고, 번역할 문구를 정확히 지정합니다.
모양·글꼴 인상·비율·색·배치를 원본에 맞추되, 생성 도구가 비문자 픽셀까지 보존했다고 가정하지 않습니다.
원문 글자 제거 영역과 최종 글자 레이어를 별도로 준비합니다. 모든 입력은 원본 크기이며 회전·확대된 작업 패널은
선택한 글자 레이어만 원본 좌표로 되돌립니다. 전체 텍스처를 리사이즈하지 않습니다.

작업 폴더 안에 다음 입력을 준비합니다.

- `layers/old-text.png`: 원문 글자 제거 영역, 0/255 L 마스크.
- `layers/background.png`: 원문을 제거한 배경판, 원본 크기 RGBA. 마스크 안 RGB만 사용합니다.
- `layers/lettering.png`: 원본 좌표의 최종 한글 RGBA. 글자 밖 알파는 0입니다.

`compose.json`의 경로는 **레시피 파일이 아닌 job.json의 폴더 기준**입니다.
`map_id`는 해당 작업의 `source.json`에서 컬러 맵 ID를 사용합니다.

```json
{
  "map_id": "diffuse-실제ID",
  "old_text": "layers/old-text.png",
  "background": "layers/background.png",
  "lettering": "layers/lettering.png"
}
```

```bat
.venv\Scripts\python.exe localize.py compose workspace/items/mayo/job.json workspace/items/mayo/compose.json
```

결과는 원본 위에 배경 제거 영역과 글자 알파만 합성합니다. 원본 Diffuse 알파는 그대로 남습니다.
마스크는 원문 제거와 새 글자 영역의 합집합으로 기록하며, UV 경계 변경은 중단합니다.
`reports/lettering.json`은 사용한 글자 파일과 후보의 해시를 기록합니다.

## 같은 글자로 보조맵 효과 만들기

Normal·Gloss에 원래 글자 효과가 없는 평면 인쇄는 원본을 유지합니다.
원래 요철·광택 효과가 있다면 **각 맵의 원문 효과만 제거한 neutral RGBA**와 **old-effect 마스크**를 준비합니다.
컬러 이미지의 넓은 마스크를 그대로 재사용하지 않습니다.

`selection`은 컬러 글자 중 실제 재질 효과가 필요한 글자만 고르는 선택적 L 마스크입니다.
예를 들어 뚜껑 글자에만 효과가 있다면 뚜껑 글자 영역을 선택합니다. 새 글자 모양은 항상 합성에 사용한 RGBA 알파에서 가져옵니다.

```json
{
  "selection": "layers/lid-selection.png",
  "maps": [
    {
      "map_id": "normal-실제ID",
      "neutral": "layers/normal-neutral.png",
      "old_effect": "layers/normal-old-effect.png",
      "height_scale_texels": 0.2,
      "polarity": 1,
      "bevel_passes": 1
    },
    {
      "map_id": "gloss-실제ID",
      "neutral": "layers/gloss-neutral.png",
      "old_effect": "layers/gloss-old-effect.png",
      "channel_deltas": {"R": 8, "G": 8, "B": 8}
    }
  ]
}
```

위 효과 수치는 형식 예시입니다. 방향·강도·채널은 각 원본 재질에 맞춰 정합니다.

```bat
.venv\Scripts\python.exe localize.py derive workspace/items/mayo/job.json workspace/items/mayo/derive.json
```

같은 양수 UV scale·offset, U/V Repeat, 동일 크기 또는 2의 거듭제곱 축소에서만 파생합니다.
다른 컬러와 공유하는 보조맵은 변경하지 않습니다. 현재 Normal producer는 DXT5nm의 G/A 방향 채널을 계산하고 R/B를 보존합니다.
Gloss는 지정한 선형 RGB 채널 변화만 적용하며 알파를 보존합니다. 지원하지 않는 채널 구성은 자동 처리하지 않습니다.

한글 문구·배치·원문 잔상·효과의 자연스러움은 비교 이미지와 게임에서 확인합니다.
