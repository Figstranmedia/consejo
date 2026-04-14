"""
CONSEJO — Motor de deliberación multi-agente
FastAPI + WebSocket + Ollama

Soporta paneles configurables, múltiples modos de deliberación,
sesiones persistentes e integración con Obsidian.
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import ollama
import httpx
from duckduckgo_search import DDGS
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import obsidian

# ─── Dependencias opcionales ──────────────────────────────────────────────────
try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import docx as _docx_module
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

# ─── Directorios ──────────────────────────────────────────────────────────────

APP_DIR      = Path(__file__).parent
UI_FILE      = APP_DIR / "ui" / "index.html"
SESSIONS_DIR = APP_DIR / "sessions"
PANELS_DIR   = APP_DIR / "panels"
PORT         = int(os.getenv("CONSEJO_PORT", "8766"))
SESSIONS_DIR.mkdir(exist_ok=True)
PANELS_DIR.mkdir(exist_ok=True)

# ─── Documentos de contexto ───────────────────────────────────────────────────

def load_documents_from_folder(docs_path: str, max_chars: int = 4000) -> list:
    """Lee PDFs, TXTs y MDs de una carpeta para inyectar como contexto."""
    p = Path(docs_path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        return []

    docs = []
    supported = {'.pdf', '.txt', '.md', '.markdown', '.rst', '.docx'}

    for f in sorted(p.iterdir()):
        if not f.is_file() or f.suffix.lower() not in supported:
            continue
        text = ''
        try:
            if f.suffix.lower() in ('.txt', '.md', '.markdown', '.rst'):
                text = f.read_text(encoding='utf-8', errors='ignore')
            elif f.suffix.lower() == '.pdf' and HAS_PYPDF:
                reader = PdfReader(str(f))
                text = '\n'.join(
                    (page.extract_text() or '') for page in reader.pages[:20]
                )
            elif f.suffix.lower() == '.docx' and HAS_DOCX:
                doc = _docx_module.Document(str(f))
                text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            pass

        text = text.strip()
        if text:
            docs.append({
                'name': f.name,
                'size': f.stat().st_size,
                'content': text[:max_chars],
                'truncated': len(text) > max_chars,
            })
        if len(docs) >= 15:
            break

    return docs


async def fetch_url_content(url: str, max_chars: int = 2500) -> str:
    """Descarga y extrae texto de una URL para usar como cita."""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers={'User-Agent': 'Mozilla/5.0 (CONSEJO/1.0)'})
            html = r.text

        if HAS_BS4:
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'aside', 'header']):
                tag.decompose()
            # Prefer article/main content
            main = soup.find('article') or soup.find('main') or soup.body
            if main:
                lines = [l.strip() for l in main.get_text('\n', strip=True).splitlines() if l.strip()]
                text = '\n'.join(lines)
            else:
                text = soup.get_text('\n', strip=True)
        else:
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text).strip()

        return text[:max_chars]
    except Exception as e:
        return f"[Error al cargar {url}: {e}]"


# ─── Modos de deliberación ────────────────────────────────────────────────────

MODES = {
    "debate":     "Los agentes debaten tensando posiciones hasta alcanzar consenso.",
    "oracle":     "Cada agente responde independientemente desde su perspectiva.",
    "review":     "Los agentes critican un texto o código proporcionado.",
    "synthesis":  "Los agentes destilan documentos en un resumen estructurado.",
    "brainstorm": "Los agentes generan ideas libremente sin debate.",
}

# ─── Mediador ─────────────────────────────────────────────────────────────────

MEDIATOR_SYSTEM = """\
Eres un mediador que estructura conocimiento emergente de deliberaciones multi-agente.
Responde ÚNICAMENTE con JSON válido, sin texto adicional, sin markdown:
{
  "nodes": [
    {"id": "string", "label": "frase corta max 6 palabras", "agent": "id_agente|consenso", "type": "claim|problem|fact|solution|question"}
  ],
  "edges": [
    {"from": "id_origen", "to": "id_destino", "label": "apoya|contradice|refina|conecta|resuelve"}
  ],
  "consensus": {
    "reached": false,
    "conclusion": "resumen preciso si hay acuerdo real",
    "confidence": 0.0
  },
  "needs_more": true,
  "needs_more_reason": "por qué se necesitan o no más rondas"
}
Reglas:
- Solo nodos genuinamente nuevos (no repetir IDs previos).
- El consenso requiere acuerdo específico y explícito de todos los agentes.
- confidence entre 0.0 y 1.0 según solidez del acuerdo.
- Si no hay consenso claro, reached=false y confidence < 0.5.
- needs_more=true si el debate tiene tensiones sin resolver que merecen otra ronda.
- needs_more=false si las posiciones se han estabilizado o el tema está agotado.
"""

AUTO_AGENTS_SYSTEM = """\
Eres un arquitecto de debates intelectuales.
Dado un tema, genera exactamente 3 agentes especializados que tendrían el debate más rico y productivo sobre ese tema.
Cada agente debe tener una perspectiva genuinamente distinta que cree tensión intelectual con los otros.
Responde ÚNICAMENTE con JSON válido:
{
  "panel_name": "nombre descriptivo del panel",
  "panel_description": "qué tipo de debate generará este panel",
  "agents": [
    {
      "id": "id_sin_espacios",
      "name": "Nombre del agente",
      "emoji": "un emoji representativo",
      "color": "#hexcolor",
      "specialty": "una frase que describe su especialidad",
      "system": "prompt del sistema completo: quién es, cómo analiza, qué busca, cómo responde. Incluye: Para buscar información escribe: [BUSCAR: consulta en inglés]. Responde en español. Máximo 160 palabras por turno."
    }
  ]
}
Colores sugeridos: #e74c3c, #3498db, #2ecc71, #9b59b6, #e67e22, #1abc9c
"""

# ─── Búsqueda web ─────────────────────────────────────────────────────────────

def do_search(query: str) -> list[dict]:
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=3))
    except Exception as exc:
        return [{"title": "Error", "body": str(exc), "href": ""}]

# ─── Detección de modelos Ollama ──────────────────────────────────────────────

def list_ollama_models() -> list[str]:
    try:
        result = ollama.list()
        return [m["name"] for m in result.get("models", [])]
    except Exception:
        return []

# ─── Turno de agente ──────────────────────────────────────────────────────────

async def run_agent_turn(
    agent: dict,
    history: list[dict],
    ws: WebSocket,
    model: str,
    context_header: str = "",
) -> str:
    await ws.send_json({"type": "agent_start", "agent": agent["id"], "name": agent["name"]})

    # Modelo por agente tiene prioridad sobre el modelo global
    agent_model = agent.get("model") or model

    system_prompt = agent["system"]
    if context_header:
        system_prompt += "\n\n" + context_header

    messages = [{"role": "system", "content": system_prompt}, *history]

    def call_ollama():
        return ollama.chat(model=agent_model, messages=messages)

    result = await asyncio.to_thread(call_ollama)
    text: str = result["message"]["content"]

    # Extraer bloque de pensamiento si existe (<think>…</think> o <thinking>…</thinking>)
    think_match = re.search(r'<think(?:ing)?>(.*?)</think(?:ing)?>', text, re.DOTALL | re.IGNORECASE)
    if think_match:
        thinking_text = think_match.group(1).strip()
        text = (text[:think_match.start()] + text[think_match.end():]).strip()
        await ws.send_json({"type": "agent_thinking", "agent": agent["id"], "thinking": thinking_text})

    # Búsquedas web opcionales [BUSCAR: query]
    search_pattern = re.compile(r"\[BUSCAR:\s*([^\]]+)\]", re.IGNORECASE)
    queries = search_pattern.findall(text)

    # Citas de URL [CITAR: https://...]
    cite_pattern = re.compile(r"\[CITAR:\s*(https?://[^\]]+)\]", re.IGNORECASE)
    cite_urls = cite_pattern.findall(text)

    extra_context = []

    if queries:
        for query in queries[:2]:
            query = query.strip()
            await ws.send_json({"type": "search", "agent": agent["id"], "query": query})
            results = await asyncio.to_thread(do_search, query)
            await ws.send_json({
                "type": "search_result",
                "agent": agent["id"],
                "query": query,
                "results": [{"title": r.get("title", ""), "snippet": r.get("body", "")[:200]}
                            for r in results[:3]],
            })
            snippets = "; ".join(r.get("body", "")[:180] for r in results[:2])
            extra_context.append(f'[Búsqueda "{query}"]: {snippets}')

    if cite_urls:
        for url in cite_urls[:3]:
            url = url.strip()
            await ws.send_json({"type": "citation_loading", "agent": agent["id"], "url": url})
            content = await fetch_url_content(url)
            domain = re.sub(r'^https?://(www\.)?', '', url).split('/')[0]
            await ws.send_json({
                "type": "citation",
                "agent": agent["id"],
                "url": url,
                "domain": domain,
                "snippet": content[:300],
            })
            extra_context.append(f'[Cita de {url}]:\n{content}')

    if extra_context:
        cont_messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
             "Información de fuentes:\n" + "\n\n".join(extra_context) +
             "\n\nContinúa y completa tu análisis integrando esta información con citas explícitas."},
        ]

        def call_cont():
            return ollama.chat(model=agent_model, messages=cont_messages)

        result2 = await asyncio.to_thread(call_cont)
        text = result2["message"]["content"]

    # Streaming palabra a palabra
    words = text.split(" ")
    for i, word in enumerate(words):
        chunk = word + (" " if i < len(words) - 1 else "")
        await ws.send_json({"type": "chunk", "agent": agent["id"], "text": chunk})
        await asyncio.sleep(0.012)

    await ws.send_json({"type": "agent_done", "agent": agent["id"]})
    return text

# ─── Mediador ─────────────────────────────────────────────────────────────────

async def run_mediator(
    round_num: int,
    responses: dict,
    agents: list[dict],
    prev_ids: list[str],
    model: str,
    max_rounds: int = 8,
) -> dict:
    agent_blocks = "\n\n".join(
        f"{a['name'].upper()}:\n{responses.get(a['id'], '')[:700]}"
        for a in agents
    )
    prompt = (
        f"Ronda {round_num} de máximo {max_rounds}.\n\n{agent_blocks}\n\n"
        f"IDs ya usados (no repetir): {prev_ids}\n"
        f"Usa IDs con formato: r{round_num}_1, r{round_num}_2, etc.\n"
        f"Evalúa honestamente si needs_more=true (hay tensiones sin resolver) o false (tema agotado/estabilizado)."
    )

    def call():
        return ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": MEDIATOR_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            format="json",
        )

    result = await asyncio.to_thread(call)
    raw = re.sub(r"```json\s*|\s*```", "", result["message"]["content"]).strip()

    try:
        data = json.loads(raw)
        # Asegurar campos necesarios
        if "needs_more" not in data:
            data["needs_more"] = True
        return data
    except Exception:
        return {
            "nodes": [], "edges": [],
            "consensus": {"reached": False, "conclusion": "", "confidence": 0.0},
            "needs_more": True,
        }


async def auto_generate_agents(topic: str, model: str) -> dict:
    """Genera automáticamente un panel de agentes apropiados para el tema."""
    def call():
        return ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": AUTO_AGENTS_SYSTEM},
                {"role": "user", "content": f"Tema del debate:\n{topic}"},
            ],
            format="json",
        )

    result = await asyncio.to_thread(call)
    raw = re.sub(r"```json\s*|\s*```", "", result["message"]["content"]).strip()

    try:
        return json.loads(raw)
    except Exception:
        return {"error": "No se pudo generar el panel automáticamente."}

# ─── Modos de deliberación ────────────────────────────────────────────────────

async def mode_debate(ws: WebSocket, topic: str, agents: list[dict], cfg: dict) -> dict:
    """Debate con tensión hacia consenso."""
    model     = cfg["model"]
    mediator  = cfg["mediator_model"]
    max_rounds = cfg.get("max_rounds", 8)
    threshold  = cfg.get("consensus_threshold", 0.75)
    context    = cfg.get("context_header", "")

    all_nodes, all_edges = [], []
    histories     = {a["id"]: [] for a in agents}
    all_responses = {a["id"]: {} for a in agents}
    rounds_log    = []

    await ws.send_json({"type": "debate_start", "topic": topic, "model": model})

    for round_num in range(1, max_rounds + 1):
        await ws.send_json({"type": "round_start", "round": round_num, "max": max_rounds})
        round_responses = {}

        for agent in agents:
            if round_num == 1:
                user_msg = (
                    f"Debate iniciado. Tema:\n{topic}\n\n"
                    "Presenta tu perspectiva inicial. "
                    "Sé directo sobre lo que ves sólido y lo que ves problemático."
                )
            else:
                other_views = "\n\n".join(
                    f"**{a['name']}**: {all_responses[a['id']].get(round_num - 1, '')[:350]}"
                    for a in agents if a["id"] != agent["id"]
                )
                user_msg = (
                    f"Ronda {round_num}. Respuestas anteriores:\n\n{other_views}\n\n"
                    "Responde, refina o desafía. Avanza hacia una posición más precisa."
                )

            histories[agent["id"]].append({"role": "user", "content": user_msg})
            response = await run_agent_turn(agent, histories[agent["id"]], ws, model, context)
            histories[agent["id"]].append({"role": "assistant", "content": response})
            round_responses[agent["id"]] = response
            all_responses[agent["id"]][round_num] = response

        await ws.send_json({"type": "mediator_thinking"})
        prev_ids = [n["id"] for n in all_nodes]
        analysis = await run_mediator(round_num, round_responses, agents, prev_ids, mediator, max_rounds)

        new_nodes = analysis.get("nodes", [])
        new_edges = analysis.get("edges", [])
        all_nodes.extend(new_nodes)
        all_edges.extend(new_edges)

        rounds_log.append({
            "round": round_num,
            "responses": round_responses,
            "nodes": new_nodes,
            "edges": new_edges,
        })

        await ws.send_json({
            "type": "graph_update",
            "new_nodes": new_nodes,
            "new_edges": new_edges,
            "round": round_num,
        })

        consensus = analysis.get("consensus", {})
        needs_more = analysis.get("needs_more", True)
        needs_more_reason = analysis.get("needs_more_reason", "")

        # Consenso alcanzado
        if consensus.get("reached") and consensus.get("confidence", 0) >= threshold:
            await ws.send_json({
                "type": "consensus_reached",
                "conclusion": consensus["conclusion"],
                "confidence": consensus["confidence"],
                "round": round_num,
                "all_nodes": all_nodes,
                "all_edges": all_edges,
            })
            return {
                "rounds": rounds_log,
                "consensus": consensus,
                "all_nodes": all_nodes,
                "all_edges": all_edges,
            }

        # La IA indica que no se necesitan más rondas (mínimo 2 rondas siempre)
        min_rounds = cfg.get("min_rounds", 2)
        if round_num >= min_rounds and not needs_more:
            await ws.send_json({
                "type": "debate_end",
                "rounds": round_num,
                "consensus": False,
                "early_stop": True,
                "message": f"El debate alcanzó madurez en {round_num} rondas. {needs_more_reason}",
            })
            return {
                "rounds": rounds_log,
                "consensus": None,
                "all_nodes": all_nodes,
                "all_edges": all_edges,
            }

        # Notificar al frontend si hay más rondas
        if round_num < max_rounds:
            await ws.send_json({
                "type": "round_assessment",
                "needs_more": needs_more,
                "reason": needs_more_reason,
                "rounds_done": round_num,
                "max_rounds": max_rounds,
            })

    await ws.send_json({
        "type": "debate_end",
        "rounds": max_rounds,
        "consensus": False,
        "message": f"Se completaron {max_rounds} rondas sin consenso formal.",
    })
    return {"rounds": rounds_log, "consensus": None, "all_nodes": all_nodes, "all_edges": all_edges}


async def mode_oracle(ws: WebSocket, topic: str, agents: list[dict], cfg: dict) -> dict:
    """Cada agente responde independientemente — sin debate."""
    model   = cfg["model"]
    context = cfg.get("context_header", "")

    await ws.send_json({"type": "debate_start", "topic": topic, "model": model, "mode": "oracle"})
    responses = {}

    for agent in agents:
        history = [{"role": "user", "content": f"Pregunta:\n{topic}\n\nResponde desde tu perspectiva experta."}]
        text = await run_agent_turn(agent, history, ws, model, context)
        responses[agent["id"]] = text

    await ws.send_json({"type": "debate_end", "rounds": 1, "consensus": False, "mode": "oracle"})
    return {"rounds": [{"round": 1, "responses": responses}], "consensus": None}


async def mode_review(ws: WebSocket, topic: str, agents: list[dict], cfg: dict) -> dict:
    """Los agentes critican el contenido proporcionado en el tema."""
    model   = cfg["model"]
    context = cfg.get("context_header", "")

    await ws.send_json({"type": "debate_start", "topic": topic, "model": model, "mode": "review"})
    responses = {}

    for agent in agents:
        history = [{
            "role": "user",
            "content": (
                f"Revisa y critica el siguiente contenido desde tu perspectiva experta.\n"
                f"Identifica fortalezas, debilidades y mejoras concretas.\n\n{topic}"
            )
        }]
        text = await run_agent_turn(agent, history, ws, model, context)
        responses[agent["id"]] = text

    await ws.send_json({"type": "debate_end", "rounds": 1, "consensus": False, "mode": "review"})
    return {"rounds": [{"round": 1, "responses": responses}], "consensus": None}


async def mode_brainstorm(ws: WebSocket, topic: str, agents: list[dict], cfg: dict) -> dict:
    """Generación libre de ideas sin debate."""
    model   = cfg["model"]
    context = cfg.get("context_header", "")

    await ws.send_json({"type": "debate_start", "topic": topic, "model": model, "mode": "brainstorm"})
    responses = {}

    for agent in agents:
        history = [{
            "role": "user",
            "content": (
                f"Genera ideas creativas y concretas sobre:\n{topic}\n\n"
                "No debatas — simplemente genera desde tu perspectiva. "
                "Lista tus mejores ideas con explicación breve de cada una."
            )
        }]
        text = await run_agent_turn(agent, history, ws, model, context)
        responses[agent["id"]] = text

    await ws.send_json({"type": "debate_end", "rounds": 1, "consensus": False, "mode": "brainstorm"})
    return {"rounds": [{"round": 1, "responses": responses}], "consensus": None}


MODE_RUNNERS = {
    "debate":     mode_debate,
    "oracle":     mode_oracle,
    "review":     mode_review,
    "brainstorm": mode_brainstorm,
    "synthesis":  mode_review,  # synthesis usa la misma lógica que review
}

# ─── Sesiones ─────────────────────────────────────────────────────────────────

def save_session(session: dict) -> Path:
    sid = session.get("id", str(uuid.uuid4())[:8])
    filename = f"{session.get('date', datetime.now().strftime('%Y%m%d_%H%M%S'))}_{sid}.json"
    path = SESSIONS_DIR / filename
    path.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

# ─── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(title="CONSEJO")

# Servir assets estáticos (logo, fuentes, etc.)
ASSETS_DIR  = APP_DIR / "ui" / "assets"
EXPORTS_DIR = APP_DIR / "exports"
ASSETS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)
app.mount("/assets",  StaticFiles(directory=str(ASSETS_DIR)),  name="assets")
app.mount("/exports", StaticFiles(directory=str(EXPORTS_DIR)), name="exports")


@app.get("/")
async def serve_index():
    return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))


@app.get("/api/models")
async def api_models():
    return {"models": list_ollama_models()}


@app.get("/api/config")
async def api_config():
    return config.load()


@app.post("/api/config")
async def api_set_config(data: dict):
    cfg = config.load()
    # Merge recursivo de primer nivel
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    config.save(cfg)
    return {"status": "ok"}


@app.get("/api/panels")
async def api_panels():
    panels = []
    for f in sorted(PANELS_DIR.glob("*.json")):
        try:
            panels.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"panels": panels}


@app.post("/api/panels")
async def api_save_panel(panel: dict):
    name = panel.get("name", "panel")
    slug = re.sub(r"[^\w-]", "_", name.lower())
    path = PANELS_DIR / f"{slug}.json"
    path.write_text(json.dumps(panel, indent=2, ensure_ascii=False), encoding="utf-8")
    # Exportar al vault si está configurado
    if config.get("obsidian.vault_path"):
        obsidian.write_panel(panel)
    return {"status": "ok", "path": str(path)}


@app.delete("/api/panels/{slug}")
async def api_delete_panel(slug: str):
    path = PANELS_DIR / f"{slug}.json"
    if path.exists():
        path.unlink()
        return {"status": "ok"}
    return {"status": "not_found"}


@app.get("/api/sessions")
async def api_sessions():
    return {"sessions": config.get_recent_sessions(20)}


@app.get("/api/sessions/{filename}")
async def api_session(filename: str):
    path = SESSIONS_DIR / filename
    if not path.exists():
        return {"error": "not_found"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/auto-agents")
async def api_auto_agents(data: dict):
    topic = data.get("topic", "").strip()
    if not topic:
        return {"error": "Tema vacío"}
    model = data.get("model") or config.get("models.default", "gemma4:26b")
    return await auto_generate_agents(topic, model)


@app.get("/api/docs")
async def api_docs():
    """Lista los documentos disponibles en la carpeta configurada."""
    docs_path = config.get("docs.path", "")
    if not docs_path:
        return {"configured": False, "docs": []}
    p = Path(docs_path).expanduser().resolve()
    if not p.exists():
        return {"configured": True, "exists": False, "docs": []}
    docs = load_documents_from_folder(docs_path)
    return {
        "configured": True,
        "exists": True,
        "path": str(p),
        "docs": [{"name": d["name"], "size": d["size"], "truncated": d["truncated"]} for d in docs],
    }


@app.post("/api/export-html")
async def api_export_html(data: dict):
    """Guarda HTML en exports/ y devuelve la URL para abrir en el navegador."""
    html_content = data.get("html", "")
    if not html_content:
        return {"error": "Contenido vacío"}
    filename = f"consejo_export_{uuid.uuid4().hex[:8]}.html"
    path = EXPORTS_DIR / filename
    path.write_text(html_content, encoding="utf-8")
    # Eliminar exports viejos (mantener solo los últimos 10)
    exports = sorted(EXPORTS_DIR.glob("consejo_export_*.html"), key=lambda f: f.stat().st_mtime)
    for old in exports[:-10]:
        try: old.unlink()
        except Exception: pass
    return {"url": f"http://127.0.0.1:{PORT}/exports/{filename}"}


@app.get("/api/obsidian/status")
async def api_obsidian_status():
    vault = config.get("obsidian.vault_path", "")
    if not vault:
        return {"configured": False}
    p = Path(vault).expanduser().resolve()
    return {"configured": True, "exists": p.exists(), "path": str(p)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        data = await ws.receive_json()

        topic      = data.get("topic", "")
        mode       = data.get("mode", "debate")
        panel_data = data.get("panel", {})
        agents     = panel_data.get("agents", [])
        panel_name = panel_data.get("name", "Panel")

        if not agents:
            await ws.send_json({"type": "error", "message": "No hay agentes en el panel."})
            return
        if not topic:
            await ws.send_json({"type": "error", "message": "El tema está vacío."})
            return

        cfg_data = config.load()
        model          = data.get("model") or cfg_data.get("models", {}).get("default", "gemma4:26b")
        mediator_model = cfg_data.get("models", {}).get("mediator", model)
        max_rounds     = cfg_data.get("debate", {}).get("max_rounds", 8)
        threshold      = cfg_data.get("debate", {}).get("consensus_threshold", 0.75)

        # ── Cargar documentos de contexto ──────────────────────────────
        docs_path = cfg_data.get("docs", {}).get("path", "")
        context_header = ""
        if docs_path:
            docs = load_documents_from_folder(docs_path)
            if docs:
                doc_names = [d["name"] for d in docs]
                await ws.send_json({
                    "type": "docs_loaded",
                    "count": len(docs),
                    "names": doc_names,
                })
                doc_blocks = "\n\n---\n\n".join(
                    f"📄 {d['name']}\n{d['content']}" for d in docs
                )
                context_header = (
                    "=== DOCUMENTOS DE CONTEXTO PARA ESTE DEBATE ===\n"
                    "Tienes acceso a los siguientes documentos. Cítalos cuando sean relevantes usando [CITAR: nombre_del_doc].\n\n"
                    + doc_blocks
                    + "\n\n=== FIN DE DOCUMENTOS ==="
                )

        run_cfg = {
            "model": model,
            "mediator_model": mediator_model,
            "max_rounds": max_rounds,
            "min_rounds": cfg_data.get("debate", {}).get("min_rounds", 2),
            "consensus_threshold": threshold,
            "context_header": context_header,
        }

        runner = MODE_RUNNERS.get(mode, mode_debate)
        result = await runner(ws, topic, agents, run_cfg)

        # Guardar sesión localmente
        session_id = str(uuid.uuid4())[:8]
        session = {
            "id": session_id,
            "topic": topic,
            "mode": mode,
            "panel": panel_name,
            "panel_agents": agents,
            "model": model,
            "date": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "consensus_reached": bool(result.get("consensus") and result["consensus"].get("reached")),
            "consensus": result.get("consensus"),
            "rounds": result.get("rounds", []),
            "all_nodes": result.get("all_nodes", []),
            "all_edges": result.get("all_edges", []),
        }
        session_path = save_session(session)

        # Exportar a Obsidian si está configurado
        obsidian_path = None
        if config.get("obsidian.vault_path") and config.get("obsidian.auto_export", True):
            obsidian_path = obsidian.write_debate(
                topic=topic,
                panel_name=panel_name,
                agents=agents,
                rounds=result.get("rounds", []),
                consensus=result.get("consensus"),
                model=model,
                session_id=session_id,
            )

        await ws.send_json({
            "type": "session_saved",
            "session_id": session_id,
            "session_file": session_path.name,
            "obsidian_path": obsidian_path,
        })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
