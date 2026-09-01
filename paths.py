"""
paths.py — resolução de caminhos que funciona tanto rodando via `python3
vigia_braile.py` / `python3 app.py` (dev) quanto empacotado como .exe pelo
PyInstaller (produção).

Duas noções de "pasta base":

- `base_dir()`      — onde ficam os dados EDITÁVEIS pelo usuário (config,
                        CSV, log, estado). Ao lado do .exe quando empacotado;
                        ao lado dos .py em dev. É aqui que o time de TI edita
                        `vigia_braile_config.json` pra apontar pro H:\\ de
                        produção, sem precisar recompilar nada.
- `resource_path()` — onde ficam os recursos empacotados SOMENTE LEITURA
                        (templates/, static/, o Tesseract embutido). Dentro
                        do bundle do PyInstaller (`sys._MEIPASS`) quando
                        empacotado; ao lado dos .py em dev.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return SCRIPT_DIR


def resource_path(*parts: str) -> Path:
    if is_frozen():
        root = Path(getattr(sys, "_MEIPASS", base_dir()))
    else:
        root = SCRIPT_DIR
    return root.joinpath(*parts)
