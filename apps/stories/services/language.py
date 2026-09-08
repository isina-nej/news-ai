"""Language detection service (lingua-backed, failure-safe).

Keeps three signals separate:
- source language (what the Source row claims),
- detected language (what the detector says about THIS text),
- detection confidence.

Short texts get low confidence; detection failure never raises into ingestion.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _detector():
    try:
        from lingua import Language, LanguageDetectorBuilder

        return LanguageDetectorBuilder.from_languages(
            Language.ENGLISH,
            Language.PERSIAN,
            Language.ARABIC,
            Language.FRENCH,
            Language.GERMAN,
            Language.RUSSIAN,
            Language.TURKISH,
            Language.SPANISH,
        ).build()
    except Exception:
        return None


_ISO_MAP = {
    "ENGLISH": "en",
    "PERSIAN": "fa",
    "ARABIC": "ar",
    "FRENCH": "fr",
    "GERMAN": "de",
    "RUSSIAN": "ru",
    "TURKISH": "tr",
    "SPANISH": "es",
}


def detect_language(text: str) -> dict:
    """Return {language, confidence, reliable} for a text blob.

    - Empty/very short text (<20 chars): {"language": "und", "confidence": 0.0}.
    - Detector unavailable or exception: same safe fallback, never raises.
    """
    cleaned = (text or "").strip()
    if len(cleaned) < 20:
        return {"language": "und", "confidence": 0.0, "reliable": False}
    detector = _detector()
    if detector is None:
        return {"language": "und", "confidence": 0.0, "reliable": False}
    try:
        values = detector.compute_language_confidence_values(cleaned)
        if not values:
            return {"language": "und", "confidence": 0.0, "reliable": False}
        best = max(values, key=lambda v: v.value)
        iso = _ISO_MAP.get(str(best.language).split(".")[-1], "und")
        confidence = round(float(best.value), 4)
        return {
            "language": iso,
            "confidence": confidence,
            "reliable": confidence >= 0.6 and len(cleaned) >= 40,
        }
    except Exception:
        return {"language": "und", "confidence": 0.0, "reliable": False}
