"""Pinned, independently distributed BLAS/LAPACK for reference numeric order.

This loads the Python ``mkl`` distribution, never a MATLAB installation.  The
controller remains Python; the dense kernels are ordinary numerical libraries,
as are NumPy/SciPy's own compiled BLAS kernels.
"""
from __future__ import annotations

import ctypes as C
import os

import numpy as np

from ._mkl_runtime import load_mkl_runtime

os.environ["MKL_CBWR"] = "AVX2"
_library_path, _mkl, _dll_directory_handles = load_mkl_runtime()
_mkl.MKL_Set_Num_Threads(C.c_int(1))
_mkl.MKL_CBWR_Set(C.c_int(10))  # MKL_CBWR_AVX2
_mkl.LAPACKE_set_nancheck(C.c_int(0))
_version_buffer = C.create_string_buffer(512)
_mkl.MKL_Get_Version_String(_version_buffer, len(_version_buffer))
if "Version 2025.0.1-" not in _version_buffer.value.decode():
    raise ImportError("R2026a-compatible controller numerics require mkl==2025.0.1.")
_I, _P, _D, _CH = C.c_int, C.c_void_p, C.c_double, C.c_char
_chol = _mkl.LAPACKE_dpotrf
_chol.argtypes = [_I, _CH, _I, _P, _I]
_chol_complex = _mkl.LAPACKE_zpotrf
_chol_complex.argtypes = _chol.argtypes
_svd = _mkl.LAPACKE_dgesdd
_svd.argtypes = [_I, _CH, _I, _I, _P, _I, _P, _P, _I, _P, _I]
_svd_complex = _mkl.LAPACKE_zgesdd
_svd_complex.argtypes = _svd.argtypes
_qr = _mkl.LAPACKE_dgeqrf
_qr.argtypes = [_I, _I, _I, _P, _I, _P]
_qr_complex = _mkl.LAPACKE_zgeqrf
_qr_complex.argtypes = _qr.argtypes
_eigen = _mkl.LAPACKE_dsyevd
_eigen.argtypes = [_I, _CH, _CH, _I, _P, _I, _P]
_symmetric = _mkl.LAPACKE_dsysv
_symmetric.argtypes = [_I, _CH, _I, _I, _P, _I, _P, _P, _I]
_norm2 = _mkl.cblas_dnrm2
_norm2.argtypes = [_I, _P, _I]
_norm2.restype = _D
_norm2_complex = _mkl.cblas_dznrm2
_norm2_complex.argtypes = _norm2.argtypes
_norm2_complex.restype = _D


def provenance() -> dict[str, str]:
    buffer = C.create_string_buffer(512)
    _mkl.MKL_Get_Version_String(buffer, len(buffer))
    return {"version": buffer.value.decode(), "library": str(_library_path),
            "branch": "AVX2", "threads": "1"}


def norm_2(values: np.ndarray) -> float:
    """Scaled BLAS Euclidean norm of a real or complex vector."""
    is_complex = np.iscomplexobj(values)
    vector = np.ascontiguousarray(values, dtype=complex if is_complex else float).reshape(-1)
    return float((_norm2_complex if is_complex else _norm2)(vector.size, vector.ctypes.data, 1))


def cholesky_upper(matrix: np.ndarray) -> np.ndarray:
    is_complex = np.iscomplexobj(matrix)
    value = np.array(matrix, dtype=complex if is_complex else float, order="F", copy=True)
    n = value.shape[0]
    if is_complex and np.any(np.imag(np.diag(value)) != 0):
        raise np.linalg.LinAlgError("A complex Cholesky diagonal must be real.")
    flag = (_chol_complex if is_complex else _chol)(102, b"U", n, value.ctypes.data, max(1, n))
    if flag != 0:
        raise np.linalg.LinAlgError("Matrix is not positive definite.")
    return np.triu(value)


def svd_right(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    is_complex = np.iscomplexobj(matrix)
    dtype = complex if is_complex else float
    value = np.array(matrix, dtype=dtype, order="F", copy=True)
    m, n = value.shape
    if np.isnan(value).any():
        # The source SVD wrapper propagates NaN to its factors. LAPACKE's
        # DGESDD instead reports a negative argument index for this input.
        # Preserve those factors, rather than rejecting an R constructor
        # which deliberately bypasses the covariance-property validator.
        return np.full(min(m, n), np.nan), np.full((n, n), np.nan, dtype=dtype)
    singular = np.empty(min(m, n), dtype=float)
    left = np.empty((m, m), dtype=dtype, order="F")
    right = np.empty((n, n), dtype=dtype, order="F")
    flag = (_svd_complex if is_complex else _svd)(102, b"A", m, n, value.ctypes.data, max(1, m), singular.ctypes.data,
                left.ctypes.data, max(1, m), right.ctypes.data, max(1, n))
    if flag != 0:
        raise np.linalg.LinAlgError("Covariance SVD did not converge.")
    return singular, right


def qr_r(matrix: np.ndarray) -> np.ndarray:
    is_complex = np.iscomplexobj(matrix)
    dtype = complex if is_complex else float
    value = np.array(matrix, dtype=dtype, order="F", copy=True)
    m, n = value.shape
    tau = np.empty(min(m, n), dtype=dtype)
    flag = (_qr_complex if is_complex else _qr)(102, m, n, value.ctypes.data, max(1, m), tau.ctypes.data)
    if flag != 0:
        raise np.linalg.LinAlgError("Covariance QR failed.")
    return np.triu(value[:min(m, n), :])


def matmul(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    dtype = complex if np.iscomplexobj(left) or np.iscomplexobj(right) else float
    a = np.asarray(left, dtype=dtype)
    b = np.asarray(right, dtype=dtype)
    was_vector = b.ndim == 1
    rhs = b.reshape(-1, 1) if was_vector else b
    out = np.empty((a.shape[0], rhs.shape[1]), dtype=dtype)
    # All source EKF products have at most four columns.  MATLAB's small
    # matrix kernel accumulates those products in column order without FMA;
    # BLAS GEMM is a different rounded calculation even with the same MKL.
    for i in range(out.shape[0]):
        for j in range(out.shape[1]):
            value = np.complex128(0.0) if dtype is complex else np.float64(0.0)
            for k in range(a.shape[1]):
                value = value + a[i, k] * rhs[k, j]
            out[i, j] = value
    return out[:, 0] if was_vector else out


def symmetric_eigenvalues(matrix: np.ndarray) -> np.ndarray:
    value = np.array(matrix, dtype=float, order="F", copy=True)
    n = value.shape[0]
    out = np.empty(n, dtype=float)
    flag = _eigen(102, b"N", b"U", n, value.ctypes.data, max(1, n), out.ctypes.data)
    if flag != 0:
        raise np.linalg.LinAlgError("Covariance eigenvalue calculation failed.")
    return out


def solve_triangular(matrix: np.ndarray, values: np.ndarray, *, lower: bool,
                     check_finite: bool = False) -> np.ndarray:
    del check_finite
    a = np.asarray(matrix, dtype=float)
    out = np.array(values, dtype=float, copy=True)
    was_vector = out.ndim == 1
    b = out.reshape(-1, 1) if was_vector else out
    n = a.shape[0]
    # The reference small-matrix linsolve uses direct division for each RHS.
    # BLAS DTRSM/DTRSV can instead multiply a rounded reciprocal or use FMA;
    # those one-ulp differences are amplified by this model's singular ODEs.
    rows = range(n) if lower else range(n - 1, -1, -1)
    for i in rows:
        known = range(i) if lower else range(n - 1, i, -1)
        for j in range(b.shape[1]):
            value = b[i, j]
            for k in known:
                value = value - a[i, k] * b[k, j]
            b[i, j] = np.divide(value, a[i, i])
    return out


def symmetric_solve(matrix: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Direct symmetric-indefinite LDL solve of the primal-dual KKT system."""
    a = np.array(matrix, dtype=float, order="F", copy=True)
    b = np.array(values, dtype=float, order="F", copy=True)
    n = a.shape[0]
    pivots = np.empty(n, dtype=np.int32)
    columns = 1 if b.ndim == 1 else b.shape[1]
    flag = _symmetric(102, b"L", n, columns, a.ctypes.data, max(1, n),
                      pivots.ctypes.data, b.ctypes.data, max(1, n))
    if flag != 0:
        raise np.linalg.LinAlgError("Primal-dual KKT factorization is singular.")
    return b
