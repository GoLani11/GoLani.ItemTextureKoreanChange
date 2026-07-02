"""
아이템 텍스처 한글화 원클릭 파이프라인.

SPT_Asset_Editor의 핵심 로직(UnityPy 추출/교체 + 원본 포맷 유지)만 가져온 CLI.
GUI 클릭 없이 명령 한 줄(또는 .bat 더블클릭)로 동작.

사용법:
  python tools/auto.py extract [필터]   # 게임 번들 → work/1_raw/ 로 PNG 추출
  python tools/auto.py hires   [필터] [size] [lambda]  # inspect용 BC7 RDO 생성
  python tools/auto.py build   [필터]   # _D 기준 _N/_G 생성 + 번들 리팩 (derive+repack)
  python tools/auto.py all     [필터] [size] [lambda]  # derive+repack+hires+deploy
  python tools/auto.py index   [경로prefix]  # 게임 번들 텍스처명 인덱스 생성/갱신
  python tools/auto.py find    <부분문자열>  # 인덱스에서 텍스처명 검색
  python tools/auto.py deploy           # DLL 빌드 + SPT user/mods 에 모드 설치
  (세부: derive=보조맵 생성만, repack=번들만)

흐름:
  1. extract → work/1_raw/ 에 PNG들 + map.json 생성
  2. (사람) GPT로 _D(컬러)만 한글화 → 같은 파일명으로 work/2_edited/ 에 저장
  3. build   → 원본 _N/_G 값체계 유지 + 디자인 위치만 _D 기준 이식 + 번들 생성
  4. deploy  → C# DLL 빌드 + bundles/ 를 user/mods 에 설치 (런처 임시파일 삭제 필요)

핵심: 사람은 _D 한 장만 만들면 됨. _N/_G는 원본 스타일 유지하며 글자만 한글로(maps.py).
[필터]: 번들 key에 포함된 문자열로 일부만 처리 (예: item_food_mayo). 생략 시 전체.
"""
import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

# ── 설정 ───────────────────────────────────────────────
SPT_DIR = os.environ.get("SPT_DIR", "D:/SPT")
GAME_ROOT = os.path.join(SPT_DIR, "EscapeFromTarkov_Data/StreamingAssets/Windows")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(PROJ, "work", "1_raw")
EDITED_DIR = os.path.join(PROJ, "work", "2_edited")
DEBUG_DIR = os.path.join(PROJ, "work", "_debug")
OUT_DIR = os.path.join(PROJ, "bundles")
HIRES_DIR = os.path.join(PROJ, "hires")
MAP_PATH = os.path.join(PROJ, "tools", "map.json")
BUNDLES_JSON = os.path.join(PROJ, "bundles.json")

# 배포 대상 (SPT 4.x: 서버는 D:/SPT/SPT, 모드는 그 아래 user/mods)
MOD_NAME = "GoLani-ItemTextureKoreanChange"
MODS_DIR = os.path.join(SPT_DIR, "SPT", "user", "mods", MOD_NAME)
CSPROJ = os.path.join(PROJ, "GoLani.ItemTextureKoreanChange.csproj")
DLL_OUT = os.path.join(PROJ, "bin", "Release", "net9.0")
HIRES_DLL = os.path.join(PROJ, "client", "GoLani.HiResInspect", "bin", "Release", "net471", "GoLani.HiResInspect.dll")
HIRES_PLUGIN_DIR = os.path.join(SPT_DIR, "BepInEx", "plugins", "GoLani.HiResInspect")
PICKER_DLL = os.path.join(PROJ, "client", "GoLani.AssetPicker", "bin", "Release", "net471", "GoLani.AssetPicker.dll")
PICKER_PLUGIN_DIR = os.path.join(SPT_DIR, "BepInEx", "plugins", "GoLani.AssetPicker")
TEXINDEX_PATH = os.path.join(PROJ, "tools", "texindex.json")
BC7ENC = os.path.join(PROJ, "tools", "bin", "bc7enc")

BC7 = 25  # TextureFormat.BC7
DXT5 = 12  # TextureFormat.DXT5 (BC3) 폴백용


def _unitypy():
    """UnityPy는 쓰는 명령에서만 로드."""
    import UnityPy
    return UnityPy


def _image():
    """Pillow는 쓰는 명령에서만 로드."""
    from PIL import Image
    return Image


def _manifest_entries(flt=None):
    """bundles.json에서 manifest 항목을 읽음 (필터 적용)."""
    with open(BUNDLES_JSON, encoding="utf-8") as f:
        manifest = json.load(f)["manifest"]
    return [m for m in manifest if not flt or flt in m["key"]]


def _keys(flt=None):
    """bundles.json에서 번들 key 목록을 읽음 (필터 적용)."""
    return [m["key"] for m in _manifest_entries(flt)]


def _asset_type(key, manifest_entry=None):
    """번들 key/manifest로 item|map 판별."""
    if manifest_entry and manifest_entry.get("assetType") in ("item", "map"):
        return manifest_entry["assetType"]
    k = key.lower()
    if "/items/" in k or "/usable_items/" in k:
        return "item"
    return "map"


def _png_name(key, tex):
    """번들 key + 텍스처 이름 → 충돌 없는 PNG 파일명. ('/'는 키마다 중복되니 인코딩)"""
    return key.replace("/", "@") + "__" + tex + ".png"


def _full_mip_count(width, height):
    """최종 크기 기준 풀 밉 체인 개수."""
    return int(math.floor(math.log2(max(width, height)))) + 1


def extract(flt=None):
    UnityPy = _unitypy()
    os.makedirs(RAW_DIR, exist_ok=True)
    entries = []
    for key in _keys(flt):
        path = os.path.join(GAME_ROOT, key)
        if not os.path.exists(path):
            print(f"[건너뜀] 게임에 없음: {key}")
            continue
        env = UnityPy.load(path)
        n = 0
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            fn = _png_name(key, data.m_Name)
            data.image.save(os.path.join(RAW_DIR, fn))
            entries.append({"png": fn, "key": key, "texture": data.m_Name})
            n += 1
        print(f"[추출] {key}  (텍스처 {n}개)")
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"\n완료: PNG {len(entries)}개 → {RAW_DIR}")
    print("다음: work/2_edited/ 에 _D(컬러)만 한글화해 같은 파일명으로 저장 후 build")


def _classify(orig_png):
    """원본 텍스처 종류 자동 판별: 'N'(노멀) | 'G'(광택) | 'D'(컬러)."""
    import numpy as np
    Image = _image()
    a = np.array(Image.open(orig_png).convert("RGBA"))
    R, G, B, A = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    if R.min() == 255 and R.max() == 255:
        return "N"  # DXT5nm 노멀: R채널 255 고정
    chan_eq = np.abs(R.astype(int) - G).mean() + np.abs(G.astype(int) - B).mean()
    if chan_eq < 6 and A.mean() > 250:
        return "G"  # 회색조 + 불투명 = 광택맵
    return "D"  # 그 외 = 컬러(디퓨즈)


def derive(flt=None):
    """work/2_edited 의 _D(컬러)만 기준으로 같은 번들의 _N/_G를 생성.
    원본 _N/_G의 값체계·강도는 유지하고 디자인 위치만 _D 기준으로 이식(maps.py).
    + _D 알파 복구. 디버그 마스크는 work/_debug/ 에 저장."""
    import maps  # cv2 의존 → derive 쓸 때만 로드
    Image = _image()

    if not os.path.exists(MAP_PATH):
        sys.exit("map.json 없음. 먼저 extract 실행.")
    with open(MAP_PATH, encoding="utf-8") as f:
        entries = [e for e in json.load(f) if not flt or flt in e["key"]]

    by_key = {}
    for e in entries:
        by_key.setdefault(e["key"], {})[_classify(os.path.join(RAW_DIR, e["png"]))] = e["png"]

    os.makedirs(DEBUG_DIR, exist_ok=True)
    made = 0
    for key, m in by_key.items():
        d_png = m.get("D")
        if not d_png:
            continue
        kor_d = os.path.join(EDITED_DIR, d_png)
        if not os.path.exists(kor_d):
            print(f"[건너뜀] {key}: 편집한 _D 없음 (work/2_edited)")
            continue
        orig_d = os.path.join(RAW_DIR, d_png)
        dbg = os.path.join(DEBUG_DIR, key.replace("/", "@"))
        print(f"[생성] {key}")

        # _N, _G 는 kor_d 원본(GPT) RGB 를 읽으므로 _D 알파복구보다 먼저 처리
        if "N" in m:
            img, info = maps.transplant_normal(os.path.join(RAW_DIR, m["N"]), orig_d, kor_d, debug=dbg)
            img.save(os.path.join(EDITED_DIR, m["N"]))
            print(f"   _N: 원본요철 k={info['k']} → 한글음각 {'추가' if info['added'] else '없음(평면)'}")
        if "G" in m:
            img, info = maps.transplant_gloss(os.path.join(RAW_DIR, m["G"]), orig_d, kor_d, debug=dbg)
            img.save(os.path.join(EDITED_DIR, m["G"]))
            print(f"   _G: 광택차 delta={info['delta']} → 한글적용 {'O' if info['added'] else 'X(차이없음)'}")

        # _D: 한글 컬러 + 원본 알파 복구 (마지막)
        od = Image.open(orig_d).convert("RGBA")
        kd = Image.open(kor_d).convert("RGB").resize(od.size, Image.LANCZOS).convert("RGBA")
        kd.putalpha(od.getchannel("A"))
        kd.save(os.path.join(EDITED_DIR, d_png))
        made += 1

    if made == 0:
        sys.exit("처리할 _D 없음. work/2_edited 에 GPT _D를 (1_raw와 같은 파일명으로) 넣으세요.")
    print(f"\n완료: {made}개 번들 보조맵 생성 → work/2_edited/  (디버그 마스크: work/_debug/)")


def _replace(bundle_path, out_path, replacements, target_size):
    """원본 번들 로드 → 해당 텍스처를 새 PNG로 교체 → out_path 저장."""
    UnityPy = _unitypy()
    Image = _image()
    env = UnityPy.load(bundle_path)
    changed = False
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        new_png = replacements.get(data.m_Name)
        if not new_png:
            continue
        img = Image.open(new_png)
        if target_size is None:
            width, height = data.m_Width, data.m_Height
        else:
            width, height = target_size, target_size
        if (img.width, img.height) != (width, height):
            img = img.resize((width, height), Image.LANCZOS)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        mip = _full_mip_count(width, height)
        fmt = BC7 if data.m_Name.endswith("_D") else data.m_TextureFormat
        try:
            data.set_image(img, target_format=fmt, mipmap_count=mip)
            data.save()
        except Exception as e:
            print(f"   포맷({fmt}) 실패 → DXT5 폴백: {e}")
            fmt = DXT5
            data.set_image(img, target_format=DXT5, mipmap_count=mip)
            data.save()
        changed = True
        print(f"   교체: {data.m_Name} ({width}x{height}, 밉 {mip}, 포맷 {fmt})")
    if changed:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(env.file.save(packer="lz4"))
    return changed


def _wsl_path(path):
    """윈도우 경로를 WSL /mnt/<drive>/... 경로로 바꿈."""
    p = os.path.abspath(path).replace("\\", "/")
    if p.startswith("/mnt/"):
        return p
    if len(p) >= 3 and p[1] == ":" and p[2] == "/":
        return f"/mnt/{p[0].lower()}/{p[3:]}"
    return p


def _dds_payload(dds_path, width, height):
    """bc7enc DDS에서 해당 밉 레벨의 BC7 페이로드만 잘라냄."""
    with open(dds_path, "rb") as f:
        data = f.read()
    if data[:4] != b"DDS ":
        raise ValueError(f"DDS 매직 불일치: {dds_path}")
    offset = 148 if data[84:88] == b"DX10" else 128
    size = math.ceil(width / 4) * math.ceil(height / 4) * 16
    payload = data[offset:offset + size]
    if len(payload) != size:
        raise ValueError(f"DDS 페이로드 크기 불일치: {dds_path} ({len(payload)} != {size})")
    return payload


def _run_bc7enc(src_png, dst_dds, lam):
    """WSL의 bc7enc_rdo로 PNG 한 장을 BC7 DDS로 인코딩."""
    cmd = [
        "wsl", _wsl_path(BC7ENC),
        "-q", "-g", "-u4", "-e", "-y", f"-z{lam}",  # -y: DDS(위→아래)를 Unity(아래→위) 순서로 플립
        _wsl_path(src_png), _wsl_path(dst_dds),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if e.stdout:
            print(e.stdout)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        raise


def hires(flt=None, size=4096, lam=1.0):
    """원본 PNG를 업스케일한 뒤 BC7 RDO 밉 체인으로 저장."""
    Image = _image()
    if not os.path.exists(MAP_PATH):
        sys.exit("map.json 없음. 먼저 extract 실행.")
    if size < 1:
        sys.exit("size는 1 이상이어야 합니다.")
    if not os.path.exists(BC7ENC):
        sys.exit(f"bc7enc 없음: {BC7ENC}")

    with open(MAP_PATH, encoding="utf-8") as f:
        entries = [
            e for e in json.load(f)
            if (not flt or flt in e["key"]) and _asset_type(e["key"]) == "item"
        ]
    if not entries:
        sys.exit("처리할 item 텍스처 없음. 필터를 확인하세요.")

    os.makedirs(HIRES_DIR, exist_ok=True)
    manifest = []
    total_raw = 0
    total_gz = 0
    started = time.perf_counter()

    for idx, e in enumerate(entries, 1):
        edited = os.path.join(EDITED_DIR, e["png"])
        raw = os.path.join(RAW_DIR, e["png"])
        src = edited if os.path.exists(edited) else raw
        if not os.path.exists(src):
            print(f"[건너뜀] 소스 PNG 없음: {e['png']} (extract 먼저 실행)")
            continue

        tex_started = time.perf_counter()
        name = e["texture"]
        print(f"[hires {idx}/{len(entries)}] {name}: {size} BC7 RDO 시작")
        scratch = tempfile.mkdtemp(prefix="golani_hires_")
        try:
            with Image.open(src) as src_img:
                base = src_img.convert("RGBA").resize((size, size), Image.LANCZOS)
            mips = []
            level = 0
            while True:
                w = max(1, size >> level)
                h = max(1, size >> level)
                png = os.path.join(scratch, f"{name}_{level:02d}.png")
                dds = os.path.join(scratch, f"{name}_{level:02d}.dds")
                img = base if w == size and h == size else base.resize((w, h), Image.LANCZOS)
                img.save(png)
                print(f"   밉 {level + 1}: {w}x{h} 인코딩")
                _run_bc7enc(png, dds, lam)
                mips.append(_dds_payload(dds, w, h))
                if w == 1 and h == 1:
                    break
                level += 1

            raw = b"".join(mips)
            out_name = f"{name}.bc7.gz"
            out_path = os.path.join(HIRES_DIR, out_name)
            with gzip.open(out_path, "wb", compresslevel=9) as f:
                f.write(raw)
            gz_size = os.path.getsize(out_path)
            raw_size = len(raw)
            total_raw += raw_size
            total_gz += gz_size
            manifest.append({
                "name": name,
                "file": out_name,
                "width": size,
                "height": size,
                "mipCount": len(mips),
                "format": 25,
                "linear": not name.endswith("_D"),
                "rawSize": raw_size,
            })
            elapsed = time.perf_counter() - tex_started
            print(f"   완료: {elapsed:.1f}초, {raw_size / 1024 / 1024:.1f}MB → {gz_size / 1024 / 1024:.1f}MB")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    with open(os.path.join(HIRES_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    elapsed = time.perf_counter() - started
    print(f"\n완료: 텍스처 {len(manifest)}개 → {HIRES_DIR}")
    print(f"요약: {elapsed:.1f}초, {total_raw / 1024 / 1024:.1f}MB → {total_gz / 1024 / 1024:.1f}MB")


def repack(flt=None):
    UnityPy = _unitypy()
    entries = _manifest_entries(flt)
    if not entries:
        sys.exit("처리할 번들 없음. 필터를 확인하세요.")

    skipped_missing_bundle = 0
    skipped_no_png = 0
    skipped_errors = 0
    done = 0
    for entry in entries:
        key = entry["key"]
        src = os.path.join(GAME_ROOT, key)
        out = os.path.join(OUT_DIR, key)
        if not os.path.exists(src):
            skipped_missing_bundle += 1
            print(f"[건너뜀] 게임에 없음: {key}")
            continue

        replacements = {}
        try:
            env = UnityPy.load(src)
            for obj in env.objects:
                if obj.type.name != "Texture2D":
                    continue
                data = obj.read()
                name = _png_name(key, data.m_Name)
                edited = os.path.join(EDITED_DIR, name)
                raw = os.path.join(RAW_DIR, name)
                if os.path.exists(edited):
                    replacements[data.m_Name] = edited
                elif os.path.exists(raw):
                    replacements[data.m_Name] = raw
        except Exception as e:
            skipped_errors += 1
            print(f"[건너뜀] 번들 읽기 실패: {key}: {e}")
            continue

        if not replacements:
            skipped_no_png += 1
            print(f"[건너뜀] 소스 PNG 없음: {key} (extract 먼저 실행)")
            continue

        typ = _asset_type(key, entry)
        size = 1024 if typ == "item" else None
        size_desc = "1024" if size else "원본크기"
        print(f"[리팩] {key} ({typ}, {size_desc}, 텍스처 {len(replacements)}개)")
        if _replace(src, out, replacements, size):
            done += 1
    print(f"\n완료: 번들 {done}개 → {OUT_DIR}")
    print(f"요약: 게임에 없음 {skipped_missing_bundle}개, 소스 PNG 없음 {skipped_no_png}개, 오류 {skipped_errors}개")
    if done == 0:
        sys.exit("생성한 번들 없음. extract 먼저 실행했는지 확인하세요.")
    print("다음: SPT 런처에서 '임시 파일 삭제' 후 게임 실행")


def index(prefix=None):
    """게임 번들의 Texture2D 이름 → 번들 key 인덱스 생성/갱신."""
    UnityPy = _unitypy()
    if not os.path.exists(GAME_ROOT):
        sys.exit(f"게임 번들 폴더 없음: {GAME_ROOT}")

    if prefix:
        prefix = prefix.replace("\\", "/").lstrip("/")

    bundles = []
    for root, _, files in os.walk(GAME_ROOT):
        for fn in files:
            if not fn.endswith(".bundle"):
                continue
            path = os.path.join(root, fn)
            key = os.path.relpath(path, GAME_ROOT).replace("\\", "/")
            if prefix and not key.startswith(prefix):
                continue
            bundles.append((key, path))
    bundles.sort()

    if not bundles:
        sys.exit("인덱싱할 번들 없음. 경로prefix를 확인하세요.")

    current = {}
    if os.path.exists(TEXINDEX_PATH):
        with open(TEXINDEX_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        for name, keys in loaded.items():
            if isinstance(keys, list):
                current[name] = list(dict.fromkeys(k for k in keys if isinstance(k, str)))

    scanned = {}
    ok_keys = set()
    skipped = 0
    started = time.perf_counter()
    total = len(bundles)
    for idx, (key, path) in enumerate(bundles, 1):
        if idx == 1 or idx == total or idx % 25 == 0:
            print(f"[index {idx}/{total}] {key}")
        try:
            env = UnityPy.load(path)
            names = set()
            for obj in env.objects:
                if obj.type.name != "Texture2D":
                    continue
                data = obj.read()
                if data.m_Name:
                    names.add(data.m_Name)
            for name in names:
                scanned.setdefault(name, []).append(key)
            ok_keys.add(key)
        except Exception as e:
            skipped += 1
            print(f"[건너뜀] 인덱스 실패: {key}: {e}")

    for name in list(current):
        current[name] = [k for k in current[name] if k not in ok_keys]
        if not current[name]:
            del current[name]
    for name, keys in scanned.items():
        merged = current.setdefault(name, [])
        for key in keys:
            if key not in merged:
                merged.append(key)

    for keys in current.values():
        keys.sort()

    with open(TEXINDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(current.items())), f, ensure_ascii=False, indent=2)

    elapsed = time.perf_counter() - started
    print(f"\n완료: 텍스처명 {len(current)}개 → {TEXINDEX_PATH}")
    print(f"요약: 번들 {len(ok_keys)}개 갱신, 스킵 {skipped}개, {elapsed:.1f}초")


def find(query):
    """texindex.json에서 텍스처명을 부분 검색."""
    if not os.path.exists(TEXINDEX_PATH):
        sys.exit("tools/auto.py index 를 먼저 실행하세요")
    if not query:
        sys.exit("검색할 부분문자열을 입력하세요.")

    with open(TEXINDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)

    q = query.lower()
    matches = [(name, keys) for name, keys in data.items() if q in name.lower()]
    if not matches:
        print("검색 결과 없음")
        return

    for name, keys in sorted(matches):
        print(f"{name} →")
        for key in keys:
            print(f"  {key}")
    print(f"\n완료: {len(matches)}개 텍스처명")


def all_steps(flt=None, size=4096, lam=1.0):
    """derive → repack → hires → deploy 원클릭 실행."""
    print("========== derive ==========")
    try:
        derive(flt)
    except SystemExit as e:
        print(f"[계속] derive 건너뜀: {e}")

    print("========== repack ==========")
    repack(flt)

    print("========== hires ==========")
    hires(flt, size=size, lam=lam)

    print("========== deploy ==========")
    deploy()


def _copy_if_exists(src, dst_dir, label):
    """있으면 복사하고, 잠김/권한 오류는 배포 흐름을 막지 않음."""
    if not os.path.exists(src):
        return False
    os.makedirs(dst_dir, exist_ok=True)
    try:
        shutil.copy2(src, dst_dir)
        print(f"{label} 설치 완료")
        return True
    except OSError:
        print(f"  [건너뜀] {os.path.basename(src)} 잠김(실행 중). 종료 후 재배포.")
        return False


def deploy():
    # 1. C# DLL 빌드 (SPT 4.x 번들 모드는 DLL 필수)
    print("[빌드] dotnet build ...")
    subprocess.run(["dotnet", "build", CSPROJ, "-c", "Release"], check=True)

    # 2. 배포할 번들 = bundles/ 안의 모든 .bundle (실제로 만든 것만)
    keys = []
    for root, _, files in os.walk(OUT_DIR):
        for fn in files:
            if fn.endswith(".bundle"):
                rel = os.path.relpath(os.path.join(root, fn), OUT_DIR).replace("\\", "/")
                keys.append(rel)
    if not keys:
        sys.exit("bundles/ 에 .bundle 없음. 먼저 repack 실행.")

    # 3. 모드 폴더 구성. 번들(매번 바뀜) 먼저, DLL(거의 안 바뀜)은 best-effort.
    os.makedirs(MODS_DIR, exist_ok=True)
    manifest = {"manifest": [{"key": k, "dependencyKeys": []} for k in keys]}
    with open(os.path.join(MODS_DIR, "bundles.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=4)
    dst = os.path.join(MODS_DIR, "bundles")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(OUT_DIR, dst)
    print(f"번들 {len(keys)}개 설치 완료")

    # DLL/deps: 서버 실행 중이면 잠겨서 복사 실패 → 메타데이터 안 바뀌었으면 무시 가능
    for f in (f"{MOD_NAME.replace('-', '.')}.dll", f"{MOD_NAME.replace('-', '.')}.deps.json"):
        try:
            shutil.copy2(os.path.join(DLL_OUT, f), MODS_DIR)
        except OSError:  # WinError 1224(사용 중) 등은 PermissionError가 아님
            print(f"  [건너뜀] {f} 잠김(서버 실행 중). 메타데이터 바꿨으면 서버 종료 후 재배포.")

    if os.path.exists(HIRES_DIR):
        dst = os.path.join(HIRES_PLUGIN_DIR, "hires")
        try:
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(HIRES_DIR, dst)
            print("하이레즈 텍스처 설치 완료")
        except OSError:
            print("  [건너뜀] hires 잠김(게임 실행 중). 게임 종료 후 재배포.")

    if os.path.exists(HIRES_DLL):
        os.makedirs(HIRES_PLUGIN_DIR, exist_ok=True)
        try:
            shutil.copy2(HIRES_DLL, HIRES_PLUGIN_DIR)
            print("하이레즈 inspect 플러그인 설치 완료")
        except OSError:
            print("  [건너뜀] GoLani.HiResInspect.dll 잠김(게임 실행 중)")

    picker_index_copied = False
    if os.path.exists(TEXINDEX_PATH):
        _copy_if_exists(TEXINDEX_PATH, HIRES_PLUGIN_DIR, "하이레즈 inspect 인덱스")
        picker_index_copied = _copy_if_exists(TEXINDEX_PATH, PICKER_PLUGIN_DIR, "에셋 피커 인덱스")

    if os.path.exists(PICKER_DLL):
        _copy_if_exists(PICKER_DLL, PICKER_PLUGIN_DIR, "에셋 피커 플러그인")
        if os.path.exists(TEXINDEX_PATH) and not picker_index_copied:
            _copy_if_exists(TEXINDEX_PATH, PICKER_PLUGIN_DIR, "에셋 피커 인덱스")

    print(f"\n완료 → {MODS_DIR}")
    print("다음: SPT 런처에서 '임시 파일 삭제' 후 게임 실행")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    flt = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "extract":
        extract(flt)
    elif cmd == "derive":
        derive(flt)
    elif cmd == "repack":
        repack(flt)
    elif cmd == "hires":
        hires(flt, size=int(sys.argv[3]) if len(sys.argv) > 3 else 4096, lam=float(sys.argv[4]) if len(sys.argv) > 4 else 1.0)
    elif cmd == "build":  # derive + repack (한 방)
        derive(flt)
        repack(flt)
    elif cmd == "all":
        all_steps(flt, size=int(sys.argv[3]) if len(sys.argv) > 3 else 4096, lam=float(sys.argv[4]) if len(sys.argv) > 4 else 1.0)
    elif cmd == "index":
        index(flt)
    elif cmd == "find":
        find(flt)
    elif cmd == "deploy":
        deploy()
    else:
        sys.exit("사용법: python tools/auto.py [extract|derive|repack|hires|build|all|index|find|deploy] [필터|경로prefix|검색어] [size] [lambda]")
