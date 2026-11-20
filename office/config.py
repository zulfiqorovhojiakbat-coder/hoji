"""Settings: LLM provider, key and models. Env vars win over data/config.json."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("OFFICE_DATA_DIR") or ROOT / "data")
CONFIG_PATH = DATA / "config.json"
WEB_DIR = ROOT / "web"

PROVIDERS = ("gemini", "openai")


def _read_file() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def load() -> dict:
    """Effective settings (includes the key; never send this to the browser)."""
    cfg = _read_file()
    provider = os.environ.get("OFFICE_PROVIDER") or cfg.get("provider") or "gemini"
    if provider not in PROVIDERS:
        provider = "gemini"
    if provider == "gemini":
        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    else:
        env_key = os.environ.get("OPENAI_API_KEY")
    models = os.environ.get("OFFICE_MODELS") or cfg.get("models") or ""
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    return {
        "provider": provider,
        "api_key": env_key or cfg.get("api_key") or "",
        "key_from_env": bool(env_key),
        "models": models,
        "base_url": os.environ.get("OPENAI_BASE_URL") or cfg.get("base_url") or "",
    }


def public() -> dict:
    cfg = load()
    key = cfg.pop("api_key")
    cfg["has_key"] = bool(key)
    cfg["key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ""
    return cfg


def save(provider: str, api_key: str | None, models: list[str], base_url: str) -> None:
    cfg = _read_file()
    cfg["provider"] = provider if provider in PROVIDERS else "gemini"
    if api_key:
        cfg["api_key"] = api_key.strip()
    cfg["models"] = models
    cfg["base_url"] = base_url.strip()
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
