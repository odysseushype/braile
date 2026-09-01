#!/usr/bin/env python3
"""
launcher.py — ponto de entrada do .exe empacotado (PyInstaller).

`python3 app.py` (modo dev) usa o servidor de desenvolvimento do Flask.
Já o .exe empacotado sobe com waitress (servidor WSGI de produção, sem os
avisos/instabilidade do servidor de dev) e abre o navegador padrão sozinho,
pra rodar com duplo clique sem precisar digitar nada no terminal.

Todo o corpo roda dentro de um try/except: se algo falhar ao iniciar (ex.:
uma dependência que não carregou direito dentro do .exe empacotado), a
janela do console fecha sozinha rápido demais pra dar tempo de ler o erro
quando aberta por duplo clique. Por isso, em caso de erro, o traceback é
gravado num arquivo de log ao lado do .exe e a janela fica aberta esperando
ENTER antes de fechar.
"""

from __future__ import annotations

import sys
import threading
import traceback
import webbrowser

from paths import base_dir

HOST = "127.0.0.1"
PORT = 5000


def _abrir_navegador() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}/")


def _registrar_falha(exc: BaseException) -> None:
    log_path = base_dir() / "vigia_braile_crash.log"
    texto = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        log_path.write_text(texto, encoding="utf-8")
    except OSError:
        pass
    print("ERRO ao iniciar o Vigia Braile:\n")
    print(texto)
    print(f"(esse erro também foi salvo em {log_path})")
    input("\nPressione ENTER pra fechar...")


def main() -> None:
    from waitress import serve

    from app import app

    threading.Timer(1.0, _abrir_navegador).start()
    print(f"Vigia Braile rodando em http://{HOST}:{PORT}  (feche esta janela pra encerrar)")
    serve(app, host=HOST, port=PORT)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # captura falha de import/inicialização também
        _registrar_falha(exc)
        sys.exit(1)
