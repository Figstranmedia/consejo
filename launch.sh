#!/bin/bash
# CONSEJO — Primer arranque
# Instala dependencias y lanza la app

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PYTHON="python3"
VENV="$DIR/.venv"

# Crear venv si no existe
if [ ! -d "$VENV" ]; then
  echo "⚡ Creando entorno virtual…"
  $PYTHON -m venv "$VENV"
fi

source "$VENV/bin/activate"

# Instalar/actualizar dependencias
echo "⚡ Verificando dependencias…"
pip install -q -r requirements.txt

echo "⚡ Iniciando CONSEJO…"
python app.py
