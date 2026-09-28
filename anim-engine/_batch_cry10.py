# -*- coding: utf-8 -*-
"""Natural-variation batch: ONE prompt (hard crying), 10 seeds.
The prompt never changes -- seeds produce the biological variation."""
import json, time, urllib.request, urllib.error

API = "http://127.0.0.1:8188"
SEEDS = [11, 23, 37, 41, 53, 67, 79, 83, 97, 109]

PROMPT = """integrated_multimodal_description:

[Shot 1]
Reference image guidance: Take the provided cute anime girl as the definitive character prototype. Precisely match her round face, large amber eyes, clear dark eyebrows, small mouth, light blush on the cheeks, light brown fluffy hair with a single ahoge, and the oversized cream sweater.
[Shot 1] She is crying hard: heavy tears streaming down her cheeks, eyes squeezed shut, brows drawn together, mouth open in a sob, shoulders trembling slightly. She raises both hands to her face wiping the tears. The camera stays completely fixed with no zoom and no pan. The background stays exactly the same solid pure green screen with no changes, no shadows. Clean TV-animation look."""


def build(seed):
    return {
        "127": {"class_type": "UNETLoader", "inputs": {"unet_name": "lynnreal_omni_flash_int8_lite.safetensors", "weight_dtype": "default"}},
        "205": {"class_type": "LynnRealFlashTokenCompression", "inputs": {"model": ["127", 0], "start_block": 2, "end_block": 28, "spatial_stride": 2, "residual_gain": 1.0}},
        "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "119": {"class_type": "LynnRealH3VAELoader", "inputs": {"vae_name": "lynnreal_omni_light_vae_fp16.safetensors", "num_layers": 0, "tile_size": 0, "tile_overlap": 0, "compile_decoder": True}},
        "120": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "115": {"class_type": "ResolutionSelector", "inputs": {"aspect_ratio": "1:1 (Square)", "megapixels": 0.33, "multiple": 32}},
        "132": {"class_type": "PrimitiveFloat", "inputs": {"value": 5.0}},
        "131": {"class_type": "ComfyMathExpression", "inputs": {"expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17", "values.a": ["132", 0]}},
        "137": {"class_type": "LoadImage", "inputs": {"image": "Girl_ref_green.png"}},
        "138": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": PROMPT}},
        "136": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
            "prompt": ["138", 0],
            "width": ["115", 0], "height": ["115", 1], "length": ["131", 1],
            "ref_image_size": "match",
            "ref_images.ref_image_0": ["137", 0],
        }},
        "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "123": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "124": {"class_type": "BasicScheduler", "inputs": {"model": ["205", 0], "scheduler": "simple", "steps": 3, "denoise": 1.0}},
        "126": {"class_type": "BasicGuider", "inputs": {"model": ["205", 0], "conditioning": ["136", 0]}},
        "125": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": ["124", 0], "latent_image": ["136", 1]}},
        "122": {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}},
        "121": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}},
        "130": {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "audio": ["121", 0], "fps": 24, "bit_depth": 8, "color_space": "sRGB", "codec": "none"}},
        "92": {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "filename_prefix": "anim/cryhard/s%03d" % seed, "format": "auto", "format.codec": "auto", "codec": "auto"}},
    }


def post(P):
    req = urllib.request.Request(API + "/prompt",
        data=json.dumps({"prompt": P, "client_id": "crybatch"}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("prompt_id")
    except urllib.error.HTTPError as e:
        print("!! rejected:", e.read().decode("utf-8", "replace")[:1500], flush=True)
        return None


def wait(pid):
    t0 = time.time()
    while time.time() - t0 < 15 * 60:
        time.sleep(8)
        with urllib.request.urlopen(API + "/history/" + pid, timeout=30) as r:
            hist = json.load(r)
        if pid in hist:
            st = hist[pid].get("status", {})
            ok = st.get("status_str") == "success"
            print("  seed done: %s (%ds)" % (st.get("status_str"), int(time.time() - t0)), flush=True)
            if not ok:
                for m in (st.get("messages") or [])[-2:]:
                    print("   msg:", str(m)[:400], flush=True)
            return ok
    print("  !! timeout", flush=True)
    return False


# queue all 10 up front (ComfyUI runs them sequentially, model stays hot)
ids = []
for s in SEEDS:
    pid = post(build(s))
    print("queued seed %d -> %s" % (s, pid), flush=True)
    if pid:
        ids.append((s, pid))

fails = []
for s, pid in ids:
    print("waiting seed", s, flush=True)
    if not wait(pid):
        fails.append(s)
print("BATCH DONE. fails:", fails if fails else "none", flush=True)
