"""Translation of gsensor/morphs/get_points.m."""

from .calc_foot_point import calc_foot_point
from .calc_intersect import calc_intersect
from .get_side import get_side
from ..utils.annotate_point import annotate_point
from ..utils.make_line import make_line
from ..utils.mark_point import mark_point


def get_points(I, is_full):
    fig_2d, ia_obj = get_interactive_figure(I)
    ax_2d = ia_obj.ax
    if is_full:
        title("Full mode: corner at center", Parent=ax_2d)
    else:
        title("Non-full mode: corner at side", Parent=ax_2d)

    p_text_func_general = lambda order, p_str, p_text_add: (
        "Mark "
        + order
        + " point ("
        + p_str
        + ") of "
        + p_text_add
        + " in 2D and hit ENTER"
    )

    u_ad_struct = get_side(ia_obj, "u", "small", "adjacent", p_text_func_general)
    v_ad_struct = get_side(ia_obj, "v", "large", "adjacent", p_text_func_general)
    m = calc_intersect(u_ad_struct.t, u_ad_struct.e, v_ad_struct.t, v_ad_struct.e)
    annotate_point(ax_2d, m, "m")

    if not is_full:
        u = calc_foot_point(m, u_ad_struct)
        annotate_point(ax_2d, u, "u")
        make_line(ax_2d, m, u, "-")

        v = calc_foot_point(m, v_ad_struct)
        annotate_point(ax_2d, v, "v")
        make_line(ax_2d, m, v, "-")

        w_ad_struct = get_side(ia_obj, "w", [], [], p_text_func_general)
        w = w_ad_struct.w
        make_line(ax_2d, m, w, "-")

        u_op_struct = []
        v_op_struct = []
    else:
        u_op_struct = get_side(ia_obj, "u", "small", "opposite", p_text_func_general)
        u = calc_intersect(u_op_struct.t, u_op_struct.e, u_ad_struct.t, u_ad_struct.e)
        annotate_point(ax_2d, u, "u")
        make_line(ax_2d, m, u, "-")

        v_op_struct = get_side(ia_obj, "v", "large", "opposite", p_text_func_general)
        v = calc_intersect(v_op_struct.t, v_op_struct.e, v_ad_struct.t, v_ad_struct.e)
        annotate_point(ax_2d, v, "v")
        make_line(ax_2d, m, v, "-")

        w = calc_intersect(u_op_struct.t, u_op_struct.e, v_op_struct.t, v_op_struct.e)
        annotate_point(ax_2d, w, "w")
        make_line(ax_2d, m, w, "-")
        make_line(ax_2d, u, w, "-")
        make_line(ax_2d, v, w, "-")

    kernel = type("kernel", (), {})()
    kernel.k_c_cell = []
    kernel.k_o_cell = []
    kernel_orders = ["1.", "2.", "3.", "4."]
    for ii in range(4):
        kernel.k_c_cell.append(
            mark_point(
                p_text_func_general(kernel_orders[ii], "k_c", "kernel corners"),
                ia_obj,
                "y*",
            )
        )
        annotate_point(ax_2d, kernel.k_c_cell[ii], "k_c_" + str(ii + 1))
        if len(kernel.k_c_cell) >= 2:
            make_line(ax_2d, kernel.k_c_cell[-2], kernel.k_c_cell[-1], "-")
        kernel.k_o_cell.append(
            mark_point(
                p_text_func_general(kernel_orders[ii], "k_o", "kernel outer points"),
                ia_obj,
                "wo",
            )
        )
        annotate_point(ax_2d, kernel.k_o_cell[ii], "k_o_" + str(ii + 1))
    make_line(ax_2d, kernel.k_c_cell[-1], kernel.k_c_cell[0], "-")

    mark_point("Click anywhere and hit ENTER to close", ia_obj, ".")
    if not exist("gsensor_data", "dir"):
        mkdir("gsensor_data")

    hold(ax_2d, "off")
    if not exist(fullfile("..", "gsensor_data"), "dir"):
        mkdir(fullfile("..", "gsensor_data"))
    savefig(fig_2d, fullfile("..", "gsensor_data", "2d.fig"))
    close(fig_2d)
    return (
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
    )
