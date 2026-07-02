"""Diffuse-guided N/G Style Transplant (ComfyUI 없이 OpenCV/NumPy).

핵심 원칙(GPT Pro 검토안):
- 원본 _N/_G의 값체계·강도·극성은 유지. 디자인 "위치"만 한글 _D 기준으로 이식.
- 원본에 실제 요철/광택차가 약하면(평면 인쇄) 새로 추가하지 않고 영어 흔적만 제거.
- 모든 반경/시그마는 텍스처 크기에 비례(u = min(W,H)/512).
"""
import cv2
import numpy as np
from PIL import Image

K_THR = 0.012      # 노멀: 이 이상 요철이 원본에 있었으면 한글도 음각 추가
DELTA_THR = 3 / 255.0  # 광택: 디자인-주변 차이가 이 이상이면 한글에도 적용


def _u(w, h):
    return min(w, h) / 512.0


def _rgb(path, size):
    return np.array(Image.open(path).convert("RGB").resize(size, Image.LANCZOS))


def _design_mask(rgb_u8, u):
    """디퓨즈에서 디자인(글자/로고/그래픽) 영역 → 0~1 소프트 마스크."""
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    L, A, B = lab[..., 0], lab[..., 1], lab[..., 2]
    hp_s = np.abs(L - cv2.GaussianBlur(L, (0, 0), max(1.0, 1.2 * u)))
    hp_m = np.abs(L - cv2.GaussianBlur(L, (0, 0), max(1.0, 3.0 * u)))
    chroma = (np.abs(A - cv2.GaussianBlur(A, (0, 0), max(1.0, 2.0 * u)))
              + np.abs(B - cv2.GaussianBlur(B, (0, 0), max(1.0, 2.0 * u))))
    score = np.maximum.reduce([hp_s, hp_m, 0.5 * chroma])
    med = np.median(score)
    mad = np.median(np.abs(score - med)) + 1e-6
    mask = (score > med + 3.0 * mad).astype(np.uint8)

    k = max(1, int(round(1.5 * u)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))

    # 너무 작은 노이즈 / 너무 큰 배경 컴포넌트 제거
    n, cc, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    total = mask.size
    out = np.zeros_like(mask)
    for i in range(1, n):
        r = stats[i, cv2.CC_STAT_AREA] / total
        if 3e-6 <= r <= 0.30:
            out[cc == i] = 1

    out = cv2.dilate(out, np.ones((max(1, int(round(1.5 * u))),) * 2, np.uint8))
    return np.clip(cv2.GaussianBlur(out.astype(np.float32), (0, 0), max(1.0, 1.5 * u)), 0, 1)


def _decode_dxt5nm(rgba):
    x = rgba[..., 3].astype(np.float32) / 255 * 2 - 1   # X = A
    y = rgba[..., 1].astype(np.float32) / 255 * 2 - 1   # Y = G
    return x, y


def transplant_normal(orig_n, orig_d, kor_d, debug=None):
    on = np.array(Image.open(orig_n).convert("RGBA"))
    H, W = on.shape[:2]
    u = _u(W, H)
    x, y = _decode_dxt5nm(on)

    s = max(1.0, 3.0 * u)
    xb, yb = cv2.GaussianBlur(x, (0, 0), s), cv2.GaussianBlur(y, (0, 0), s)
    xd, yd = x - xb, y - yb                       # 고주파 = 글자/스크래치 후보
    relief = np.sqrt(xd * xd + yd * yd)

    old_mask = _design_mask(_rgb(orig_d, (W, H)), u)
    new_mask = _design_mask(_rgb(kor_d, (W, H)), u)

    protect = (relief > np.percentile(relief, 90)).astype(np.float32)  # 구조(뚜껑/홈) 보호
    remove = old_mask * (1 - protect)

    sel = (old_mask > 0.5) & (protect < 0.5)
    k = float(np.median(relief[sel])) if sel.sum() > 50 else 0.0

    # 1) 영어 흔적 제거 (remove 영역의 고주파만 제거, 구조 저주파는 유지)
    x = xb + xd * (1 - remove)
    y = yb + yd * (1 - remove)

    # 2) 원본에 디자인 요철이 충분했을 때만 한글 음각 추가 (SDF bevel)
    added = False
    if k > K_THR:
        m = (new_mask > 0.5).astype(np.uint8)
        sdf = cv2.distanceTransform(m, cv2.DIST_L2, 5) - cv2.distanceTransform(1 - m, cv2.DIST_L2, 5)
        gx = cv2.Sobel(sdf, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(sdf, cv2.CV_32F, 0, 1, ksize=3)
        nrm = np.sqrt(gx * gx + gy * gy) + 1e-6
        x = x + k * (gx / nrm) * new_mask
        y = y + k * (gy / nrm) * new_mask
        added = True

    # 벡터 길이 클램프 후 DXT5nm 재패킹 (R=255, G=Y, A=X, B=원본 유지)
    r = np.sqrt(x * x + y * y)
    sc = np.minimum(0.98 / np.maximum(r, 1e-6), 1.0)
    x, y = x * sc, y * sc
    out = on.copy()
    out[..., 0] = 255
    out[..., 1] = np.clip((y * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip((x * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)

    if debug:
        Image.fromarray((old_mask * 255).astype(np.uint8)).save(debug + "_N_oldmask.png")
        Image.fromarray((new_mask * 255).astype(np.uint8)).save(debug + "_N_newmask.png")
    return Image.fromarray(out, "RGBA"), {"k": round(k, 4), "added": added}


def transplant_gloss(orig_g, orig_d, kor_d, debug=None):
    og = np.array(Image.open(orig_g).convert("RGBA"))
    H, W = og.shape[:2]
    u = _u(W, H)
    g = og[..., 0].astype(np.float32) / 255.0

    old_mask = _design_mask(_rgb(orig_d, (W, H)), u)
    new_mask = _design_mask(_rgb(kor_d, (W, H)), u)

    om = (old_mask > 0.5).astype(np.uint8)
    e2 = max(1, int(round(2 * u)))
    e5 = max(2, int(round(5 * u)))
    core = cv2.erode(om, np.ones((e2, e2), np.uint8))
    ring = cv2.dilate(om, np.ones((e5, e5), np.uint8)) - cv2.dilate(om, np.ones((e2, e2), np.uint8))
    g_des = np.median(g[core > 0]) if (core > 0).sum() > 50 else 0.0
    g_bg = np.median(g[ring > 0]) if (ring > 0).sum() > 50 else 0.0
    delta = float(g_des - g_bg)

    # 영어 흔적 제거 (Telea 인페인트)
    inpaint = (old_mask > 0.3).astype(np.uint8) * 255
    g_clean = cv2.inpaint((g * 255).astype(np.uint8), inpaint, max(1, int(round(3 * u))),
                          cv2.INPAINT_TELEA).astype(np.float32) / 255.0

    added = False
    if abs(delta) > DELTA_THR:
        soft = cv2.GaussianBlur(new_mask, (0, 0), max(1.0, 1.2 * u))
        g_clean = np.clip(g_clean + delta * soft, 0, 1)
        added = True

    g8 = (g_clean * 255).astype(np.uint8)
    out = np.dstack([g8, g8, g8, np.full_like(g8, 255)])
    if debug:
        Image.fromarray((old_mask * 255).astype(np.uint8)).save(debug + "_G_oldmask.png")
    return Image.fromarray(out, "RGBA"), {"delta": round(delta, 4), "added": added}
