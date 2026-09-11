# 파일 검증과 배포

검증은 파일 검사, 시각 확인, 게임 확인을 구분합니다. 한 품목이 다른 품목의 완료를 기다릴 필요가 없습니다.

## 자동 파일 검사

`validate <job.json>`은 원본 파일 해시, RGBA 크기, 변경 허용 마스크, 보존 채널, UV 경계,
공유 보조맵 변경을 확인합니다. 실패하면 종료 코드 1을 반환하고 `reports/validation.json`에 결과를 남깁니다.
파일 자체를 읽을 수 없거나 원본 해시가 다르면 오류로 중단합니다.
비교 이미지는 원본·후보·변경 픽셀을 나란히 표시합니다.

`package <job.json>`은 검사를 다시 수행하고 다음을 확인합니다.

- 실제 Texture2D의 bundle/assets-file/path-id와 원본 추출 PNG 일치.
- 원본 해상도, 압축 포맷과 모든 밉 수 유지.
- 변경 없는 텍스처의 원본 압축 payload 유지.
- 변경 있는 텍스처의 편집 블록 밖 바이트와 보존 채널 유지.
- 편집 블록의 채널별 평균·99백분위·최대 압축 오차.
- UnityFS 디렉터리, serialized object와 비대상 payload 보존.
- 재개봉한 텍스처 payload 및 패키징 시작·종료 입력 해시 일치.

현재 블록 패키징은 DXT1·DXT5·BC7과 streamed texture를 지원합니다. 압축된 UnityFS 데이터 블록의
수정이나 비영(非零) 컨테이너 데이터 해시 등 원본 구조를 유지할 수 없는 경우는 중단합니다.
밉은 컬러는 선형광, Normal은 방향 벡터, Gloss는 선형 채널로 편집 변화량을 계산해 원본 밉에 적용합니다.
편집과 무관한 원본 밉의 필터링을 새로 만들지 않습니다.
DXT5 컬러 알파 블록은 원본을 복사합니다. 다른 포맷도 재디코딩 뒤 보존 채널이 달라지면 중단합니다.

**무손실 보존의 범위:** PNG 후보에서는 마스크 밖 픽셀이 원본과 같아야 합니다. 압축 뒤에는 4×4 블록 단위로
보존하므로 편집 블록 내부의 주변 픽셀은 압축 오차를 포함할 수 있습니다. 하위 밉의 효과 범위도 달라집니다.
시각 검사는 이런 부분과 작은 글자 가독성을 확인해야 합니다.

## 테스트 패키지

```text
packages/<id>/
├─ package.json
├─ roundtrip/                         맵별 재디코딩 밉
└─ GoLani-ItemTextureKoreanChange/
   ├─ GoLani.ItemTextureKoreanChange.dll
   ├─ GoLani.ItemTextureKoreanChange.deps.json
   ├─ bundles.json
   ├─ bundles/                       해당 품목의 번들만 포함
   └─ TEST-ONLY.txt
```

패키지는 원본 스냅샷에 저장한 게임 카탈로그의 실제 의존성을 사용합니다.
`package.json`과 파일 목록은 고정된 결과이며 이후 수정하지 않습니다. 같은 입력의 기존 패키지는
파일을 다시 검증한 뒤 재사용합니다. 검토 기록은 패키지 밖에 둡니다.
패키지 ID에는 제작 코드와 압축 라이브러리 버전도 포함합니다. 도구나 압축 정책이 바뀌면 별도 패키지를 만듭니다.

SDK는 [Microsoft 공식 설치 스크립트](https://dotnet.microsoft.com/en-us/download/dotnet/scripts)로
작업 폴더에 설치할 수 있습니다. 예를 들어 로컬 SDK를 사용한다면 다음처럼 실행합니다.

```bat
.venv\Scripts\python.exe localize.py package workspace/items/mayo/job.json --dotnet workspace/toolchain/dotnet/dotnet.exe
```

## 게임 확인 후 배포본 만들기

실제 게임에서 글자·색·사광의 요철·광택·원문 잔상·거리별 밉을 확인한 뒤 검토 기록을 작성합니다.
아래 값은 형식 예시이며 실제 확인 전에는 true로 기록하지 않습니다.
`package_sha256`은 **package.json 파일**의 SHA-256입니다. `evidence` 경로는 검토 JSON 폴더 기준입니다.

```json
{
  "package_sha256": "검사한-package.json의-SHA256",
  "visual_reviewed": true,
  "runtime_tested": true,
  "reviewer": "확인한 사람",
  "notes": "확인한 게임 버전과 관찰 결과",
  "evidence": [
    {"path": "evidence/game-view.png", "sha256": "이미지의-SHA256"}
  ]
}
```

```bat
.venv\Scripts\python.exe localize.py release workspace/items/mayo/packages/패키지ID --review workspace/items/mayo/runtime-review.json --output workspace/releases/mayo-v1
```

코드는 검토 기록과 증거 파일이 해당 패키지에 연결되었는지 검증합니다. 실제 게임을 실행하거나 기록의 관찰 내용을 대신 판단하지 않습니다.
편집이 전혀 없는 원본 패키지는 현지화 배포본으로 승격하지 않습니다.
배포본에는 검토 기록과 증거를 복사하고 테스트 패키지 자체는 보존합니다.
설치와 외부 배포는 별도 작업입니다.
