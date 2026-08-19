# Kamil Vision — Plan

**End goal:** Kamil runs on a home server/PC; a smart-glasses-type wearable feeds it
what you see, so Kamil is "on hand" — able to look, answer, and remember, hands-free.

**Guiding principle (non-negotiable):** privacy. "Cloud" here means **your own Kamil
box reached over a private VPN**, never a third-party vision API. Frames travel an
encrypted tunnel to your machine and nowhere else. Same guarantee as today, just
reachable from your face.

---

## Architecture (4 layers, built bottom-up)

```
[wearable cam] --frames--> [private VPN] --> [Kamil server: VLM tier] --> [memory]
   capture + gate            Tailscale         see() + /vision            salient-only
```

1. **Eyes (VLM tier)** — a local vision-language model Kamil can query with an image.
2. **Transport** — Tailscale/WireGuard so the device reaches Kamil securely, no open ports.
3. **Streaming + gating** — send sequential *frames* (never video), only when something
   changes or you ask; discard the rest.
4. **Memory** — keep the *observation*, delete the raw frame.

Each layer is testable alone. We build **eyes first** — everything stacks on it.

---

## Layer 1 — The VLM tier (Session 1, the foundation)

Mirror the existing `heavy_tier` pattern: an on-demand llama-server serving a
multimodal GGUF, loaded when vision is needed, freed when idle.

**Model:** Qwen3-VL-8B, **Q5_K_M or Q8_0** (your pick).
- VRAM (3090/24GB): Q5 weights ≈5.5GB, Q8 ≈8.5GB, + mmproj projector ≈1–2GB + KV.
  Comfortably fits, and can even coexist with the chat 8B (~16GB total for Q8).
- ⚠️ **First build-time check:** confirm the current llama.cpp build (~b8994) has
  Qwen3-VL multimodal support (needs a `mmproj` GGUF + libmtmd in llama-server).
  Qwen3-VL is new — if support isn't landed yet, fall back to **Qwen2.5-VL-7B**
  (proven in llama.cpp) at the same quant, and revisit Qwen3-VL later. Same tier
  code either way.

**How llama.cpp serves it:** `llama-server -m qwen3vl-8b-Q5_K_M.gguf --mmproj
qwen3vl-mmproj.gguf --port 8088 …`. The OpenAI `/v1/chat/completions` endpoint then
accepts image content parts (base64 `data:` URI), so an image rides in as a normal
chat turn.

**New/changed files (Session 1):**
- `core/vision_tier.py` — resolve the VLM+mmproj GGUFs, `ensure_loaded()`, on port 8088,
  `_kill_stale()`, idle-unload (copy heavy_tier.py).
- `core/hwfit.py` — mmproj-aware VRAM fit (projector is extra, always on GPU).
- `models/client.py` — `chat_image(messages, image_bytes, …)`: attach an image as a
  base64 data-URI content part.
- `actions/tools.py` — `see(image_path, question="what is this?")` tool → routes to the
  vision tier, returns a text description/answer. AUTO permission (read-only).
- `interfaces/web/app.py` — `POST /vision` (accepts an uploaded frame + optional
  question, returns the answer) so the app / a device can push a frame.
- `prompts/` — a short vision-answer style (concise, grounded, "say what you actually
  see, don't guess").

**Session 1 done = success test:** point it at a static image and a webcam grab; Kamil
describes it correctly, reads text/signs, answers "what am I looking at?" — all local,
verified in the terminal + app.

---

## Layer 2 — Transport (later session)

- **Tailscale** on the Kamil box and the device → they share a private network; the
  device hits `http://kamil-box:3000/vision`. No port-forwarding, no exposure.
- TLS inside the tunnel is redundant but cheap; keep it simple first.

## Layer 3 — Streaming + gating (later, the optimization you described)

- Device captures continuously but **never uploads video**. A cheap on-device gate
  decides what to send:
  - **scene-change / motion diff** (frame differs enough from the last sent one), or
  - **explicit voice trigger** ("Kamil, what am I looking at?").
- Only gated frames hit the VLM (the expensive part fires rarely, on demand).
- Server keeps a small **ring buffer** of recent frames; everything else is dropped
  immediately.
- Target cadence: ~1 keyframe/sec max when active, 0 when idle.

## Layer 4 — Memory policy

- The VLM's *observation* may be written to long-term memory ("2pm: parking sign, 2hr
  limit") **only if salient** (reuse the existing memory-reflect selectivity).
- **Raw frames are deleted after processing** unless you explicitly pin one. No image
  hoarding — matches your "delete unnecessary data."

---

## Hardware — don't block on glasses

Good open smart glasses barely exist. Prototype the pipeline with what's on hand:
1. **Phone cam → Kamil** (a tiny app / browser page streaming keyframes) — fastest.
2. **Raspberry Pi + camera** or **ESP32-CAM** as a DIY wearable.
3. Swap in real glasses later — the server pipeline is identical regardless of lens.

---

## Build order

| Session | Scope |
|---|---|
| **1 (next, ~3h)** | VLM tier + `see()` tool + `/vision` endpoint; verify Qwen3-VL support (else Qwen2.5-VL-7B); test on image + webcam. **Kamil can see.** |
| 2 | Frame-gating (motion/scene-change) + voice-trigger "what am I looking at" wired to the voice call mode + TTS answer. |
| 3 | Tailscale transport; phone-cam prototype pushing keyframes remotely. |
| 4 | Memory policy (salient observations, delete raw frames); ring buffer. |
| 5+ | Wearable hardware (Pi/ESP32 → glasses). |

## Open items to confirm at build time
- Qwen3-VL-8B GGUF + mmproj availability in the current llama.cpp build (fallback ready).
- Where to store the VLM GGUFs (`models/` — check disk).
- Whether vision tier coexists with the chat 8B in VRAM or swaps on demand (measure).
