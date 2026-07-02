from pathlib import Path

import numpy as np
import pytest
import scipy.io as sio

import crystallization_mpc.apps.gsensor.detection.update_line as update_line_module


DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "gsensor"
CASE_PATHS = sorted(DATA_DIR.glob("hough_debug_*.mat"))


@pytest.mark.parametrize("case_path", CASE_PATHS, ids=lambda path: path.name)
def test_hough_matches_matlab(case_path):
    case = load_case(case_path)

    H_actual, theta_actual, rho_actual = update_line_module.hough(
        case.I_hough_input,
        theta=case.theta_input,
    )

    theta_expected = np.asarray(case.theta_input, dtype=float).reshape(-1)
    rho_expected = np.asarray(case.rho_output, dtype=float).reshape(-1)
    H_expected = np.asarray(case.H_raw)

    assert_arrays_equal_with_context(
        theta_actual,
        theta_expected,
        case_path=case_path,
        layer="hough theta",
    )
    assert_hough_rho_matches(rho_actual, rho_expected, case_path)
    assert_arrays_equal_with_context(
        H_actual,
        H_expected,
        case_path=case_path,
        layer="hough accumulator",
    )


@pytest.mark.parametrize("case_path", CASE_PATHS, ids=lambda path: path.name)
def test_rho_filter_matches_matlab(case_path):
    case = load_case(case_path)

    H_actual = apply_current_matlab_rho_filter(
        case.H_raw,
        case.rho_output,
        case.rho_min_max,
    )

    assert_arrays_equal_with_context(
        H_actual,
        np.asarray(case.H_rho_filtered),
        case_path=case_path,
        layer="rho filter",
    )


@pytest.mark.parametrize("case_path", CASE_PATHS, ids=lambda path: path.name)
def test_houghpeaks_matches_matlab(case_path):
    case = load_case(case_path)

    peaks_actual = normalize_peaks(
        update_line_module.houghpeaks(
            case.H_rho_filtered,
            int(case.params_G.num_peak),
            nhood_size=(9, 1),
        )
    )
    peaks_expected = matlab_peaks_zero_based(case.peaks_matlab_1based)

    assert peaks_actual == peaks_expected, (
        f"\ncase: {case_path.name}"
        "\nlayer: houghpeaks"
        f"\nexpected zero-based peaks: {peaks_expected}"
        f"\nactual peaks:              {peaks_actual}"
    )


def load_case(case_path):
    data = sio.loadmat(case_path, squeeze_me=True, struct_as_record=False)
    return data["hough_debug"]


def matlab_peaks_zero_based(peaks):
    peaks_array = np.asarray(peaks, dtype=int)
    if peaks_array.size == 0:
        return []
    if peaks_array.ndim == 1:
        peaks_array = peaks_array.reshape(1, 2)
    return normalize_peaks(peaks_array - 1)


def normalize_peaks(peaks):
    return [tuple(int(value) for value in row) for row in np.asarray(peaks)]


def apply_current_matlab_rho_filter(H_raw, rho_output, rho_min_max):
    H = np.asarray(H_raw).copy()
    rho = np.asarray(rho_output, dtype=float).reshape(-1)
    rho_min, rho_max = np.asarray(rho_min_max, dtype=float).reshape(-1)
    mask = (rho < rho_min) | (rho > rho_max)

    # Reproduce the current MATLAB statement exactly:
    # Hs(rhos < rho_min | rhos > rho_max) = 0;
    #
    # With Hs as a matrix and rhos as a vector, MATLAB applies the logical
    # vector through column-major linear indexing, so this only addresses the
    # first numel(rhos) elements of Hs(:), not every rho row across all columns.
    H_flat_matlab_order = H.reshape(-1, order="F")
    H_flat_matlab_order[: mask.size][mask] = 0
    return H_flat_matlab_order.reshape(H.shape, order="F")


def assert_hough_rho_matches(actual, expected, case_path):
    actual = np.asarray(actual, dtype=float).reshape(-1)
    expected = np.asarray(expected, dtype=float).reshape(-1)

    if actual.shape != expected.shape:
        raise AssertionError(
            f"\ncase: {case_path.name}"
            "\nlayer: hough rho"
            f"\nexpected shape: {expected.shape}"
            f"\nactual shape:   {actual.shape}"
            f"\nexpected range: {expected[0]} to {expected[-1]}"
            f"\nactual range:   {actual[0]} to {actual[-1]}"
        )

    assert_arrays_equal_with_context(
        actual,
        expected,
        case_path=case_path,
        layer="hough rho",
    )


def assert_arrays_equal_with_context(actual, expected, *, case_path, layer):
    actual = np.asarray(actual)
    expected = np.asarray(expected)

    if actual.shape != expected.shape:
        raise AssertionError(
            f"\ncase: {case_path.name}"
            f"\nlayer: {layer}"
            f"\nexpected shape: {expected.shape}"
            f"\nactual shape:   {actual.shape}"
        )

    if not np.array_equal(actual, expected):
        diff = actual.astype(float) - expected.astype(float)
        nonzero = np.argwhere(diff != 0)
        first_indices = nonzero[:10].tolist()
        raise AssertionError(
            f"\ncase: {case_path.name}"
            f"\nlayer: {layer}"
            f"\nshape: {actual.shape}"
            f"\ndifferent elements: {nonzero.shape[0]}"
            f"\nmax absolute diff: {np.max(np.abs(diff))}"
            f"\nfirst different indices: {first_indices}"
        )
