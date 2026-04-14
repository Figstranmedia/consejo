"""
CONSEJO — Integración con Obsidian
Escribe debates y consensos directamente al vault como notas markdown
con frontmatter, tags y wikilinks entre sesiones relacionadas.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import config


def _vault_root() -> Optional[Path]:
    """Devuelve la ruta al subfolder de CONSEJO dentro del vault, o None si no está configurado."""
    vault = config.get("obsidian.vault_path", "").strip()
    if not vault:
        return None
    root = Path(vault).expanduser().resolve()
    if not root.exists():
        return None
    subfolder = config.get("obsidian.subfolder", "CONSEJO")
    consejo_dir = root / subfolder
    consejo_dir.mkdir(exist_ok=True)
    (consejo_dir / "debates").mkdir(exist_ok=True)
    (consejo_dir / "consensos").mkdir(exist_ok=True)
    (consejo_dir / "paneles").mkdir(exist_ok=True)
    return consejo_dir


def _slugify(text: str) -> str:
    """Convierte texto a slug válido para nombre de archivo."""
    text = text.lower().strip()[:60]
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def _find_related(topic: str, panel: str, limit: int = 3) -> list[str]:
    """
    Busca notas previas en el vault relacionadas por panel o palabras del tema.
    Devuelve lista de nombres de archivo (sin extensión) para wikilinks.
    """
    root = _vault_root()
    if not root:
        return []

    words = set(w.lower() for w in topic.split() if len(w) > 4)
    related = []

    for note in sorted((root / "debates").glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
        content = note.read_text(encoding="utf-8")
        # Match por panel
        if f"panel: {panel}" in content.lower():
            related.append(note.stem)
            continue
        # Match por palabras clave del tema
        if any(w in content.lower() for w in words):
            related.append(note.stem)
        if len(related) >= limit:
            break

    return related


def write_debate(
    topic: str,
    panel_name: str,
    agents: list,
    rounds: list,
    consensus: Optional[dict],
    model: str,
    session_id: str,
) -> Optional[str]:
    """
    Escribe la sesión completa de debate al vault de Obsidian.
    Devuelve la ruta del archivo creado, o None si el vault no está configurado.

    rounds: lista de {"round": int, "responses": {"agent_id": "texto"}, "nodes": [...], "edges": [...]}
    consensus: {"reached": bool, "conclusion": str, "confidence": float} | None
    """
    root = _vault_root()
    if not root:
        return None

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    slug = _slugify(topic)
    filename = f"{date_str}_{slug}.md"
    path = root / "debates" / filename

    # Evitar colisión de nombres
    counter = 1
    while path.exists():
        path = root / "debates" / f"{date_str}_{slug}_{counter}.md"
        counter += 1

    agent_names = [a["name"] for a in agents]
    tags = ["consejo", "debate", _slugify(panel_name)]
    if consensus and consensus.get("reached"):
        tags.append("consenso")

    related = _find_related(topic, panel_name)
    related_links = "\n".join(f"- [[{r}]]" for r in related) if related else "_ninguno_"

    # Frontmatter
    frontmatter_tags = "\n".join(f"  - {t}" for t in tags)
    frontmatter = f"""---
title: "{topic[:80]}"
date: {date_str}
time: {time_str}
panel: "{panel_name}"
agents: [{", ".join(agent_names)}]
model: {model}
rounds: {len(rounds)}
consensus: {"true" if consensus and consensus.get("reached") else "false"}
confidence: {consensus.get("confidence", 0.0) if consensus else 0.0}
session_id: {session_id}
tags:
{frontmatter_tags}
---"""

    # Resumen ejecutivo
    consensus_block = ""
    if consensus and consensus.get("reached"):
        conf_pct = int(consensus.get("confidence", 0) * 100)
        consensus_block = f"""
## Consenso Alcanzado

> {consensus.get("conclusion", "")}

**Confianza:** {conf_pct}%
"""
    else:
        consensus_block = "\n## Sin Consenso Formal\n\nEl debate terminó sin acuerdo explícito entre los agentes. Ver posiciones finales.\n"

    # Rondas
    rounds_md = ""
    for r in rounds:
        rounds_md += f"\n### Ronda {r['round']}\n\n"
        for agent in agents:
            aid = agent["id"]
            text = r.get("responses", {}).get(aid, "")
            if text:
                rounds_md += f"**{agent['name']}** ({agent.get('emoji','·')})\n\n{text[:500]}{'…' if len(text) > 500 else ''}\n\n"

    # Documento completo
    content = f"""{frontmatter}

# {topic}

**Panel:** {panel_name} · **Modelo:** {model} · **Fecha:** {date_str} {time_str}
{consensus_block}

## Agentes

{chr(10).join(f"- **{a['name']}** — {a.get('system','')[:100]}…" for a in agents)}

## Notas relacionadas

{related_links}

---

## Transcripción por Rondas
{rounds_md}

---
*Generado por CONSEJO — Sistema de Deliberación Multi-Agente*
"""

    path.write_text(content, encoding="utf-8")

    # Si hay consenso, también escribir nota de consenso separada con link
    if consensus and consensus.get("reached"):
        _write_consensus_note(
            topic=topic,
            panel_name=panel_name,
            conclusion=consensus.get("conclusion", ""),
            confidence=consensus.get("confidence", 0.0),
            debate_note=path.stem,
            date_str=date_str,
            time_str=time_str,
            model=model,
            root=root,
        )

    return str(path)


def _write_consensus_note(
    topic: str,
    panel_name: str,
    conclusion: str,
    confidence: float,
    debate_note: str,
    date_str: str,
    time_str: str,
    model: str,
    root: Path,
) -> None:
    """Escribe nota de consenso separada en consensos/ con link al debate."""
    slug = _slugify(topic)
    filename = f"{date_str}_consenso_{slug}.md"
    path = root / "consensos" / filename

    conf_pct = int(confidence * 100)
    content = f"""---
title: "Consenso: {topic[:60]}"
date: {date_str}
time: {time_str}
panel: "{panel_name}"
model: {model}
confidence: {confidence}
type: consenso
tags:
  - consejo
  - consenso
  - {_slugify(panel_name)}
---

# Consenso: {topic}

> {conclusion}

**Confianza:** {conf_pct}% · **Panel:** {panel_name} · **Fecha:** {date_str}

## Debate de origen

[[{debate_note}]]

---
*Generado por CONSEJO*
"""
    path.write_text(content, encoding="utf-8")


def write_panel(panel: dict) -> Optional[str]:
    """
    Escribe la definición de un panel de agentes al vault.
    Útil para documentar qué hace cada configuración.
    """
    root = _vault_root()
    if not root:
        return None

    name = panel.get("name", "Panel")
    slug = _slugify(name)
    path = root / "paneles" / f"{slug}.md"

    agents_md = ""
    for a in panel.get("agents", []):
        agents_md += f"\n### {a['name']} {a.get('emoji', '')}\n\n{a.get('system', '')}\n"

    content = f"""---
title: "{name}"
type: panel
tags:
  - consejo
  - panel
---

# {name}

{panel.get("description", "")}

## Agentes
{agents_md}

---
*Panel de CONSEJO*
"""
    path.write_text(content, encoding="utf-8")
    return str(path)
