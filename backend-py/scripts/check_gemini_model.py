"""Component 5: Model selection check.

Run once to confirm the current model is cost-optimal.

  cd backend-py && PYTHONPATH=. python scripts/check_gemini_model.py
"""
from __future__ import annotations

from app.config import settings

model = settings.gemini_model
print(f"Current model: {model}")

OPTIMAL_MODELS = {"gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.0-flash-lite"}
PRO_MODELS_WARN = {"gemini-2.5-pro", "gemini-2.0-pro", "gemini-1.5-pro", "gemini-pro"}

if model in OPTIMAL_MODELS:
    print("✓ Model is cost-optimal. No change needed.")
elif any(p in model for p in ("pro", "ultra")):
    print(
        f"⚠  '{model}' is a Pro/Ultra variant — significantly more expensive.\n"
        "   Recommendation: switch to 'gemini-2.0-flash' in app/config.py for\n"
        "   primary text extraction and vision. Pro is overkill for structured\n"
        "   field extraction from course pages."
    )
else:
    print(f"ℹ  Model '{model}' is not in the known optimal set {OPTIMAL_MODELS}.\n"
          "   Review pricing before using in production.")
