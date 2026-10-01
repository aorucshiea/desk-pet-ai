from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from gateway import llama_client
from gateway import server as server_mod


def test_windows_auto_normalises_to_cpu(monkeypatch):
    monkeypatch.delenv("PET_DEVICE", raising=False)
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "AMD64")

    assert llama_client._normalise_device("auto") == "cpu"
    assert llama_client._normalise_device("") == "cpu"


def test_macos_auto_normalises_to_metal(monkeypatch):
    monkeypatch.delenv("PET_DEVICE", raising=False)
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "arm64")

    assert llama_client._normalise_device("auto") == "metal"


def test_windows_devices_reports_cpu_recommended_and_vulkan_experimental(tmp_path, monkeypatch):
    vulkan = tmp_path / "llama-server.exe"
    vulkan.write_text("", encoding="utf-8")
    monkeypatch.delenv("PET_DEVICE", raising=False)
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        llama_client,
        "_candidate_binary_paths",
        lambda device=None: [vulkan] if device == "vulkan" else [],
    )

    info = llama_client.detect_backend()

    assert info["recommended"] == "cpu"
    assert info["current"] == "cpu"
    assert "cpu" in info["available"]
    assert "vulkan" in info["available"]
    assert "vulkan" in info["experimental"]


def test_macos_devices_do_not_expose_vulkan(monkeypatch):
    monkeypatch.setenv("PET_DEVICE", "vulkan")
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(llama_client.shutil, "which", lambda _name: None)

    info = llama_client.detect_backend()

    assert llama_client._normalise_device("vulkan") == "metal"
    assert info["recommended"] == "metal"
    assert info["current"] == "metal"
    assert "vulkan" not in info["available"]
    assert "vulkan" not in info["experimental"]


def test_windows_vulkan_binary_paths_do_not_fall_back_to_default_cpu(monkeypatch):
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(llama_client, "_platform_triple", lambda: "win-x64")
    monkeypatch.setattr(llama_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("PET_LLAMA_SERVER", raising=False)

    paths = llama_client._candidate_binary_paths("vulkan")
    rendered = [str(p) for p in paths]

    assert any("backends/vulkan" in s.replace("\\", "/") for s in rendered)
    assert not any(s.replace("\\", "/").endswith("bin/win-x64/llama-server.exe") for s in rendered)
    assert not any(s.replace("\\", "/").endswith("llama.cpp/build/bin/llama-server.exe") for s in rendered)


def test_windows_vulkan_binary_paths_ignore_generic_override(monkeypatch, tmp_path):
    override = tmp_path / "cpu-override.exe"
    override.write_text("", encoding="utf-8")
    monkeypatch.setenv("PET_LLAMA_SERVER", str(override))
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(llama_client, "_platform_triple", lambda: "win-x64")
    monkeypatch.setattr(llama_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    paths = llama_client._candidate_binary_paths("vulkan")

    assert override not in paths


def test_cpu_backend_does_not_pass_gpu_layers(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_text("", encoding="utf-8")
    server = llama_client.LlamaServer(model_path=model, n_gpu_layers=-1)
    server.device = "cpu"
    server._binary = Path("llama-server.exe")
    server.port = 18766

    argv = server._build_argv()

    assert "--gpu-layers" not in argv


def test_set_device_rejects_vulkan_off_windows(monkeypatch):
    monkeypatch.setenv("PET_DEVICE", "metal")
    monkeypatch.setattr(server_mod.platform, "system", lambda: "Darwin")
    app = server_mod.build_app(initial_model=None)

    with TestClient(app) as client:
        response = client.post("/api/set-device", json={"device": "vulkan"})

    assert response.status_code == 400
    assert "only configurable on Windows" in response.json()["error"]
    assert llama_client.os.environ["PET_DEVICE"] == "metal"


# ── 推理后端: what Settings may offer, and why ───────────────────────────
# The captain's report: "推理后端只能选 cpu，无法选 vulkan 和 cuda". The old
# Windows branch hardcoded cpu(+vulkan-if-installed) and never listed cuda,
# while the UI looped over a literal ["cpu","vulkan"]. Detection now asks
# the installed engine which devices it can actually address.

def _win_windows(monkeypatch):
    monkeypatch.delenv("PET_DEVICE", raising=False)
    monkeypatch.setattr(llama_client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llama_client.platform, "machine", lambda: "AMD64")


def _stub_paths(monkeypatch, shared, per_backend):
    """`shared` = what the no-backend lookup finds; `per_backend[be]` = the
    dedicated backends/<be>/ build (empty list when only the shared binary
    plus its DLL exists)."""
    def paths(device=None):
        if device in (None, "", "auto", "cpu"):
            return list(shared)
        return list(per_backend.get(device, []))
    monkeypatch.setattr(llama_client, "_candidate_binary_paths", paths)


def test_parse_list_devices_reads_names_and_skips_the_none_line():
    out = "Available devices:\n  CUDA0\n  VULKAN0\n"
    assert llama_client._parse_list_devices(out) == ["CUDA0", "VULKAN0"]
    assert llama_client._parse_list_devices("Available devices:\n  (none)\n") == []
    assert llama_client._parse_list_devices("") == []


def test_backend_of_device_maps_llama_names():
    assert llama_client.backend_of_device("CUDA0") == "cuda"
    assert llama_client.backend_of_device("VULKAN1") == "vulkan"
    assert llama_client.backend_of_device("CPU0") == ""


def test_windows_cuda_is_offered_when_the_engine_sees_a_cuda_device(tmp_path, monkeypatch):
    _win_windows(monkeypatch)
    shared = tmp_path / "llama-server.exe"
    shared.write_text("", encoding="utf-8")
    (tmp_path / "ggml-cuda.dll").write_text("", encoding="utf-8")
    _stub_paths(monkeypatch, [shared], {"cuda": [shared], "vulkan": [], "metal": []})
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: ["CUDA0"])

    info = llama_client.detect_backend()

    assert "cuda" in info["available"]
    assert info["devices"] == ["CUDA0"]
    assert info["current"] == "cpu"  # offered is not the same as chosen


def test_windows_cuda_is_explained_not_offered_when_no_device_answers(tmp_path, monkeypatch):
    """The real state of this box: ggml-cuda.dll is present, nvidia-smi runs,
    yet llama-server enumerates zero devices because the CUDA runtime DLLs
    the build needs are missing. Hiding the option silently is what made
    this look like a UI bug."""
    _win_windows(monkeypatch)
    shared = tmp_path / "llama-server.exe"
    shared.write_text("", encoding="utf-8")
    (tmp_path / "ggml-cuda.dll").write_text("", encoding="utf-8")
    _stub_paths(monkeypatch, [shared], {"cuda": [shared], "vulkan": [], "metal": []})
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: [])

    info = llama_client.detect_backend()

    assert info["available"] == ["cpu"]
    assert "cuda" not in info["available"]
    assert "枚举不到 CUDA 设备" in info["reasons"]["cuda"]


def test_windows_keeps_installed_backends_when_the_probe_cannot_run(tmp_path, monkeypatch):
    """An unaskable question must not remove an option that may work — that
    is the pre-existing behaviour the old test suite locks in."""
    _win_windows(monkeypatch)
    shared = tmp_path / "llama-server.exe"
    shared.write_text("", encoding="utf-8")
    (tmp_path / "ggml-cuda.dll").write_text("", encoding="utf-8")
    _stub_paths(monkeypatch, [shared], {"cuda": [shared], "vulkan": [], "metal": []})
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: None)

    info = llama_client.detect_backend()

    assert "cuda" in info["available"]
    assert "未能向引擎确认" in info["reasons"]["cuda"]


def test_windows_missing_backend_says_how_to_install_it(tmp_path, monkeypatch):
    _win_windows(monkeypatch)
    shared = tmp_path / "llama-server.exe"
    shared.write_text("", encoding="utf-8")
    _stub_paths(monkeypatch, [shared], {"cuda": [], "vulkan": [], "metal": []})
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: [])

    info = llama_client.detect_backend()

    assert info["available"] == ["cpu"]
    assert "未安装" in info["reasons"]["vulkan"]
    assert "未安装" in info["reasons"]["cuda"]


def test_explicit_backend_passes_the_engines_own_device_name(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.write_text("", encoding="utf-8")
    server = llama_client.LlamaServer(model_path=model, n_gpu_layers=-1)
    server.device = "cuda"
    server._binary = Path("llama-server.exe")
    server.port = 18766
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: ["VULKAN0", "CUDA0"])

    argv = server._build_argv()

    assert argv[argv.index("--device") + 1] == "CUDA0"
    assert "--gpu-layers" in argv


def test_unprobed_backend_does_not_get_a_guessed_device_name(tmp_path, monkeypatch):
    """llama-server rejects an unknown --device value and refuses to start,
    so no answer means no flag."""
    model = tmp_path / "model.gguf"
    model.write_text("", encoding="utf-8")
    server = llama_client.LlamaServer(model_path=model, n_gpu_layers=-1)
    server.device = "vulkan"
    server._binary = Path("llama-server.exe")
    server.port = 18766
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: None)

    argv = server._build_argv()

    assert "--device" not in argv


def test_backend_lib_present_only_counts_sibling_ggml_dlls(tmp_path):
    (tmp_path / "ggml-cuda.dll").write_text("", encoding="utf-8")
    (tmp_path / "ggml-cpu-x64.dll").write_text("", encoding="utf-8")
    assert llama_client._backend_lib_present(tmp_path, "cuda") is True
    assert llama_client._backend_lib_present(tmp_path, "vulkan") is False
    assert llama_client._backend_lib_present(tmp_path / "nope", "cuda") is False


def test_windows_never_reports_metal_as_installed(tmp_path, monkeypatch):
    """`_candidate_binary_paths("metal")` has no Windows backend directory,
    so it returns the SHARED binary again. Counting that as "Metal
    installed" made /api/devices advertise a backend the box cannot run."""
    _win_windows(monkeypatch)
    shared = tmp_path / "llama-server.exe"
    shared.write_text("", encoding="utf-8")
    _stub_paths(monkeypatch, [shared], {"cuda": [], "vulkan": [], "metal": [shared]})
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: [])

    info = llama_client.detect_backend()

    assert info["installed"] == []
    assert "metal" not in info["installed"]
    assert llama_client._dedicated_backend_binary("metal", [shared]) is None


def test_dedicated_backend_build_counts_as_installed(tmp_path, monkeypatch):
    _win_windows(monkeypatch)
    shared = tmp_path / "llama-server.exe"
    shared.write_text("", encoding="utf-8")
    vulkan_dir = tmp_path / "backends" / "vulkan"
    vulkan_dir.mkdir(parents=True)
    vulkan = vulkan_dir / "llama-server.exe"
    vulkan.write_text("", encoding="utf-8")
    _stub_paths(monkeypatch, [shared], {"cuda": [], "vulkan": [vulkan], "metal": []})
    monkeypatch.setattr(llama_client, "probe_device_names", lambda _b: ["VULKAN0"])

    info = llama_client.detect_backend()

    assert info["installed"] == ["vulkan"]
    assert "vulkan" in info["available"]
    assert "vulkan" in info["experimental"]
    assert info["devices"] == ["VULKAN0"]
