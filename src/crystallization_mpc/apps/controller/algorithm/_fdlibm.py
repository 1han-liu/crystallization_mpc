"""Binary64 real power with the reference scalar libm evaluation order.

Python translation of Sun fdlibm e_pow.c, available from
https://netlib.org/fdlibm/e_pow.c . No MATLAB or Java runtime is used.

Copyright (C) 1993, 2004 by Sun Microsystems, Inc. All rights reserved.
Permission to use, copy, modify, and distribute this software is freely
granted, provided that this notice is preserved.
"""
from __future__ import annotations

import math
import struct


def _words(x: float) -> tuple[int, int]:
    bits = struct.unpack(">Q", struct.pack(">d", x))[0]
    return bits >> 32, bits & 0xffffffff


def _high(x: float, high: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", ((high & 0xffffffff) << 32) | _words(x)[1]))[0]


def _truncate(x: float) -> float:
    return struct.unpack(">d", struct.pack(">Q", _words(x)[0] << 32))[0]


def real_atan(x: float) -> float:
    """Sun fdlibm s_atan.c; the permissive notice above also applies."""
    x = float(x)
    high, _ = _words(x)
    magnitude = high & 0x7fffffff
    negative = bool(high & 0x80000000)
    upper = (4.63647609000806093515e-1, 7.85398163397448278999e-1,
             9.82793723247329054082e-1, 1.57079632679489655800)
    lower = (2.26987774529616870924e-17, 3.06161699786838301793e-17,
             1.39033110312309984516e-17, 6.12323399573676603587e-17)
    coefficients = (3.33333333333329318027e-1, -1.99999999998764832476e-1,
        1.42857142725034663711e-1, -1.11111104054623557880e-1,
        9.09088713343650656196e-2, -7.69187620504482999495e-2,
        6.66107313738753120669e-2, -5.83357013379057348645e-2,
        4.97687799461593236017e-2, -3.65315727442169155270e-2,
        1.62858201153657823623e-2)
    if magnitude >= 0x44100000:
        if math.isnan(x):
            return x + x
        return -upper[3] - lower[3] if negative else upper[3] + lower[3]
    if magnitude < 0x3fdc0000:
        if magnitude < 0x3e200000:
            return x
        region = -1
    else:
        x = abs(x)
        if magnitude < 0x3ff30000:
            if magnitude < 0x3fe60000:
                region, x = 0, (2.0 * x - 1.0) / (2.0 + x)
            else:
                region, x = 1, (x - 1.0) / (x + 1.0)
        elif magnitude < 0x40038000:
            region, x = 2, (x - 1.5) / (1.0 + 1.5 * x)
        else:
            region, x = 3, -1.0 / x
    z = x * x
    w = z * z
    a = coefficients
    odd = z * (a[0] + w * (a[2] + w * (a[4] + w * (a[6] + w * (a[8] + w * a[10])))))
    even = w * (a[1] + w * (a[3] + w * (a[5] + w * (a[7] + w * a[9]))))
    if region < 0:
        return x - x * (odd + even)
    result = upper[region] - ((x * (odd + even) - lower[region]) - x)
    return -result if negative else result


def real_atan2(y: float, x: float) -> float:
    """Sun fdlibm e_atan2.c, retaining quadrant and signed-zero order."""
    x, y = float(x), float(y)
    if math.isnan(x) or math.isnan(y):
        return x + y
    if x == 1.0:
        return real_atan(y)
    hx, _ = _words(x)
    hy, _ = _words(y)
    quadrant = ((hy >> 31) & 1) | ((hx >> 30) & 2)
    if y == 0.0:
        if quadrant < 2:
            return y
        return math.pi if quadrant == 2 else -math.pi
    if x == 0.0:
        return -math.pi / 2 if hy >> 31 else math.pi / 2
    if math.isinf(x):
        if math.isinf(y):
            return (math.pi / 4, -math.pi / 4, 3.0 * (math.pi / 4), -3.0 * (math.pi / 4))[quadrant]
        return (0.0, -0.0, math.pi, -math.pi)[quadrant]
    if math.isinf(y):
        return -math.pi / 2 if hy >> 31 else math.pi / 2
    exponent_difference = ((hy & 0x7fffffff) - (hx & 0x7fffffff)) >> 20
    if exponent_difference > 60:
        z = math.pi / 2 + 0.5 * 1.2246467991473531772e-16
    elif hx >> 31 and exponent_difference < -60:
        z = 0.0
    else:
        z = real_atan(abs(y / x))
    if quadrant == 0:
        return z
    if quadrant == 1:
        return -z
    if quadrant == 2:
        return math.pi - (z - 1.2246467991473531772e-16)
    return (z - 1.2246467991473531772e-16) - math.pi


def real_acos(x: float) -> float:
    """Sun fdlibm e_acos.c, with the same permissive notice as e_pow.c."""
    x = float(x)
    ix = _words(x)[0] & 0x7fffffff
    if abs(x) >= 1.0:
        if x == 1.0:
            return 0.0
        if x == -1.0:
            return math.pi + 2.0 * 6.12323399573676603587e-17
        return math.nan
    if math.isnan(x):
        return math.nan
    if ix < 0x3fe00000 and ix <= 0x3c600000:
        return 1.570796326794896558 + 6.12323399573676603587e-17
    z = x * x if ix < 0x3fe00000 else (1.0 + x if x < 0 else 1.0 - x) * .5
    p = z * (1.66666666666666657415e-1 + z * (-3.25565818622400915405e-1
        + z * (2.01212532134862925881e-1 + z * (-4.00555345006794114027e-2
        + z * (7.91534994289814532176e-4 + z * 3.47933107596021167570e-5)))))
    q = 1.0 + z * (-2.40339491173441421878 + z * (2.02094576023350569471
        + z * (-6.88283971605453293030e-1 + z * 7.70381505559019352791e-2)))
    r = p / q
    if ix < 0x3fe00000:
        return 1.570796326794896558 - (x - (6.12323399573676603587e-17 - x * r))
    s = math.sqrt(z)
    if x < 0:
        w = r * s - 6.12323399573676603587e-17
        return math.pi - 2.0 * (s + w)
    df = _truncate(s)
    correction = (z - df * df) / (s + df)
    return 2.0 * (df + (r * s + correction))


def real_hypot(x: float, y: float) -> float:
    """Sun fdlibm e_hypot.c, with its power-of-two scaling order."""
    a, b = abs(float(x)), abs(float(y))
    ha, hb = _words(a)[0], _words(b)[0]
    if hb > ha:
        a, b, ha, hb = b, a, hb, ha
    if ha - hb > 0x3c00000:
        return a + b
    scale = 0
    if ha > 0x5f300000:
        if ha >= 0x7ff00000:
            if math.isinf(a):
                return a
            if math.isinf(b):
                return b
            return a + b
        ha -= 0x25800000
        hb -= 0x25800000
        a, b, scale = _high(a, ha), _high(b, hb), 600
    if hb < 0x20b00000:
        if hb <= 0x000fffff:
            if b == 0:
                return a
            factor = _high(0.0, 0x7fd00000)
            a, b, scale = a * factor, b * factor, scale - 1022
            # The source retains ha/hb until its explicit high-word reads.
        else:
            ha += 0x25800000
            hb += 0x25800000
            a, b, scale = _high(a, ha), _high(b, hb), scale - 600
    difference = a - b
    if difference > b:
        upper = _high(0.0, ha)
        lower = a - upper
        result = math.sqrt(upper * upper - (b * (-b) - lower * (a + upper)))
    else:
        a = a + a
        y_upper = _high(0.0, hb)
        y_lower = b - y_upper
        upper = _high(0.0, ha + 0x00100000)
        lower = a - upper
        result = math.sqrt(upper * y_upper - (difference * (-difference) - (upper * y_lower + lower * b)))
    return _high(1.0, 0x3ff00000 + (scale << 20)) * result if scale else result


def real_log1p(x: float) -> float:
    """Sun fdlibm s_log1p.c, including its low correction term."""
    x = float(x)
    high = _words(x)[0]
    signed_high = high if high < 0x80000000 else high - 0x100000000
    magnitude = high & 0x7fffffff
    k, correction, hu = 1, 0.0, 0
    if signed_high < 0x3fda827a:
        if magnitude >= 0x3ff00000:
            return -math.inf if x == -1.0 else math.nan
        if magnitude < 0x3e200000:
            return x if magnitude < 0x3c900000 else x - x * x * 0.5
        if signed_high > 0 or signed_high <= 0xbfd2bec3 - 0x100000000:
            k, f, hu = 0, x, 1
    if signed_high >= 0x7ff00000:
        return x + x
    if k:
        if signed_high < 0x43400000:
            u = 1.0 + x
            hu = _words(u)[0]
            k = (hu >> 20) - 1023
            correction = (1.0 - (u - x) if k > 0 else x - (u - 1.0)) / u
        else:
            u = x
            hu = _words(u)[0]
            k = (hu >> 20) - 1023
        hu &= 0x000fffff
        if hu < 0x6a09e:
            u = _high(u, hu | 0x3ff00000)
        else:
            k += 1
            u = _high(u, hu | 0x3fe00000)
            hu = (0x00100000 - hu) >> 2
        f = u - 1.0
    upper_ln2, lower_ln2 = 6.93147180369123816490e-1, 1.90821492927058770002e-10
    half_square = 0.5 * f * f
    if hu == 0:
        if f == 0.0:
            return 0.0 if k == 0 else k * upper_ln2 + (correction + k * lower_ln2)
        remainder = half_square * (1.0 - 0.66666666666666666 * f)
        return f - remainder if k == 0 else k * upper_ln2 - ((remainder - (k * lower_ln2 + correction)) - f)
    s = f / (2.0 + f)
    z = s * s
    remainder = z * (6.666666666666735130e-1 + z * (3.999999999940941908e-1
        + z * (2.857142874366239149e-1 + z * (2.222219843214978396e-1
        + z * (1.818357216161805012e-1 + z * (1.531383769920937332e-1
        + z * 1.479819860511658591e-1))))))
    if k == 0:
        return f - (half_square - s * (half_square + remainder))
    return k * upper_ln2 - ((half_square - (s * (half_square + remainder)
                                             + (k * lower_ln2 + correction))) - f)


def real_asinh(x: float) -> float:
    """Sun fdlibm s_asinh.c, with the source log1p evaluation."""
    x = float(x)
    magnitude = _words(x)[0] & 0x7fffffff
    if magnitude >= 0x7ff00000:
        return x + x
    if magnitude < 0x3e300000:
        return x
    if magnitude > 0x41b00000:
        result = math.log(abs(x)) + 6.93147180559945286227e-1
    elif magnitude > 0x40000000:
        absolute = abs(x)
        result = math.log(2.0 * absolute + 1.0 / (math.sqrt(x * x + 1.0) + absolute))
    else:
        squared = x * x
        result = real_log1p(abs(x) + squared / (1.0 + math.sqrt(1.0 + squared)))
    return math.copysign(result, x)


def real_power(x: float, y: float) -> float:
    x, y = float(x), float(y)
    hx, lx = _words(x)
    hy, ly = _words(y)
    ix, iy = hx & 0x7fffffff, hy & 0x7fffffff
    if y == 0.0:
        return 1.0
    if math.isnan(x) or math.isnan(y):
        return x + y
    yisint = 0
    if hx & 0x80000000:
        if abs(y) >= 9007199254740992.0:
            yisint = 2
        elif math.isfinite(y) and y.is_integer():
            yisint = 1 if int(y) & 1 else 2
    if ly == 0:
        if math.isinf(y):
            if abs(x) == 1.0:
                return math.nan
            return math.inf if (abs(x) > 1) == (y > 0) else 0.0
        if abs(y) == 1.0:
            return x if y > 0 else (1.0 / x if x != 0 else math.copysign(math.inf, x))
        if y == 2.0:
            return x * x
        if y == .5 and not (hx & 0x80000000):
            return math.sqrt(x)
    ax = abs(x)
    if lx == 0 and (math.isinf(ax) or ax == 0.0 or ax == 1.0):
        z = ax
        if y < 0:
            z = 1.0 / z if z != 0 else math.inf
        if hx & 0x80000000:
            if ax == 1.0 and yisint == 0:
                return math.nan
            if yisint == 1:
                z = -z
        return z
    if x < 0 and yisint == 0:
        return math.nan
    sign = -1.0 if x < 0 and yisint == 1 else 1.0
    if iy > 0x41e00000:
        if iy > 0x43f00000:
            if ix <= 0x3fefffff:
                return math.inf if y < 0 else 0.0
            if ix >= 0x3ff00000:
                return math.inf if y > 0 else 0.0
        if ix < 0x3fefffff:
            return sign * (math.inf if y < 0 else 0.0)
        if ix > 0x3ff00000:
            return sign * (math.inf if y > 0 else 0.0)
        t = ax - 1.0
        w = t * t * (.5 - t * (1.0 / 3.0 - t * .25))
        u = 1.44269502162933349609 * t
        v = t * 1.92596299112661746887e-8 - w * 1.44269504088896338700
        t1 = _truncate(u + v)
        t2 = v - (t1 - u)
    else:
        n = 0
        if ix < 0x00100000:
            ax *= 9007199254740992.0
            n -= 53
            ix = _words(ax)[0]
        n += (ix >> 20) - 0x3ff
        j = ix & 0xfffff
        ix = j | 0x3ff00000
        if j <= 0x3988e:
            k = 0
        elif j < 0xbb67a:
            k = 1
        else:
            k = 0
            n += 1
            ix -= 0x100000
        ax = _high(ax, ix)
        bp = (1.0, 1.5)[k]
        u = ax - bp
        v = 1.0 / (ax + bp)
        ss = u * v
        s_h = _truncate(ss)
        t_h = _high(0.0, ((ix >> 1) | 0x20000000) + 0x80000 + (k << 18))
        t_l = ax - (t_h - bp)
        s_l = v * ((u - s_h * t_h) - s_h * t_l)
        s2 = ss * ss
        r = s2 * s2 * (5.99999999999994648725e-1 + s2 * (4.28571428578550184252e-1
            + s2 * (3.33333329818377432918e-1 + s2 * (2.72728123808534006489e-1
            + s2 * (2.30660745775561754067e-1 + s2 * 2.06975017800338417784e-1)))))
        r += s_l * (s_h + ss)
        s2 = s_h * s_h
        t_h = _truncate(3.0 + s2 + r)
        t_l = r - ((t_h - 3.0) - s2)
        u = s_h * t_h
        v = s_l * t_h + t_l * ss
        p_h = _truncate(u + v)
        p_l = v - (p_h - u)
        z_h = 9.61796700954437255859e-1 * p_h
        z_l = -7.02846165095275826516e-9 * p_h + p_l * 9.61796693925975554329e-1 + (0., 1.35003920212974897128e-8)[k]
        dp_h = (0., 5.84962487220764160156e-1)[k]
        t1 = _truncate(((z_h + z_l) + dp_h) + float(n))
        t2 = z_l - (((t1 - float(n)) - dp_h) - z_h)
    y1 = _truncate(y)
    p_l = (y - y1) * t1 + y * t2
    p_h = y1 * t1
    z = p_l + p_h
    if z >= 1024.0:
        if z > 1024.0 or p_l + 8.0085662595372944372e-17 > z - p_h:
            return sign * math.inf
    elif z <= -1075.0:
        if z < -1075.0 or p_l <= z - p_h:
            return sign * 0.0
    j, _ = _words(z)
    if j & 0x80000000:
        j -= 0x100000000
    i = j & 0x7fffffff
    k = (i >> 20) - 0x3ff
    n = 0
    if i > 0x3fe00000:
        high = j + (0x100000 >> (k + 1))
        k = ((high & 0x7fffffff) >> 20) - 0x3ff
        t = _high(0., high & ~(0xfffff >> k))
        n = ((high & 0xfffff) | 0x100000) >> (20 - k)
        if j < 0:
            n = -n
        p_h -= t
    t = _truncate(p_l + p_h)
    u = t * 6.93147182464599609375e-1
    v = (p_l - (t - p_h)) * 6.93147180559945286227e-1 + t * -1.90465429995776804525e-9
    z = u + v
    w = v - (z - u)
    t = z * z
    t1 = z - t * (1.66666666666666019037e-1 + t * (-2.77777777770155933842e-3
        + t * (6.61375632143793436117e-5 + t * (-1.65339022054652515390e-6
        + t * 4.13813679705723846039e-8))))
    r = (z * t1) / (t1 - 2.) - (w + z * w)
    z = 1.0 - (r - z)
    high = _words(z)[0] + (n << 20)
    if high >> 20 <= 0:
        return sign * math.ldexp(z, n)
    return sign * _high(z, high)
