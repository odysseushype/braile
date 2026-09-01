#!/usr/bin/env python3
"""
launcher.py — ponto de entrada do .exe empacotado (PyInstaller).

`python3 app.py` (modo dev) usa o servidor de desenvolvimento do Flask.
Já o .exe empacotado sobe com waitress (servidor WSGI de produção, sem os
avisos/instabilidade do servidor de dev) e abre o navegador padrão sozinho,
pra rodar com duplo clique sem precisar digitar nada no terminal.
"""

from __future__ import annotations

import threading
import webbrowser

from waitress import serve

from app import app

HOST = "127.0.0.1"
PORT = 5000


def _abrir_navegador() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}/")


if __name__ == "__main__":
    threading.Timer(1.0, _abrir_navegador).start()
    print(f"Vigia Braile rodando em http://{HOST}:{PORT}  (feche esta janela pra encerrar)")
    serve(app, host=HOST, port=PORT)
