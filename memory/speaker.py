"""
memory/speaker.py

Speaker recognition for Kamil using resemblyzer.
Identifies who is speaking before processing any query.

Architecture:
- Each known speaker has a stored 256-dim voiceprint (mean of enrollment embeddings)
- New audio compared against all known voiceprints via cosine similarity
- Unknown speakers get "guest" trust level
- Owner (Adnan) gets full access
- Trusted speakers get family-level access

Trust levels:
- owner    : full context, all models, all memory
- trusted  : personal help, 8B model, no owner-private context
- guest    : general help only, 8B model, no personal context
"""

import os
import json
import numpy as np
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

from core.paths import data_path
SPEAKERS_PATH = data_path("speakers.json")
ENROLLMENT_SAMPLES_DIR = data_path("voice_samples")

# Similarity thresholds
OWNER_THRESHOLD = 0.78      # high confidence required for owner
TRUSTED_THRESHOLD = 0.75    # slightly lower for trusted
GUEST_THRESHOLD = 0.0       # everyone else is guest

# Enrollment
MIN_ENROLLMENT_SECONDS = 5  # minimum audio for enrollment
ENROLLMENT_EMBEDDINGS = 5   # how many embeddings to average per speaker


@dataclass
class Speaker:
    name: str
    trust_level: str        # "owner" | "trusted" | "guest"
    voiceprint: list[float] # 256-dim mean embedding
    enrolled_at: str
    enrollment_count: int   # number of samples used
    model_preference: str   # which model to use for this speaker


@dataclass
class IdentificationResult:
    speaker_name: str
    trust_level: str
    confidence: float
    model_preference: str
    identified: bool        # False = guest/unknown


# ── Encoder singleton ─────────────────────────────────────────────

_encoder = None

def get_encoder():
    global _encoder
    if _encoder is None:
        from resemblyzer import VoiceEncoder
        print("[speaker] Loading VoiceEncoder...")
        _encoder = VoiceEncoder()
        print("[speaker] VoiceEncoder ready")
    return _encoder


# ── Storage ───────────────────────────────────────────────────────

def _load_speakers() -> dict[str, Speaker]:
    if not os.path.exists(SPEAKERS_PATH):
        return {}
    try:
        with open(SPEAKERS_PATH, "r") as f:
            data = json.load(f)
        return {
            name: Speaker(**info)
            for name, info in data.items()
        }
    except Exception as e:
        print(f"[speaker] Error loading speakers: {e}")
        return {}


def _save_speakers(speakers: dict[str, Speaker]):
    os.makedirs(os.path.dirname(SPEAKERS_PATH), exist_ok=True)
    with open(SPEAKERS_PATH, "w") as f:
        json.dump(
            {name: asdict(s) for name, s in speakers.items()},
            f, indent=2
        )


# ── Enrollment ────────────────────────────────────────────────────

def enroll_speaker(
    name: str,
    audio_samples: list[np.ndarray],  # list of raw audio arrays at 16kHz
    trust_level: str = "trusted",
    model_preference: str = "auto"
) -> Speaker:
    """
    Enroll a new speaker from multiple audio samples.
    audio_samples: list of numpy arrays, each at 16kHz sample rate
    Returns the enrolled Speaker.
    """
    from resemblyzer import preprocess_wav

    if trust_level not in ("owner", "trusted", "guest"):
        raise ValueError(f"Invalid trust level: {trust_level}")

    if model_preference == "auto":
        model_preference = {
            "owner": "primary",    # DeepSeek 14B
            "trusted": "light",    # 8B model
            "guest": "light"
        }[trust_level]

    encoder = get_encoder()
    embeddings = []

    for audio in audio_samples:
        try:
            # Preprocess — resemblyzer expects 16kHz mono float32
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32)
            if audio.max() > 1.0:
                audio = audio / 32768.0  # normalize int16 to float

            wav = preprocess_wav(audio, source_sr=16000)
            if len(wav) < 16000 * 1:  # at least 1 second
                print(f"[speaker] Sample too short ({len(wav)/16000:.1f}s), skipping")
                continue

            embedding = encoder.embed_utterance(wav)
            embeddings.append(embedding)
        except Exception as e:
            print(f"[speaker] Error processing sample: {e}")
            continue

    if not embeddings:
        raise ValueError("No valid audio samples for enrollment")

    # Mean embedding as voiceprint
    voiceprint = np.mean(embeddings, axis=0).tolist()

    speaker = Speaker(
        name=name,
        trust_level=trust_level,
        voiceprint=voiceprint,
        enrolled_at=datetime.now().isoformat(),
        enrollment_count=len(embeddings),
        model_preference=model_preference
    )

    speakers = _load_speakers()
    speakers[name] = speaker
    _save_speakers(speakers)

    print(f"[speaker] Enrolled '{name}' ({trust_level}) from {len(embeddings)} samples")
    return speaker


def enroll_from_wav_file(
    name: str,
    wav_path: str,
    trust_level: str = "trusted"
) -> Speaker:
    """Enroll a speaker from a WAV file."""
    import soundfile as sf
    audio, sr = sf.read(wav_path)
    if sr != 16000:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    if len(audio.shape) > 1:
        audio = audio.mean(axis=1)  # stereo to mono
    return enroll_speaker(name, [audio], trust_level)


# ── Identification ────────────────────────────────────────────────

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def identify_speaker(
    audio: np.ndarray,
    sample_rate: int = 16000
) -> IdentificationResult:
    """
    Identify who is speaking from audio.
    Returns IdentificationResult with speaker info and confidence.
    """
    from resemblyzer import preprocess_wav

    speakers = _load_speakers()
    if not speakers:
        return IdentificationResult(
            speaker_name="guest",
            trust_level="guest",
            confidence=0.0,
            model_preference="light",
            identified=False
        )

    encoder = get_encoder()

    try:
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0

        wav = preprocess_wav(audio, source_sr=sample_rate)
        query_embedding = encoder.embed_utterance(wav)
    except Exception as e:
        print(f"[speaker] Error processing audio for identification: {e}")
        return IdentificationResult(
            speaker_name="guest",
            trust_level="guest",
            confidence=0.0,
            model_preference="light",
            identified=False
        )

    # Compare against all known speakers
    best_match = None
    best_score = 0.0

    for name, speaker in speakers.items():
        voiceprint = np.array(speaker.voiceprint)
        score = _cosine_similarity(query_embedding, voiceprint)

        if score > best_score:
            best_score = score
            best_match = speaker

    if best_match is None:
        return IdentificationResult(
            speaker_name="guest",
            trust_level="guest",
            confidence=0.0,
            model_preference="light",
            identified=False
        )

    # Apply threshold based on trust level
    threshold = {
        "owner": OWNER_THRESHOLD,
        "trusted": TRUSTED_THRESHOLD,
        "guest": GUEST_THRESHOLD
    }.get(best_match.trust_level, TRUSTED_THRESHOLD)

    if best_score >= threshold:
        print(f"[speaker] Identified: {best_match.name} (confidence={best_score:.3f})")
        return IdentificationResult(
            speaker_name=best_match.name,
            trust_level=best_match.trust_level,
            confidence=round(best_score, 3),
            model_preference=best_match.model_preference,
            identified=True
        )
    else:
        print(f"[speaker] Unknown speaker (best={best_match.name}, score={best_score:.3f} < threshold={threshold})")
        return IdentificationResult(
            speaker_name="guest",
            trust_level="guest",
            confidence=round(best_score, 3),
            model_preference="light",
            identified=False
        )


def identify_from_wav_bytes(wav_bytes: bytes) -> IdentificationResult:
    """
    Identify speaker from raw WAV bytes (from Whisper transcription pipeline).
    This is what gets called from the voice endpoint.
    """
    import io
    import soundfile as sf

    try:
        buf = io.BytesIO(wav_bytes)
        audio, sr = sf.read(buf)
        if len(audio.shape) > 1:
            audio = audio.mean(axis=1)
        return identify_speaker(audio, sample_rate=sr)
    except Exception as e:
        print(f"[speaker] Error reading WAV bytes: {e}")
        return IdentificationResult(
            speaker_name="guest",
            trust_level="guest",
            confidence=0.0,
            model_preference="light",
            identified=False
        )


# ── Management ────────────────────────────────────────────────────

def list_speakers() -> list[dict]:
    speakers = _load_speakers()
    return [
        {
            "name": s.name,
            "trust_level": s.trust_level,
            "enrolled_at": s.enrolled_at,
            "enrollment_count": s.enrollment_count,
            "model_preference": s.model_preference
        }
        for s in speakers.values()
    ]


def remove_speaker(name: str) -> bool:
    speakers = _load_speakers()
    if name in speakers:
        del speakers[name]
        _save_speakers(speakers)
        print(f"[speaker] Removed speaker: {name}")
        return True
    return False


def update_trust_level(name: str, trust_level: str) -> bool:
    speakers = _load_speakers()
    if name in speakers:
        speakers[name].trust_level = trust_level
        _save_speakers(speakers)
        return True
    return False


# ── Session context ───────────────────────────────────────────────

# Current session speaker — updated per voice interaction
_current_speaker: Optional[IdentificationResult] = None


def set_current_speaker(result: IdentificationResult):
    global _current_speaker
    _current_speaker = result


def get_current_speaker() -> Optional[IdentificationResult]:
    return _current_speaker


def get_current_trust_level() -> str:
    if _current_speaker:
        return _current_speaker.trust_level
    return "guest"


def get_current_model_preference() -> str:
    if _current_speaker:
        return _current_speaker.model_preference
    return "light"