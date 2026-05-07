"""Translation of gsensor/detection/initialize_DSCGR.m."""

from ..morphs.choose_corner import choose_corner
from ..morphs.choose_is_full import choose_is_full
from ..morphs.get_points import get_points


def initialize_DSCGR(image_file):
    path = fullfile(image_file.folder, image_file.name)
    warning("off", "all")
    I = imread(path)
    warning("on", "all")

    is_full = choose_is_full(I)
    (
        fig_2d,
        ax_2d,
        m,
        w,
        u,
        v,
        u_op_struct,
        v_op_struct,
        u_ad_struct,
        v_ad_struct,
        kernel,
    ) = get_points(I, is_full)
    corner = choose_corner(I)
    fig_3d, ax_3d, M, W, U, V = recover_3d_all(I, m, w, u, v, corner, is_full)
    uv_struct_list = []
    uv_struct_1, uv_struct_2 = calc_2d_3d_info(
        M,
        W,
        U,
        V,
        u_op_struct,
        v_op_struct,
        u_ad_struct,
        v_ad_struct,
        is_full,
    )
    uv_struct_list.append(uv_struct_1)
    uv_struct_list.append(uv_struct_2)
    if not exist(fullfile("..", "gsensor_data"), "dir"):
        mkdir(fullfile("..", "gsensor_data"))
    save(fullfile("..", "gsensor_data", "DSCGR_info.mat"))
    return uv_struct_list, kernel
