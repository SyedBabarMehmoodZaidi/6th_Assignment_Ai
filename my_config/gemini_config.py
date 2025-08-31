"""
gemini_config.py

Simple config helper for Gemini key and mode.
If you later decide to integrate real Gemini calls, use GEMINI_API_KEY and MODE here.
"""

import os

def load_config(env_path=None):
    # Basic load from environment variables (we don't require python-dotenv).
    # If you prefer, install python-dotenv and load .env before importing.
    return {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", "").strip(),
        "MODE": os.getenv("MODE", "offline").strip().lower(),
        "CUSTOMER_ID": os.getenv("CUSTOMER_ID", "").strip(),
    }

CONFIG = load_config()
