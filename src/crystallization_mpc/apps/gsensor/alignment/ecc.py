from __future__ import annotations
import cv2
import numpy as np

# --- 配置区域 ---
MASK_ALPHA = 0.4

# 颜色配置（BGR）
MASK_COLOR_FULL = (255, 0, 0)   # 蓝色：原始边界
MASK_COLOR_CORE = (0, 255, 0)   # 绿色：ECC 计算核心区

# Core mask 自适应生成参数
# 各核大小 = clamp(r_eq × frac, min_px, max_px)，其中 r_eq = sqrt(mask_area / π)
OPEN_RADIUS_FRAC  = 0.08;  OPEN_MIN_PX  =  5;  OPEN_MAX_PX  = 40
CLOSE_RADIUS_FRAC = 0.08;  CLOSE_MIN_PX =  5;  CLOSE_MAX_PX = 40
ERODE_RADIUS_FRAC = 0.25;  ERODE_MIN_PX = 20;  ERODE_MAX_PX = 80
# MIN_CORE_PIXELS = max(MIN_CORE_ABS, mask_area × MIN_CORE_FRAC)
MIN_CORE_FRAC = 0.05
MIN_CORE_ABS  = 500

# 背景注册参数
# 背景 mask = bitwise_not(dilate(crystal_mask, BG_DILATE_PX))
# bg_pixels >= max(BG_MIN_ABS, frame_area × BG_MIN_FRAC) 时优先用背景注册，否则降级 core mask
BG_DILATE_PX = 50    # 晶体 mask 膨胀半径（像素），排除生长边缘进入背景区
BG_MIN_FRAC  = 0.10  # 背景占帧面积的最小比例；低于此降级为 core mask 注册
BG_MIN_ABS   = 5000  # 背景区域最小绝对像素数

# ECC 参数
MOTION_TYPE     = cv2.MOTION_EUCLIDEAN  # 欧氏刚体：平移+旋转（不缩放/剪切）
MAX_ITER        = 200                 # 原单尺度最大迭代次数（保留作总迭代预算参考）
EPS             = 1e-6                # 收敛阈值
GAUSS_FILT_SIZE = 5                   # 高斯平滑核（抑制噪点对梯度的干扰）

# 多尺度金字塔 ECC 参数
# 先在低分辨率估计粗略 warp，再逐级热启动精化到原始分辨率，降低局部最小值风险。
ECC_PYRAMID_SCALES = (1 / 8, 1 / 4, 1 / 2, 1.0)
ECC_PYRAMID_MAX_ITER = 50
ECC_PYRAMID_MIN_SIZE = 16

# 变换幅度门控参数
# ECC 收敛后，若估计变换幅度低于两个阈值则视为热噪声，强制置为单位矩阵（零位移）
# 阈值依据：显微镜帧间真实抖动通常 2～15px；低于 0.3px 才是真正的热噪声
GATE_TRANS_PX  = 0.3   # 平移幅度阈值（像素）：sqrt(tx²+ty²) < 此值时不对齐
GATE_ROT_DEG   = 0.05  # 旋转幅度阈值（度）：|theta| < 此值时不对齐（需同时满足平移条件）

# 时序平滑参数
# 维护一个滚动缓冲区，计算历史中位数作为平滑估计
# 热启动改为中位数 warp，避免上帧原始误差直接传播到下一帧
WARP_BUFFER_K   = 7    # 滚动缓冲区长度（帧数）；加长以覆盖更多历史，中位数更稳
WARP_OUTLIER_PX = 8.0  # 当前估计偏离缓冲区中位数超过此值（像素）视为异常，改用中位数替代
                        # 依据：正常帧间抖动 < 8px；质心跳变（不对称生长）10～15px 会被拦截；
                        # 跑飞时几百px，阈值足以区分。15px 过松会放入质心跳变引起的错误位移。

# 定期重置参数
# 每 RESET_INTERVAL 帧强制重置参考帧 + 清空 warp_buffer，将误差累积上界从全程缩短到 N 帧
# 重置时当前帧的 warp 被记录为漂移日志，aligned_mask 本身不受影响（已完成 warpAffine）
RESET_INTERVAL  = 30   # 重置周期（帧数）；延长以减少重置导致的状态丢失；0 = 禁用

# 质心交叉验证参数
# ECC 估计平移与质心估计平移的最大允许偏差；超过此值视为 ECC 误收敛，回退为质心平移
# 依据：质心本身会因晶体不对称生长而漂移，与 ECC 的背景注册结果天然存在差异；
# 只有 ECC 真正跑飞（几百px）时才应该拒绝，正常差异在 30px 以内完全合理
CROSSVAL_DISAGREE_PX = 50.0

# CLAHE 参数（适中，比光流弱——ECC 需要平滑梯度，过强会产生伪梯度）
CLAHE_CLIP = 3.0
CLAHE_GRID = (8, 8)

def get_stable_mask(mask_raw: np.ndarray | None) -> np.ndarray | None:
    """Opening → Closing → Erosion，核大小依据当前帧 mask 面积自适应计算。"""
    if mask_raw is None:
        return None
    mask_area = int(cv2.countNonZero(mask_raw))
    if mask_area == 0:
        return np.zeros_like(mask_raw)
    r_eq      = np.sqrt(mask_area / np.pi)
    open_px   = int(np.clip(r_eq * OPEN_RADIUS_FRAC,  OPEN_MIN_PX,  OPEN_MAX_PX))
    close_px  = int(np.clip(r_eq * CLOSE_RADIUS_FRAC, CLOSE_MIN_PX, CLOSE_MAX_PX))
    erode_px  = int(np.clip(r_eq * ERODE_RADIUS_FRAC, ERODE_MIN_PX, ERODE_MAX_PX))
    k_open  = cv2.getStructuringElement(cv2.MORPH_RECT, (open_px,  open_px))
    k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (close_px, close_px))
    k_erode = cv2.getStructuringElement(cv2.MORPH_RECT, (erode_px, erode_px))
    opened  = cv2.morphologyEx(mask_raw, cv2.MORPH_OPEN,  k_open)
    closed  = cv2.morphologyEx(opened,   cv2.MORPH_CLOSE, k_close)
    eroded  = cv2.morphologyEx(closed,   cv2.MORPH_ERODE, k_erode)
    return eroded


def centroid_translation(
    prev_mask: np.ndarray, curr_mask: np.ndarray
) -> tuple[float, float]:
    """
    用两帧 mask 质心差估计相机平移量。
    返回 (dx, dy)：将 curr_mask 质心对齐到 prev_mask 质心所需的平移量，
    与 ECC warp_matrix[0,2] / [1,2] 的符号约定一致。
    若任一 mask 为空，返回 (0.0, 0.0)。
    """
    M_p = cv2.moments(prev_mask)
    M_c = cv2.moments(curr_mask)
    if M_p['m00'] < 1e-6 or M_c['m00'] < 1e-6:
        return 0.0, 0.0
    cx_p, cy_p = M_p['m10'] / M_p['m00'], M_p['m01'] / M_p['m00']
    cx_c, cy_c = M_c['m10'] / M_c['m00'], M_c['m01'] / M_c['m00']
    return float(cx_p - cx_c), float(cy_p - cy_c)


def get_background_mask(curr_mask: np.ndarray | None, frame_shape: tuple) -> np.ndarray:
    """
    背景 mask = bitwise_not(dilate(crystal_mask, BG_DILATE_PX))。
    膨胀半径排除晶体生长边缘，保留画面中纯静态的背景区域供 ECC 使用。
    若 crystal_mask 为空，则整帧均视为背景。
    """
    H, W = frame_shape[:2]
    if curr_mask is None or cv2.countNonZero(curr_mask) == 0:
        return np.ones((H, W), dtype=np.uint8) * 255
    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * BG_DILATE_PX + 1, 2 * BG_DILATE_PX + 1)
    )
    dilated = cv2.dilate(curr_mask, k)
    return cv2.bitwise_not(dilated)


def enhance_for_ecc(img_gray: np.ndarray) -> np.ndarray:
    """CLAHE 增强灰度图，提升梯度信号供 ECC 使用。"""
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=CLAHE_GRID)
    return clahe.apply(img_gray)


def scale_warp_for_resolution(warp_matrix: np.ndarray, scale: float) -> np.ndarray:
    """将全分辨率 warp 转换到指定金字塔尺度；旋转不变，平移按尺度缩放。"""
    scaled = warp_matrix.astype(np.float32).copy()
    scaled[0, 2] *= scale
    scaled[1, 2] *= scale
    return scaled


def unscale_warp_from_resolution(warp_matrix: np.ndarray, scale: float) -> np.ndarray:
    """将某金字塔尺度下的 warp 转回全分辨率坐标。"""
    full = warp_matrix.astype(np.float32).copy()
    full[0, 2] /= scale
    full[1, 2] /= scale
    return full


def find_transform_ecc_pyramid(
    prev_gray_enhanced: np.ndarray,
    curr_gray_enhanced: np.ndarray,
    initial_warp: np.ndarray,
    ecc_mask: np.ndarray,
) -> np.ndarray:
    """
    4 层多尺度金字塔 ECC。

    warp 始终在全分辨率坐标中向下一层传递；进入某层前缩放平移量，
    本层收敛后再转回全分辨率，作为下一层热启动。
    """
    full_h, full_w = prev_gray_enhanced.shape[:2]
    warp_full = initial_warp.astype(np.float32).copy()
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        ECC_PYRAMID_MAX_ITER,
        EPS,
    )
    ran_level = False
    last_error = None

    for scale in ECC_PYRAMID_SCALES:
        level_w = max(1, int(round(full_w * scale)))
        level_h = max(1, int(round(full_h * scale)))
        if level_w < ECC_PYRAMID_MIN_SIZE or level_h < ECC_PYRAMID_MIN_SIZE:
            continue

        prev_level = cv2.resize(
            prev_gray_enhanced, (level_w, level_h), interpolation=cv2.INTER_AREA
        )
        curr_level = cv2.resize(
            curr_gray_enhanced, (level_w, level_h), interpolation=cv2.INTER_AREA
        )
        mask_level = cv2.resize(
            ecc_mask, (level_w, level_h), interpolation=cv2.INTER_NEAREST
        )
        mask_level = (mask_level > 0).astype(np.uint8)
        if cv2.countNonZero(mask_level) < max(10, int(level_w * level_h * 0.005)):
            continue

        warp_level = scale_warp_for_resolution(warp_full, scale)
        try:
            _, warp_level = cv2.findTransformECC(
                prev_level.astype(np.float32),
                curr_level.astype(np.float32),
                warp_level,
                MOTION_TYPE,
                criteria,
                mask_level,
                GAUSS_FILT_SIZE,
            )
        except cv2.error as err:
            last_error = err
            if ran_level:
                print(f'  [ECC] 金字塔 {scale:g} 层精化失败，保留上一层结果')
            continue
        ran_level = True
        warp_full = unscale_warp_from_resolution(warp_level, scale)

    if not ran_level and last_error is not None:
        raise last_error
    return warp_full.astype(np.float32)


def align_one_frame_ecc(
    prev_gray_enhanced: np.ndarray,
    curr_gray: np.ndarray,
    curr_mask: np.ndarray,
    prev_warp: np.ndarray | None = None,
    warp_buffer: list | None = None,
    frame_idx: int = 1,
    prev_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list, dict[str, object]]:
    """
    用 ECC 欧氏刚体变换将 curr_gray 对齐到 prev_gray_enhanced。

    Parameters
    ----------
    prev_gray_enhanced : (H,W) uint8 — 上一帧 CLAHE 增强灰度图（参考帧）
    curr_gray          : (H,W) uint8 — 当前帧原始灰度图
    curr_mask          : (H,W) uint8 — 当前帧后处理 mask
    prev_warp          : (2,3) float32 — 上一帧欧氏矩阵（兜底用），None 则用单位矩阵
    warp_buffer        : list[ndarray] — 历史 warp 滚动缓冲区（方案 C），None 则初始化为空
    frame_idx          : int — 当前帧序号（从 1 起），用于方案 E 的定期重置判断
    prev_mask          : (H,W) uint8 | None — 上一帧后处理 mask，用于方案 F 质心交叉验证

    Returns
    -------
    aligned_mask       : (H,W) uint8  — 欧氏刚体对齐后的 mask
    curr_gray_enhanced : (H,W) uint8  — 当前帧增强图（下一帧作为 prev 传入）
    warp_matrix        : (2,3) float32 — 本帧验证后的欧氏矩阵（重置帧返回单位矩阵）
    warp_buffer        : list[ndarray] — 更新后的滚动缓冲区（重置帧返回空列表）
    """
    if warp_buffer is None:
        warp_buffer = []
    diagnostics: dict[str, object] = {"success": True, "fallback_reason": None}
    H, W = curr_gray.shape[:2]

    # Step 1：CLAHE 增强当前帧
    curr_gray_enhanced = enhance_for_ecc(curr_gray)

    # Step 2：生成 core mask（降级备用）和背景 mask（方案 D 主用）
    core        = get_stable_mask(curr_mask)
    core_pixels = cv2.countNonZero(core) if core is not None else 0
    mask_area   = int(cv2.countNonZero(curr_mask))
    min_core    = max(MIN_CORE_ABS, int(mask_area * MIN_CORE_FRAC))

    bg_raw    = get_background_mask(curr_mask, curr_gray.shape)
    bg_pixels = int(cv2.countNonZero(bg_raw))
    bg_min    = max(BG_MIN_ABS, int(H * W * BG_MIN_FRAC))

    # 优先 core mask 注册；core 不足时降级为背景注册；两者均不足则退化为零位移
    if core_pixels >= min_core:
        ecc_mask  = (core > 127).astype(np.uint8)
        reg_label = f'core 注册 ({core_pixels}px)'
    elif bg_pixels >= bg_min:
        ecc_mask  = (bg_raw > 127).astype(np.uint8)
        reg_label = f'背景降级注册 ({bg_pixels}px, core={core_pixels}px 不足)'
    else:
        ecc_mask  = None
        reg_label = None

    # Step 3：热启动 — 优先用缓冲区中位数，避免上帧原始误差直接传播
    if len(warp_buffer) >= 2:
        _txs = [w[0, 2] for w in warp_buffer]
        _tys = [w[1, 2] for w in warp_buffer]
        _ths = [np.arctan2(w[1, 0], w[0, 0]) for w in warp_buffer]
        _c, _s = np.cos(float(np.median(_ths))), np.sin(float(np.median(_ths)))
        warp_matrix = np.array(
            [[_c, -_s, float(np.median(_txs))],
             [_s,  _c, float(np.median(_tys))]], dtype=np.float32
        )
    else:
        warp_matrix = prev_warp.copy() if prev_warp is not None else np.eye(2, 3, dtype=np.float32)

    # Step 4：注册区域有效性检查
    if ecc_mask is None:
        print(f'  [ECC] 背景({bg_pixels}px)和 core({core_pixels}px)均不足，退化为零位移')
        aligned_mask = cv2.warpAffine(
            curr_mask, warp_matrix, (W, H),
            flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
        diagnostics.update(
            success=False,
            fallback_reason="ECC registration mask is too small",
        )
        return aligned_mask, curr_gray_enhanced, warp_matrix, warp_buffer, diagnostics

    print(f'  [ECC] {reg_label}')

    # Step 5：多尺度金字塔 ECC 迭代求解
    try:
        warp_matrix = find_transform_ecc_pyramid(
            prev_gray_enhanced,
            curr_gray_enhanced,
            warp_matrix,
            ecc_mask,
        )
    except cv2.error as exc:
        # 收敛失败（纹理过弱或帧间变化过大）→ 退化为单位矩阵
        print('  [ECC] 金字塔收敛失败，退化为零位移')
        warp_matrix = np.eye(2, 3, dtype=np.float32)
        diagnostics.update(
            success=False,
            fallback_reason=f"ECC pyramid did not converge: {exc}",
        )

    # Step 6：质心交叉验证（方案 F）— ECC 误收敛时回退到质心平移
    # 比较 ECC 平移与质心差估计；分歧超过阈值说明 ECC 可能吸收了生长信号
    if prev_mask is not None:
        dx_c, dy_c = centroid_translation(prev_mask, curr_mask)
        dx_e = float(warp_matrix[0, 2])
        dy_e = float(warp_matrix[1, 2])
        disagree = float(np.hypot(dx_e - dx_c, dy_e - dy_c))
        if disagree > CROSSVAL_DISAGREE_PX:
            print(f'  [ECC] 质心分歧 {disagree:.2f}px > {CROSSVAL_DISAGREE_PX}px，'
                  f'ECC({dx_e:.2f},{dy_e:.2f}) → 质心({dx_c:.2f},{dy_c:.2f})')
            warp_matrix = np.array(
                [[1.0, 0.0, dx_c], [0.0, 1.0, dy_c]], dtype=np.float32
            )

    # Step 7：变换幅度门控 — 双条件同时满足才视为有效漂移
    tx    = float(warp_matrix[0, 2])
    ty    = float(warp_matrix[1, 2])
    theta = float(np.degrees(np.arctan2(warp_matrix[1, 0], warp_matrix[0, 0])))
    if np.sqrt(tx**2 + ty**2) < GATE_TRANS_PX and abs(theta) < GATE_ROT_DEG:
        print(f'  [ECC] 幅度过小 (|t|={np.hypot(tx,ty):.3f}px, θ={theta:.4f}°)，置为零位移')
        warp_matrix = np.eye(2, 3, dtype=np.float32)

    # Step 8：时序异常值剔除 + 缓冲区更新
    if len(warp_buffer) >= 2:
        _tx_med = float(np.median([w[0, 2] for w in warp_buffer]))
        _ty_med = float(np.median([w[1, 2] for w in warp_buffer]))
        _th_med = float(np.median([np.arctan2(w[1, 0], w[0, 0]) for w in warp_buffer]))
        dev = float(np.hypot(warp_matrix[0, 2] - _tx_med, warp_matrix[1, 2] - _ty_med))
        if dev > WARP_OUTLIER_PX:
            _c, _s = np.cos(_th_med), np.sin(_th_med)
            print(f'  [ECC] 异常值: 偏离中位数 {dev:.2f}px > {WARP_OUTLIER_PX}px，用中位数替代')
            warp_matrix = np.array([[_c, -_s, _tx_med], [_s, _c, _ty_med]], dtype=np.float32)
    # 用验证后的矩阵更新缓冲区（防止误差传播到下一帧热启动）
    warp_buffer.append(warp_matrix.copy())
    if len(warp_buffer) > WARP_BUFFER_K:
        warp_buffer.pop(0)

    # Step 9：用欧氏矩阵对齐 curr_mask
    aligned_mask = cv2.warpAffine(
        curr_mask, warp_matrix, (W, H),
        flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    applied_warp = warp_matrix.copy()

    # Step 10：定期重置参考帧 — 防止误差无限累积
    # aligned_mask 已完成 warpAffine，不受重置影响；重置的是传给下一帧的状态
    if RESET_INTERVAL > 0 and frame_idx % RESET_INTERVAL == 0:
        drift_tx = float(warp_matrix[0, 2])
        drift_ty = float(warp_matrix[1, 2])
        drift_th = float(np.degrees(np.arctan2(warp_matrix[1, 0], warp_matrix[0, 0])))
        print(f'  [ECC] 第 {frame_idx} 帧触发参考帧重置 '
              f'(漂移记录: tx={drift_tx:.2f}px ty={drift_ty:.2f}px θ={drift_th:.3f}°)')
        warp_matrix = np.eye(2, 3, dtype=np.float32)
        warp_buffer = []

    diagnostics["next_warp"] = warp_matrix.copy()
    return aligned_mask, curr_gray_enhanced, applied_warp, warp_buffer, diagnostics


__all__ = [
    "align_one_frame_ecc",
    "enhance_for_ecc",
    "find_transform_ecc_pyramid",
    "get_stable_mask",
]
