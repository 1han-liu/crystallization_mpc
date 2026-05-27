"""Translation of gsensor/morphs/get_angles_d.m."""

ANGLES_D_ALL = {
    "A": (61.60038904, 82.59533203, 112.0661066),
    "B": (38.2970794, 69.71231051, 84.1233756),
    "C": (53.22106313, 59.96173898, 70.20060129),
}


def get_angles_d(corner):
    angles_d = ANGLES_D_ALL[corner]
    UMW = angles_d[0]
    VMW = angles_d[1]
    UMV = angles_d[2]
    return UMW, VMW, UMV


__all__ = ["get_angles_d"]
