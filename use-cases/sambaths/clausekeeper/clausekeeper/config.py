import os

DEFAULT_BASE_URL = "https://api.superdocs.app"
OPS_SAFETY_FLOOR = int(os.environ.get("CK_OPS_FLOOR", "50"))
MAX_TURNS_PER_RUN = int(os.environ.get("CK_MAX_TURNS", "12"))
POLL_INTERVAL_S = float(os.environ.get("CK_POLL_INTERVAL", "2"))

LATENCY = {
    "single_section_edit": {"warn_after": 10, "max_wait": 120},
    "complex_edit": {"warn_after": 120, "max_wait": 900},
    "verification_turn": {"warn_after": 30, "max_wait": 300},
}


def api_key() -> str:
    return os.environ.get("SUPERDOCS_API_KEY", "")


def base_url() -> str:
    return os.environ.get("SUPERDOCS_BASE_URL", DEFAULT_BASE_URL)
