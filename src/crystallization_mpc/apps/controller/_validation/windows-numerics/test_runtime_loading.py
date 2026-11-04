"""Portable loader contracts and real host linear algebra, without equipment."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from packaging.requirements import Requirement
import pytest

from crystallization_mpc.apps.controller.algorithm import _mkl_runtime as runtime
from crystallization_mpc.apps.controller.algorithm import _numeric_backend as numeric


@pytest.mark.parametrize("machine", ["AMD64", "amd64", "x86_64"])
def test_windows_wheel_layout(machine, tmp_path):
    candidates, directories = runtime.runtime_layout("win32", machine, 8, tmp_path, tmp_path)
    assert candidates[0] == tmp_path / "Library/bin/mkl_rt.2.dll"
    assert directories == (tmp_path / "Library/bin", tmp_path / "Scripts")


def test_linux_layout_is_unchanged(tmp_path):
    assert runtime.runtime_layout("linux", "x86_64", 8, tmp_path, tmp_path) == (
        (tmp_path / "lib/libmkl_rt.so.2",), (),
    )


@pytest.mark.parametrize("system,machine,pointers", [
    ("win32", "AMD64", 4), ("win32", "ARM64", 8),
    ("linux", "aarch64", 8), ("darwin", "x86_64", 8),
])
def test_unsupported_abi_is_rejected(system, machine, pointers, tmp_path):
    with pytest.raises(ImportError, match="64-bit Python"):
        runtime.runtime_layout(system, machine, pointers, tmp_path, tmp_path)


def fake_platform(monkeypatch, tmp_path, *, system="win32"):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform=system, prefix=str(tmp_path)))
    monkeypatch.setattr(runtime, "platform", SimpleNamespace(machine=lambda: "AMD64"))
    monkeypatch.setattr(runtime, "struct", SimpleNamespace(calcsize=lambda name: 8))
    monkeypatch.setattr(runtime, "sysconfig", SimpleNamespace(get_path=lambda name: str(tmp_path)))


def test_windows_loads_absolute_dll_and_retains_search_handles(monkeypatch, tmp_path):
    root = tmp_path / "Python environment with spaces"
    dll = root / "Library/bin/mkl_rt.2.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()
    (root / "Scripts").mkdir()
    fake_platform(monkeypatch, root)
    registered, handles, loaded = [], [], []

    def register(path):
        registered.append(path)
        handle = SimpleNamespace(close=lambda: pytest.fail("Live directory handle closed"))
        handles.append(handle)
        return handle

    library = object()
    monkeypatch.setattr(runtime.os, "add_dll_directory", register, raising=False)
    monkeypatch.setattr(runtime.ctypes, "CDLL", lambda path: loaded.append(path) or library)
    path, result, retained = runtime.load_mkl_runtime()
    assert path == dll and result is library
    assert loaded == [str(dll)]
    assert registered == [str(dll.parent), str(root / "Scripts")]
    assert retained == tuple(handles)


def test_windows_dependency_error_closes_search_handles(monkeypatch, tmp_path):
    dll = tmp_path / "Library/bin/mkl_rt.2.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()
    fake_platform(monkeypatch, tmp_path)
    closed = []
    monkeypatch.setattr(runtime.os, "add_dll_directory",
                        lambda path: SimpleNamespace(close=lambda: closed.append(path)), raising=False)

    def unavailable(path):
        raise OSError("Missing dependent DLL")

    monkeypatch.setattr(runtime.ctypes, "CDLL", unavailable)
    with pytest.raises(ImportError, match="Reinstall mkl==2025.0.1") as error:
        runtime.load_mkl_runtime()
    assert isinstance(error.value.__cause__, OSError)
    assert closed == [str(dll.parent)]


def test_missing_runtime_never_loads_from_path(monkeypatch, tmp_path):
    fake_platform(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime.ctypes, "CDLL", lambda path: pytest.fail("Unexpected DLL load"))
    with pytest.raises(ImportError, match="python -m pip install -r requirements.txt"):
        runtime.load_mkl_runtime()


def test_distinct_data_prefix_is_supported(tmp_path):
    data = tmp_path / "data"
    prefix = tmp_path / "python"
    candidates, _ = runtime.runtime_layout("win32", "AMD64", 8, prefix, data)
    assert candidates[0] == data / "Library/bin/mkl_rt.2.dll"
    assert prefix / "Library/bin/mkl_rt.2.dll" in candidates


@pytest.mark.parametrize("system,machine,enabled", [
    ("win32", "AMD64", True), ("linux", "x86_64", True),
    ("win32", "ARM64", False), ("linux", "aarch64", False), ("darwin", "arm64", False),
])
def test_dependency_marker_selects_supported_wheels(system, machine, enabled):
    root = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").is_file())
    requirement = next(Requirement(line) for line in (root / "requirements.txt").read_text().splitlines()
                       if line.startswith("mkl=="))
    assert requirement.marker.evaluate({"sys_platform": system, "platform_machine": machine}) is enabled
    assert str(requirement.specifier) == "==2025.0.1"


@pytest.mark.parametrize("dtype", [float, complex])
def test_actual_host_cholesky_qr_and_svd(dtype):
    a = np.array([[4., 1.], [1., 3.]], dtype=dtype)
    if dtype is complex:
        a[0, 1] += .5j
        a[1, 0] -= .5j
    upper = numeric.cholesky_upper(a)
    np.testing.assert_allclose(upper.conj().T @ upper, a, rtol=1e-14, atol=1e-14)
    r = numeric.qr_r(a)
    np.testing.assert_allclose(r.conj().T @ r, a.conj().T @ a, rtol=1e-14, atol=1e-14)
    singular, right = numeric.svd_right(a)
    np.testing.assert_allclose(np.sort(singular), np.sort(np.linalg.svd(a, compute_uv=False)), rtol=1e-14)
    np.testing.assert_allclose(right @ right.conj().T, np.eye(2), atol=1e-14)


def test_actual_host_symmetric_solver_and_version():
    matrix = np.array([[3., 2.], [2., -1.]])
    values = np.array([1., 4.])
    np.testing.assert_allclose(matrix @ numeric.symmetric_solve(matrix, values), values, atol=1e-14)
    np.testing.assert_allclose(numeric.symmetric_eigenvalues(matrix), np.linalg.eigvalsh(matrix), atol=1e-14)
    assert "Version 2025.0.1-" in numeric.provenance()["version"]
    assert numeric.provenance()["branch"] == "AVX2"
    assert numeric.provenance()["threads"] == "1"
