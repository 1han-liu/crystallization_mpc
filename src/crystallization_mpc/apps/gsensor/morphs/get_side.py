"""Translation of gsensor/morphs/get_side.m."""

from types import SimpleNamespace

from .calc_normal import calc_normal
from .reorient_points import reorient_points
from ..utils.annotate_point import annotate_point
from ..utils.make_arrow import make_arrow
from ..utils.mark_point import mark_point


def get_side(ia_obj, foot, angle, suffix, p_text_func_general):
    p_text_add = angle + " angle at " + suffix + " side"
    p_text_func = lambda order, p_str: p_text_func_general(order, p_str, p_text_add)

    if foot != "w":
        side_str_with_suffix = foot + "\\_" + suffix[0:2]
        t_str = "t\\_" + side_str_with_suffix
        e_str = "e\\_" + side_str_with_suffix
        n_str = "n\\_" + side_str_with_suffix
        o_str = "o\\_" + side_str_with_suffix

        t = mark_point(p_text_func("first", t_str), ia_obj, "b*")
        e = mark_point(p_text_func("second", e_str), ia_obj, "b*")
        t, e = reorient_points(t, e)
        annotate_point(ia_obj.ax, t, t_str)
        annotate_point(ia_obj.ax, e, e_str)
        n, v, vc = calc_normal(t, e)
        annotate_point(ia_obj.ax, (v + vc) / 2, n_str)
        make_arrow(ia_obj.ax, t, e)
        make_arrow(ia_obj.ax, vc, v)
        o = mark_point(p_text_func("outer", o_str), ia_obj, "go")
        annotate_point(ia_obj.ax, o, o_str)

        side_struct = SimpleNamespace(
            t=t,
            e=e,
            n=n,
            v=v,
            vc=vc,
            o=o,
            foot=foot,
            suffix=suffix,
        )
    else:
        w_str = "w"
        w = mark_point(p_text_func("middle", w_str), ia_obj, "rx")
        annotate_point(ia_obj.ax, w, w_str)
        side_struct = SimpleNamespace(w=w)

    return side_struct
