"""Translation of gsensor/utils/mark_point.m."""


def mark_point(info_str, ia_obj, color_marker):
    info_struct = []
    while not isfield(info_struct, "Position"):
        ia_obj.an.String = info_str
        disp(ia_obj.an.String)
        pause()
        info_struct = getCursorInfo(ia_obj.dcm_obj)
    p = info_struct.Position
    p = [*p, 0]
    scatter(ia_obj.ax, p[0], p[1], color_marker)
    return p
