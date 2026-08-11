# 작업 흐름

## 1. 범위 확인

`profiles/food/collection.json`의 bundle key와 target을 기준으로 삼아라. SPT 데이터베이스의
Food/Drink 항목과 대조하고 누락된 제품 또는 새 버전의 텍스처를 별도 보고하라.

```bash
python localize.py --spt-root D:/SPT inventory
python localize.py --spt-root D:/SPT extract
python localize.py status
```

`missing_bundles`가 비어 있고 target 수가 프로필과 같아야 다음 단계로 진행할 수 있다.

## 2. 판독과 번역

원본 검수 시트를 만든 뒤 각 원본을 개별 해상도로 확인하라.

```bash
python localize.py review-sheets
```

제품명, 종류, 맛, 함량, 경고, 제조 정보와 개봉 지시를 구분하라. 읽을 수 없는 미세 문구는
추측해 생성하지 말고 프로필 notes에 불확실성을 남겨라.

## 3. 시안 생성

각 PNG를 별도의 edit target으로 사용하라. exact_text를 그대로 렌더링하고 숫자, 화살표,
등록상표 기호처럼 보존 대상으로 정한 문자는 그대로 두어라. 출력은 `workspace/drafts/<id>/`
아래에 복사하고 최종 프롬프트도 같은 폴더에 저장하라.

## 4. 승인

원본과 나란히 비교하고 정확한 글자, 위치, 회전, 크기, 색, 마모를 확인하라. 생성기가 UV나
질감을 다시 그렸다면 필요한 인쇄 영역만 원본 위에 합성하라.

```bash
python localize.py stage <target-id> <candidate.png>
python localize.py review-sheets --approved
python localize.py validate
```

## 5. 보조맵과 재패킹

diffuse 인쇄 위치가 normal/gloss에 실제로 반영된 경우에만 원본 강도로 이식하라. 원본이
평면 인쇄면 보조맵에 새 요철이나 광택을 만들지 마라.

동일 포맷과 밉 수로 인코딩한 payload 길이가 원본 stream size와 같을 때만 UnityFS의 해당
논리 범위를 교체하라. 결과 번들을 다시 열어 모든 메타데이터와 왕복 이미지를 검사하라.

## 6. 설치

사용자의 명시적 요청 전에는 `workspace/bundles/`까지만 만든다. 설치 시 원본 번들과
`Windows.json`을 해시와 함께 백업하고, 실패하면 두 파일을 모두 원상 복구하라.
