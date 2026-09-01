#!/usr/bin/env python3
"""
app.py — plataforma de gerenciamento do vigia_braile

Dashboard web (Flask) que:
  - Mostra os resultados do extrator OCR (vigia_braile.py): OK, SEM_PECA_CADASTRADA, ERRO_OCR.
  - Permite filtrar por status e buscar por item/arquivo/código.
  - Tem um botão "Buscar novos" que dispara o scan das pastas configuradas em
    vigia_braile.py (mesma lógica do CLI) e um botão "Reprocessar pendentes"
    que refaz OCR nos que ficaram sem peça cadastrada/com erro (útil quando
    o arquivo é reimpresso depois do cadastro da peça).

Uso:
    pip3 install -r requirements.txt
    python3 app.py
    # abre em http://127.0.0.1:5000
"""

from __future__ import annotations

import threading

from flask import Flask, redirect, render_template, request, url_for

from paths import resource_path
import vigia_braile as vb

app = Flask(
    __name__,
    template_folder=str(resource_path("templates")),
    static_folder=str(resource_path("static")),
)

# Lock simples pra evitar dois scans concorrentes disparados pelo dashboard
# (a mesma falta de lock existe no vigia_braile.py rodando via agendador —
# aqui é sobretudo pra não deixar o usuário clicar 2x e corromper o estado).
_scan_lock = threading.Lock()


@app.route("/")
def index():
    status_filtro = request.args.get("status", "TODOS")
    busca = request.args.get("q", "").strip().lower()

    linhas = vb.ler_resultados()

    if status_filtro != "TODOS":
        linhas = [r for r in linhas if r["status"] == status_filtro]

    if busca:
        linhas = [
            r for r in linhas
            if busca in r["item"].lower()
            or busca in r["arquivo"].lower()
            or busca in r["codigo_peca"].lower()
        ]

    todas = vb.ler_resultados()
    resumo = {
        "total": len(todas),
        "ok": sum(1 for r in todas if r["status"] == "OK"),
        "sem_peca": sum(1 for r in todas if r["status"] == "SEM_PECA_CADASTRADA"),
        "erro_ocr": sum(1 for r in todas if r["status"] == "ERRO_OCR"),
    }

    return render_template(
        "index.html",
        linhas=linhas,
        resumo=resumo,
        status_filtro=status_filtro,
        busca=busca,
        pastas=[str(p) for p in vb.pastas_raiz()],
    )


@app.route("/scan", methods=["POST"])
def scan():
    reprocessar = request.form.get("reprocessar_pendentes") == "1"
    if _scan_lock.acquire(blocking=False):
        try:
            vb.executar_scan(reprocessar_pendentes=reprocessar)
        finally:
            _scan_lock.release()
    # se já tem um scan rodando, só ignora o clique em vez de empilhar.
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
