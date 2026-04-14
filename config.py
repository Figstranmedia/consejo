"""
CONSEJO — Gestión de configuración
Lee/escribe config.json y expone settings como objeto accesible.
"""

import json
from pathlib import Path

APP_DIR    = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
SESSIONS_DIR = APP_DIR / "sessions"
PANELS_DIR   = APP_DIR / "panels"
KNOWLEDGE_DIR = APP_DIR / "knowledge"

# Crear dirs si no existen
for _d in [SESSIONS_DIR, PANELS_DIR, KNOWLEDGE_DIR]:
    _d.mkdir(exist_ok=True)


def load() -> dict:
    """Carga config.json y devuelve el dict completo."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def save(cfg: dict) -> None:
    """Escribe el dict completo en config.json."""
    CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get(key_path: str, default=None):
    """
    Lee un valor usando dot-notation.
    Ej: get("models.default") → "gemma4:26b"
    """
    cfg = load()
    keys = key_path.split(".")
    for k in keys:
        if not isinstance(cfg, dict):
            return default
        cfg = cfg.get(k, default)
    return cfg


def set_value(key_path: str, value) -> None:
    """
    Escribe un valor usando dot-notation.
    Ej: set_value("obsidian.vault_path", "/Users/x/vault")
    """
    cfg = load()
    keys = key_path.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    save(cfg)


def get_recent_sessions(n: int = 10) -> list[dict]:
    """Devuelve las n sesiones más recientes ordenadas por fecha."""
    files = sorted(
        SESSIONS_DIR.glob("*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )
    result = []
    for f in files[:n]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append({
                "path": str(f),
                "name": f.stem,
                "topic": data.get("topic", "Sin título"),
                "date": data.get("date", ""),
                "panel": data.get("panel", ""),
                "consensus": data.get("consensus_reached", False),
            })
        except Exception:
            continue
    return result
