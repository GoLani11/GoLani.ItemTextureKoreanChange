# 아이템 텍스처 한글화 자동화 설계 (반자동 버전)

> 목표: 옛날엔 손으로 하던 "에셋 추출 → 이미지 수정 → 다시 적용"에서
> **추출·리팩·패키징은 스크립트로 자동화**하고, **이미지 수정만 사람이 GPT로 반자동** 처리한다.
> 게임 폴더: `D:\SPT`

---

## 1. 한눈에 보는 전체 흐름

```
게임 .bundle (영어 라벨)
      │  ① extract.py (UnityPy): 텍스처 → PNG 자동 추출
      ▼
   원본 PNG  (work/1_raw/)
      │  ② 사람이 GPT 그림으로 한글 라벨 PNG 제작  ← 반자동(수동)
      ▼
  한글 PNG  (work/2_edited/, 같은 파일명으로 저장)
      │  ③ repack.py (UnityPy): PNG → 원래 번들에 자동 삽입
      ▼
  수정된 .bundle  →  bundles/ 폴더 + bundles.json
      │  ④ SPT 모드가 원본을 덮음
      ▼
   게임에서 한글 라벨 표시
```

**자동 vs 수동 분업**
- 🤖 **자동**(스크립트): ① 추출, ③ 리팩, ④ 패키징
- ✋ **수동**(사람+GPT): ② 이미지 한글화 — GPT에 원본 PNG 주고 "글자만 한글로, 나머지 유지"로 생성 → 다듬어서 저장

> ComfyUI·Pillow 자동 편집은 **이번엔 안 씀**. (원본 폰트 그대로 재현이 어려워 GPT로 우회.)
> 나중에 자동화하고 싶으면 ②만 스크립트로 갈아끼우면 됨 — 나머지 파이프라인은 그대로.

---

## 2. 폴더 구조

```
GoLani.ItemTextureKoreanChange/
├─ src/mod.ts              # SPT 모드 (번들 로더 — 그대로 둠)
├─ bundles/                # ★ 수정된 .bundle 결과물 (게임 경로 그대로 미러링)
│   └─ assets/content/items/food/item_food_mayo/item_food_mayo.bundle
├─ bundles.json            # ★ 덮을 번들 목록 (이미 54개 등록됨)
│
├─ tools/                  # ★ 자동화 스크립트 (신규)
│   ├─ extract.py          # ① 번들 → PNG
│   ├─ repack.py           # ③ PNG → 번들
│   └─ map.json            # 추출/리팩 진행 상황 기록(자동 생성)
└─ work/                   # ★ 작업용 PNG (git 제외)
    ├─ 1_raw/              #   추출 원본  (extract.py가 채움)
    └─ 2_edited/           #   GPT로 한글화한 PNG (사람이 채움, 같은 파일명)
```

> `bundles/`는 게임 안 경로(`assets/content/...`)를 **그대로 복사한 구조**여야 SPT가 매칭함.
> `bundles.json`의 `key`와 `bundles/` 아래 폴더 경로가 일치해야 함.

---

## 3. 작업 단위 — 어떻게 매칭하나

핵심은 **원본 PNG와 수정 PNG의 파일명을 똑같이** 두는 것. 그래야 스크립트가 "어느 텍스처를 어느 그림으로 바꿀지" 자동으로 안다.

```
work/1_raw/item_food_mayo__item_food_mayo_BaseColor.png      ← 추출 원본
work/2_edited/item_food_mayo__item_food_mayo_BaseColor.png   ← GPT로 한글화 (같은 이름)
```

파일명 규칙: `<번들이름>__<텍스처이름>.png`
- `extract.py`가 이 규칙으로 저장 + `map.json`에 "이 PNG ↔ 이 번들/텍스처" 기록
- `repack.py`는 `2_edited/`의 PNG를 `map.json` 보고 원래 번들에 도로 넣음

→ 별도 레시피 JSON 직접 작성 불필요. `extract.py`가 알아서 만든다.

---

## 4. 스크립트 상세

### ① 추출 — `tools/extract.py` (UnityPy)
- 입력: `bundles.json`의 `key` 목록 → 게임 폴더에서 해당 `.bundle` 찾음
  (`D:/SPT/EscapeFromTarkov_Data/StreamingAssets/Windows/<key>`)
- 각 번들 열어 `Texture2D` 전부 PNG로 추출 → `work/1_raw/`
- `map.json`에 `{png파일명: {bundle, gamePath, textureName}}` 기록
- 라벨 있는 텍스처는 보통 `BaseColor`/`Albedo` (Normal·Mask는 글자 없음 → 무시해도 됨)

### ② 한글화 — 사람 + GPT (수동)
1. `work/1_raw/`의 PNG를 GPT(이미지 생성)에 올림
2. 프롬프트 예: *"이 라벨의 영어 글자만 한글로 바꿔줘. 글씨 위치·크기·색·배경은 그대로 유지."*
3. 결과 받아서 어긋난 부분 손보고(필요 시), **같은 파일명**으로 `work/2_edited/`에 저장
   - ⚠️ 해상도(가로×세로)는 원본과 **동일하게** 유지 (다르면 게임에서 깨질 수 있음)

### ③ 리팩 — `tools/repack.py` (UnityPy)
- `work/2_edited/`의 PNG들을 `map.json` 보고 매칭
- 원본 번들 다시 열어 해당 `Texture2D`에 `set_image(새PNG)` → `save()`
- 결과를 `bundles/<게임경로>/...bundle`로 저장

### ④ SPT 적용
- `bundles.json`에 키 등록 (이미 됨)
- `src/mod.ts` 그대로 — SPT 번들 로더가 `bundles/` 파일로 원본 덮음
- **테스트마다 런처에서 "임시 파일 삭제"** 필수 (번들 캐시 때문)

---

## 5. 테스트 루프 (한 아이템)

```
1. python tools/extract.py             # 또는 특정 번들만: extract.py item_food_mayo
2. work/1_raw/ 에서 마요네즈 PNG 확인
3. GPT로 한글화 → work/2_edited/ 에 같은 이름으로 저장
4. python tools/repack.py
5. 런처 → 임시 파일 삭제 → 게임 실행 → 인벤토리 확인
6. 어긋나면 3번(이미지)만 다시 → 4·5 반복
```

---

## 6. 로드맵 (작은 것부터)

| 단계 | 내용 | 검증 포인트 |
|------|------|------------|
| **0. POC** | 마요네즈 1개: `extract → (PNG 아무거나 표시로 수정) → repack` | **게임이 수정 번들을 인식하는가** (제일 위험) |
| 1. 추출 일괄 | 54개 번들 전부 PNG 추출 | 라벨 텍스처가 제대로 뽑히나 |
| 2. GPT 한글화 | 아이템별로 GPT로 PNG 제작 | 글자·색·위치 자연스러운가 |
| 3. 리팩 일괄 | `repack.py`로 전부 번들 생성 | 빌드 한 번에 전체 적용 |
| 4. 배포 | `bundles/` 묶어서 모드 배포 | 게임에서 전체 한글 확인 |

> **0단계가 핵심.** 추출·리팩이 게임에서 먹히는지부터 확인해야 나머지가 의미 있음.

---

## 7. ⚠️ 가장 큰 함정 — PNG 리팩 시 "밝아짐 / 깨짐"

유니티 텍스처를 PNG로 교체하면 **엄청 밝아지거나(washed out) 깨지는** 유명한 문제가 있다. 증상 2개 = 원인 2개:

| 증상 | 원인 | 해결 |
|------|------|------|
| **깨짐 / 블록 얼룩** | 저장 시 원본 압축 포맷 안 지킴 | 원본 `m_TextureFormat` 그대로 재인코딩 |
| **엄청 밝아짐 / 색바램** | sRGB(감마) 정보 손실 | (위와 동일) 포맷 유지하면 sRGB도 보존됨 |

**왜 밝아지나**: 타르코프는 Linear(선형) 렌더링 게임. 라벨 텍스처는 "sRGB"로 표시돼 있어 GPU가 그릴 때 감마 변환을 자동으로 해준다. PNG 교체 중 **텍스처 포맷이 바뀌면 이 sRGB 표시가 사라져** GPU가 변환을 건너뛰고 그대로 출력 → 밝게 뜬다.

**UnityPy 소스 확인 결과**(`Texture2DConverter.image_to_texture2d`):
- 감마 보정은 안 건드림. 픽셀 그대로 인코딩만 함 → UnityPy가 색을 망치는 게 아님.
- **포맷을 지정 안 하면 원본 압축(BC7/DXT)이 RGBA32(무압축)로 바뀔 수 있음** → 밝아짐 + 용량 폭증.

**해결 코드 패턴**:
```python
orig_fmt = tex.m_TextureFormat                    # ① 원본 포맷 기억
tex.set_image(new_png, target_format=orig_fmt)    # ② 같은 포맷으로 재인코딩
```
> BC7/DXT 재압축은 etcpak/texconv 인코더 필요. 없으면 깨짐/품질저하.
> 출처: [Unity 이슈 트래커 - crunched bundle too bright](https://issuetracker.unity3d.com/issues/crunched-asset-bundle-sprites-are-too-bright-when-loaded-from-file-in-play-mode)

**POC 0단계는 이걸로 검증**: 이미지 안 바꾸고 추출→그대로 리팩→게임에서 원본과 동일하게 보이면 포맷 유지 OK. 밝아지면 포맷이 바뀐 것.

---

## 8. ♻️ 기존 도구 재사용 — SPT Asset Editor

추출·리팩 스크립트를 **처음부터 짤 필요 없음.** 이미 만들어둔 도구가 있다:
[**GoLani11/SPT_Asset_Editor**](https://github.com/GoLani11/SPT_Asset_Editor) — UnityPy + texture2ddecoder + Pillow 기반, Texture2D 추출·교체·복원 + **원본 해상도 자동 맞춤**.

- 1순위: 이 도구로 마요네즈 추출→리팩 테스트 (위 밝아짐 검증 포함)
- 포맷 유지가 잘 되면 → 이걸 배치(여러 개 한 번에) 돌리게 확장
- 안 되면 → `target_format` 고정 로직만 보강

> `tools/extract.py`·`repack.py`는 SPT Asset Editor의 핵심 로직을 가져다 배치용으로 감싸는 정도면 충분.

---

## 9. 그 외 리스크 / 주의

- **번들 버전 호환**: EFT는 Unity 2019.x. POC에서 추출·리팩이 게임에 먹히는지 먼저 확인.
- **해상도 유지**: GPT 결과가 원본과 크기 다르면 깨짐. 원본 해상도로 맞춰 저장. (SPT Asset Editor가 자동 맞춤 지원)
- **텍스처 여러 개**: 한 번들에 BaseColor·Normal 등 여러 개. 글자 있는 건 BaseColor류. 추출 후 눈으로 확인.
- **업스케일 = 로딩 렉**: README 경고대로 4096은 무거움. 원본 해상도 유지 권장.
- **임시 파일 삭제**: 안 하면 적용 안 된 듯 보임. 디버깅 1순위 의심.
- **work/ git 제외**: 임시 PNG. `.gitignore`에 추가.

---

## 부록: 파이썬 환경

```bash
pip install UnityPy Pillow
```
> Pillow는 PNG 입출력 보조용(글자 찍기엔 이번엔 안 씀).
