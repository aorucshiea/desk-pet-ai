# -*- coding: utf-8 -*-
"""LynnReal-Omni Flash Lite ref2v test: Zero (green-screen ref) -> 5s video."""
import json, time, urllib.request, urllib.error

API = "http://127.0.0.1:8188"

PROMPT = """integrated_multimodal_description:

[Shot 1]
Reference image guidance: Take the provided cute cartoon creature as the definitive character prototype. Precisely match its snow-white rounded teardrop-shaped body, the single large glowing cyan eye, the small gentle mouth, and the tiny hexagonal ice crystal floating above its head. Match the soft TV-animation cel-shaded look and the clean smooth outlines.
[Shot 1] The small creature gently bobs up and down as if floating in water. The ice crystal above its head slowly rotates in place. The glowing cyan tail trail shimmers softly. The camera stays completely fixed with no zoom and no pan. The background stays exactly the same solid pure green screen with no changes, no shadows, no gradients. Seamless loop motion, subtle and calm."""

P = {
    "127": {"class_type": "UNETLoader", "inputs": {"unet_name": "lynnreal_omni_flash_int8_lite.safetensors", "weight_dtype": "default"}},
    "205": {"class_type": "LynnRealFlashTokenCompression", "inputs": {"model": ["127", 0], "start_block": 2, "end_block": 28, "spatial_stride": 2, "residual_gain": 1.0}},
    "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
    "119": {"class_type": "LynnRealH3VAELoader", "inputs": {"vae_name": "lynnreal_omni_light_vae_fp16.safetensors", "num_layers": 0, "tile_size": 0, "tile_overlap": 0, "compile_decoder": True}},
    "120": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
    "115": {"class_type": "ResolutionSelector", "inputs": {"aspect_ratio": "1:1 (Square)", "megapixels": 0.33, "multiple": 32}},
    "132": {"class_type": "PrimitiveFloat", "inputs": {"value": 5.0}},
    "131": {"class_type": "ComfyMathExpression", "inputs": {"expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17", "values.a": ["132", 0]}},
    "137": {"class_type": "LoadImage", "inputs": {"image": "Zero_ref_green.png"}},
    "138": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": PROMPT}},
    "136": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
        "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
        "prompt": ["138", 0],
        "width": ["115", 0], "height": ["115", 1], "length": ["131", 1],
        "ref_image_size": "match",
        "ref_images.ref_image_0": ["137", 0],
    }},
    "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": 7}},
    "123": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
    "124": {"class_type": "BasicScheduler", "inputs": {"model": ["205", 0], "scheduler": "simple", "steps": 3, "denoise": 1.0}},
    "126": {"class_type": "BasicGuider", "inputs": {"model": ["205", 0], "conditioning": ["136", 0]}},
    "125": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": ["124", 0], "latent_image": ["136", 1]}},
    "122": {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}},
    "121": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}},
    "130": {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "audio": ["121", 0], "fps": 24, "bit_depth": 8, "color_space": "sRGB", "codec": "none"}},
    "92": {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "filename_prefix": "anim/lynn_ref2v_zero_idle", "format": "auto", "format.codec": "auto", "codec": "auto"}},
}


def submit():
    req = urllib.request.Request(
        API + "/prompt",
        data=json.dumps({"prompt": P, "client_id": "lynnreal_test"}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("prompt_id")
    except urllib.error.HTTPError as e:
        print("!! submit rejected:", e.read().decode("utf-8", "replace")[:3000])
        return None


def wait(pid, deadline_min=20):
    t0 = time.time()
    while time.time() - t0 < deadline_min * 60:
        time.sleep(10)
        with urllib.request.urlopen(API + "/history/" + pid, timeout=30) as r:
            hist = json.load(r)
        if pid in hist:
            st = hist[pid].get("status", {})
            dt = int(time.time() - t0)
            print("done: %s  elapsed %dm%02ds" % (st.get("status_str"), dt // 60, dt % 60), flush=True)
            for _, node_out in hist[pid].get("outputs", {}).items():
                for _, val in node_out.items():
                    if isinstance(val, list):
                        for it in val:
                            if isinstance(it, dict) and "filename" in it:
                                print("artifact:", it.get("subfolder", ""), it["filename"])
            if st.get("status_str") != "success":
                for m in (st.get("messages") or [])[-3:]:
                    print("msg:", str(m)[:500])
            return st.get("status_str") == "success"
        print("  ...running %ds" % int(time.time() - t0), flush=True)
    print("!! timeout")
    return False


pid = submit()
if pid:
    print("submitted", pid, flush=True)
    ok = wait(pid)
    sys_exit = 0 if ok else 1
else:
    sys_exit = 1
raise SystemExit(sys_exit)
