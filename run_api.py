#!/usr/bin/env python3
"""FastAPI servisini baslatir: http://127.0.0.1:8000/docs"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    # Guvenlik durusunu baslatmadan once goster. APP_ENV=production ise
    # guvensiz bir kombinasyon uygulamayi zaten baslatmaz (fail-fast).
    from certaops import get_settings  # noqa: E402

    posture = get_settings().posture()
    if posture["production_blockers"]:
        renk = "\033[31m" if posture["app_env"] == "production" else "\033[33m"
        print(f"{renk}Guvenlik durusu ({posture['app_env']}):\033[0m")
        for blocker in posture["production_blockers"]:
            print(f"  - {blocker}")
        print("  \033[2mAyrinti: .env.example ve config/principals.example.json\033[0m")

    uvicorn.run(
        "certaops.api:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=os.getenv("API_RELOAD", "false").lower() == "true",
        # Coklu worker kalici oturum/onay/idempotency deposuyla guvenlidir.
        workers=int(os.getenv("API_WORKERS", "1")),
    )
