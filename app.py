"""
CONSEJO — Launcher nativo macOS
Inicia el servidor y abre la ventana con barra de menú nativa.
"""

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

PORT = int(os.getenv("CONSEJO_PORT", "8766"))
HOST = "127.0.0.1"
URL  = f"http://{HOST}:{PORT}"

# ── Pantalla de carga (se muestra mientras el servidor arranca) ────────────
LOADING_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: #0c0806;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    height: 100vh; gap: 22px;
    font-family: -apple-system, sans-serif;
  }
  .ring {
    width: 56px; height: 56px;
    border: 2px solid #2e2018;
    border-top-color: #c4983a;
    border-radius: 50%;
    animation: spin 1.1s linear infinite;
  }
  p { color: #c4983a; font-size: 11px; letter-spacing: .25em; text-transform: uppercase; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body><div class="ring"></div><p>Convocando el consejo</p></body></html>"""


def _start_server() -> None:
    import uvicorn
    from server import app as fastapi_app
    uvicorn.run(fastapi_app, host=HOST, port=PORT, log_level="warning")


def _wait_server(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> None:
    try:
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pywebview"])
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator

    import config

    # ── Arrancar servidor en hilo daemon ───────────────────────────────────
    threading.Thread(target=_start_server, daemon=True).start()

    # ── API expuesta a JavaScript ──────────────────────────────────────────
    class API:
        def choose_folder(self):
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
            return result[0] if result else None

        def choose_file(self, file_types=None):
            result = window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=tuple(file_types) if file_types else ("JSON Files (*.json)",),
            )
            return result[0] if result else None

        def save_file_dialog(self, filename="session.json"):
            result = window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
            return result if result else None

        def get_config(self):
            return config.load()

        def set_config(self, data: dict):
            cfg = config.load()
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
            config.save(cfg)
            return {"status": "ok"}

        def get_recent_sessions(self):
            return config.get_recent_sessions(10)

        def open_url_in_browser(self, url: str):
            """Abre una URL en el navegador predeterminado del sistema."""
            import webbrowser
            webbrowser.open(url)
            return True

    api = API()

    # ── Callbacks del menú Archivo ─────────────────────────────────────────
    def new_session():
        window.evaluate_js("window.consejo?.newSession()")

    def open_session():
        result = window.create_file_dialog(webview.OPEN_DIALOG, file_types=("JSON Files (*.json)",))
        if result:
            safe = result[0].replace("\\", "/").replace("'", "\\'")
            window.evaluate_js(f"window.consejo?.openSession('{safe}')")

    def open_recent_factory(path: str):
        def _open():
            safe = path.replace("\\", "/").replace("'", "\\'")
            window.evaluate_js(f"window.consejo?.openSession('{safe}')")
        return _open

    def save_session():
        window.evaluate_js("window.consejo?.saveSession()")

    def save_session_as():
        window.evaluate_js("window.consejo?.saveSessionAs()")

    def show_properties():
        window.evaluate_js("window.consejo?.showProperties()")

    def show_preferences():
        window.evaluate_js("window.consejo?.showPreferences()")

    def show_permissions():
        window.evaluate_js("window.consejo?.showPermissions()")

    def close_app():
        window.destroy()

    # ── Callbacks de Vista y Edición ──────────────────────────────────────
    def edit_undo():        window.evaluate_js("document.execCommand('undo')")
    def edit_redo():        window.evaluate_js("document.execCommand('redo')")
    def edit_cut():         window.evaluate_js("document.execCommand('cut')")
    def edit_copy():        window.evaluate_js("document.execCommand('copy')")
    def edit_paste():       window.evaluate_js("document.execCommand('paste')")
    def edit_select_all():  window.evaluate_js("document.execCommand('selectAll')")
    def view_sidebar():     window.evaluate_js("window.consejo?.toggleSidebar()")
    def view_graph():       window.evaluate_js("window.consejo?.toggleGraph()")
    def view_zoom_in():     window.evaluate_js("window.consejo?.graphZoomIn()")
    def view_zoom_out():    window.evaluate_js("window.consejo?.graphZoomOut()")
    def view_zoom_reset():  window.evaluate_js("window.consejo?.graphResetView()")
    def export_pdf():       window.evaluate_js("window.consejo?.triggerExportPDF()")

    # ── Callbacks del menú Ayuda ───────────────────────────────────────────
    def show_search():
        window.evaluate_js("window.consejo?.showSearch()")

    def show_license():
        window.evaluate_js("window.consejo?.showLicense()")

    # ── Submenú Abrir reciente ─────────────────────────────────────────────
    recent = config.get_recent_sessions(8)
    recent_items = (
        [MenuAction(
            f"{s['topic'][:45]}…" if len(s['topic']) > 45 else s['topic'],
            open_recent_factory(s["path"]),
        ) for s in recent]
        if recent else
        [MenuAction("(Sin sesiones recientes)", lambda: None)]
    )

    # ── Menú nativo — Archivo / Edición / Vista / Ayuda ───────────────────
    menu = [
        Menu("Archivo", [
            MenuAction("Nueva sesión",        new_session),
            MenuSeparator(),
            MenuAction("Abrir…",              open_session),
            Menu("Abrir reciente",            recent_items),
            MenuSeparator(),
            MenuAction("Guardar",             save_session),
            MenuAction("Guardar como…",       save_session_as),
            MenuAction("Exportar PDF…",       export_pdf),
            MenuSeparator(),
            MenuAction("Propiedades",         show_properties),
            MenuAction("Preferencias…",       show_preferences),
            MenuAction("Permisos",            show_permissions),
            MenuSeparator(),
            MenuAction("Cerrar",              close_app),
        ]),
        Menu("Edición", [
            MenuAction("Deshacer",            edit_undo),
            MenuAction("Rehacer",             edit_redo),
            MenuSeparator(),
            MenuAction("Cortar",              edit_cut),
            MenuAction("Copiar",              edit_copy),
            MenuAction("Pegar",               edit_paste),
            MenuSeparator(),
            MenuAction("Seleccionar todo",    edit_select_all),
        ]),
        Menu("Vista", [
            MenuAction("Barra lateral",       view_sidebar),
            MenuAction("Red de conocimiento", view_graph),
            MenuSeparator(),
            MenuAction("Acercar grafo",       view_zoom_in),
            MenuAction("Alejar grafo",        view_zoom_out),
            MenuAction("Restablecer vista",   view_zoom_reset),
        ]),
        Menu("Ayuda", [
            MenuAction("Buscar sesión…",      show_search),
            MenuSeparator(),
            MenuAction("Licencia",            show_license),
        ]),
    ]

    # ── Ventana — inicia con pantalla de carga ─────────────────────────────
    cfg = config.load()
    ui_cfg = cfg.get("ui", {})

    window = webview.create_window(
        title            = "CONSEJO — Deliberación Multi-Agente",
        html             = LOADING_HTML,       # mostrar inmediatamente
        width            = ui_cfg.get("window_width",  1440),
        height           = ui_cfg.get("window_height", 900),
        min_size         = (1000, 680),
        background_color = "#0c0806",
        text_select      = False,
        zoomable         = False,
        on_top           = False,
        js_api           = api,
    )

    # ── Navegar a la app cuando el servidor esté listo ─────────────────────
    def navigate_when_ready():
        if _wait_server():
            window.load_url(URL)
        else:
            window.evaluate_js(
                "document.body.innerHTML='<p style=\"color:#c4983a;text-align:center;"
                "margin-top:40vh;font-family:sans-serif\">Error: el servidor no pudo iniciar.</p>'"
            )

    threading.Thread(target=navigate_when_ready, daemon=True).start()

    print()
    print("⚡ CONSEJO — Deliberación Multi-Agente")
    print("──────────────────────────────────────")
    print(f"   Servidor: {URL}")
    print(f"   Modelo:   {cfg.get('models', {}).get('default', 'gemma4:26b')}")
    vault = cfg.get("obsidian", {}).get("vault_path", "")
    print(f"   Obsidian: {vault or 'no configurado'}")
    print()

    webview.start(menu=menu, debug=False)


if __name__ == "__main__":
    main()
