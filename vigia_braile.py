#!/usr/bin/env python3
"""
vigia_braile.py

1. Varre (recursivamente) uma ou mais pastas atrás de arquivos *_BRAILE*.jpg NOVOS
   (compara contra um estado persistido em disco, então funciona rodando 1-2x/dia
   via agendador, sem precisar ficar de pé o tempo todo).
2. Pra cada arquivo novo, faz OCR no rótulo "CÓDIGO PEÇA" (impresso em vermelho na
   imagem) e extrai o código.
3. Classifica como OK (código válido) ou SEM PEÇA CADASTRADA (código ausente ou
   vindo com placeholder tipo "CBCG618???").
4. Grava resultado num CSV (um append por execução) + log de texto.

Dependências (dev no M1):
    brew install tesseract tesseract-lang     # tesseract-lang traz o pacote "por"
    pip3 install pytesseract pillow

Uso:
    python3 vigia_braile.py
"""

from __future__ import annotations

import csv
import fnmatch
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import pytesseract
from PIL import Image

from paths import base_dir, resource_path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Pastas raiz a varrer (recursivo — já cobre IMAGENS_ANTIGAS, IMAGENS_ATUAIS
# e qualquer outra subpasta dentro delas). Lido de `vigia_braile_config.json`,
# ao lado do .exe/.py — editável sem precisar recompilar/reinstalar nada.
# Na primeira execução, se o arquivo não existir, ele é criado com um valor
# padrão de teste.
#
# PRODUÇÃO (rodando no Windows, onde H: já está mapeado):
#     {"pastas_raiz": ["H:\\IMAGENS_E_FACAS"]}
#
# TESTE (rodando no Mac): H: é um drive de rede mapeado dentro da rede
# Windows da empresa e normalmente NÃO fica acessível direto do Mac sem
# VPN/permissão de domínio. Mais simples pra testar: copiar algumas amostras
# reais de _BRAILE.jpg (incluindo pelo menos um caso com "???") pra uma pasta
# local e apontar o config pra ela.
CONFIG_FILE = base_dir() / "vigia_braile_config.json"
_PASTAS_RAIZ_PADRAO = [str(Path("~/vigia_braile_teste").expanduser())]


def _carregar_pastas_raiz() -> list[Path]:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps({"pastas_raiz": _PASTAS_RAIZ_PADRAO}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return [Path(p).expanduser() for p in _PASTAS_RAIZ_PADRAO]
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        pastas = cfg.get("pastas_raiz") or _PASTAS_RAIZ_PADRAO
    except (json.JSONDecodeError, OSError):
        pastas = _PASTAS_RAIZ_PADRAO
    return [Path(p).expanduser() for p in pastas]


PASTAS_RAIZ = _carregar_pastas_raiz()

FILTRO_NOME = "*_BRAILE*.jpg"   # glob, case-sensitive no Linux/macOS -> ver nota abaixo
OCR_LANG = "por+eng"

# Recorte do rótulo "CÓDIGO PEÇA" (canto superior direito da folha), em frações
# da largura/altura da imagem — assim funciona independente da resolução exata,
# desde que o layout do template seja sempre o mesmo (confirmado que sim).
# Calibrado em cima de uma amostra real de 2121x1349px.
CROP_LABEL = (0.80, 0.0, 1.0, 0.10)  # (esquerda, topo, direita, baixo) em % da imagem

ESTADO_FILE = base_dir() / "vigia_braile_estado.json"
LOG_FILE = base_dir() / "vigia_braile.log"
CSV_FILE = base_dir() / "vigia_braile_resultado.csv"

# Tesseract embutido no pacote .exe (ver .github/workflows/build-windows.yml):
# se existir, usa ele em vez de depender de uma instalação do sistema.
_TESSERACT_EMBUTIDO = resource_path("tesseract", "tesseract.exe")
if _TESSERACT_EMBUTIDO.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_TESSERACT_EMBUTIDO)

# Regex pra achar "CÓDIGO PEÇA" (tolerante a variação de OCR/acento) seguido do código.
# Aceita: CÓDIGO PEÇA, CODIGO PECA, C0DIGO PE(A, etc, e o código logo depois
# (letras/números/? na mesma linha ou na linha seguinte).
RE_LABEL = re.compile(
    r"C[ÓO0]DIGO\s*PE[ÇC]A\s*[:\-]?\s*\n?\s*([A-Z0-9?]{4,})",
    re.IGNORECASE,
)

# Regex pra extrair o "item" a partir do nome do arquivo, só pra referência no log.
# Ajustar quando souber o padrão exato do nome.
RE_ITEM_DO_NOME = re.compile(r"^(\d+)")


# ---------------------------------------------------------------------------
# TIPOS
# ---------------------------------------------------------------------------

@dataclass
class Resultado:
    timestamp: str
    arquivo: str
    pasta: str
    item: str
    codigo_peca: str
    status: str  # "OK" | "SEM_PECA_CADASTRADA" | "ERRO_OCR"


# ---------------------------------------------------------------------------
# ESTADO (persistido entre execuções, já que roda 1-2x/dia e não fica de pé)
# ---------------------------------------------------------------------------

def carregar_estado() -> set[str]:
    if not ESTADO_FILE.exists():
        return set()
    try:
        return set(json.loads(ESTADO_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        log(f"AVISO: estado corrompido em {ESTADO_FILE}, começando do zero.")
        return set()


def salvar_estado(caminhos: set[str]) -> None:
    ESTADO_FILE.write_text(
        json.dumps(sorted(caminhos), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# LOG
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linha)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(linha + "\n")


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def extrair_codigo_peca(caminho: Path) -> str | None:
    """Recorta o canto onde fica o rótulo 'CÓDIGO PEÇA' e roda OCR só nele.

    Recortar em vez de OCR na imagem inteira evita que o texto de colunas
    vizinhas do template (layout multi-coluna) se misture na leitura.
    """
    try:
        img = Image.open(caminho)
        w, h = img.size
        l, t, r, b = CROP_LABEL
        crop = img.crop((int(w * l), int(h * t), int(w * r), int(h * b)))
        texto = pytesseract.image_to_string(crop, lang=OCR_LANG)
    except Exception as e:
        log(f"ERRO_OCR em {caminho}: {e}")
        return None

    m = RE_LABEL.search(texto)
    if not m:
        return None
    return m.group(1).strip()


def codigo_valido(codigo: str | None) -> bool:
    if not codigo:
        return False
    if "?" in codigo:
        return False
    if len(codigo) < 4:
        return False
    return True


# ---------------------------------------------------------------------------
# SCAN
# ---------------------------------------------------------------------------

def varrer_pastas() -> list[Path]:
    """Retorna todos os *_BRAILE*.jpg encontrados nas pastas raiz, recursivo."""
    encontrados: list[Path] = []
    for pasta in PASTAS_RAIZ:
        if not pasta.exists():
            log(f"AVISO: pasta não encontrada, pulando: {pasta}")
            continue
        # rglob é case-sensitive no Linux/macOS; no Windows não importa.
        # Pra garantir cobertura cross-platform, casamos com FILTRO_NOME em
        # minúsculo manualmente em vez de depender do case do glob.
        for p in pasta.rglob("*.jpg"):
            if fnmatch.fnmatch(p.name.lower(), FILTRO_NOME.lower()):
                encontrados.append(p)
    return encontrados


def processar_arquivo(caminho: Path) -> Resultado:
    codigo = extrair_codigo_peca(caminho)
    m_item = RE_ITEM_DO_NOME.match(caminho.stem)
    item = m_item.group(1) if m_item else caminho.stem

    if codigo is None:
        status = "ERRO_OCR"
    elif codigo_valido(codigo):
        status = "OK"
    else:
        status = "SEM_PECA_CADASTRADA"

    resultado = Resultado(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        arquivo=caminho.name,
        pasta=str(caminho.parent),
        item=item,
        codigo_peca=codigo or "",
        status=status,
    )

    log(f"[{status}] {caminho.name}  item={item}  código={codigo!r}")
    return resultado


FIELDNAMES = [f for f in Resultado.__dataclass_fields__]  # ordem estável do CSV


def gravar_csv(resultados: list[Resultado]) -> None:
    novo = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if novo:
            writer.writeheader()
        for r in resultados:
            writer.writerow(asdict(r))


def ler_resultados() -> list[dict]:
    """Lê o CSV completo (usado pelo dashboard). Retorna lista de dicts, mais
    recente primeiro."""
    if not CSV_FILE.exists():
        return []
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    linhas.reverse()
    return linhas


def _ultimo_status_por_caminho() -> dict[str, str]:
    """Pra cada arquivo já registrado no CSV, o status da última passada
    (linhas mais recentes por último no arquivo, então elas vencem)."""
    status: dict[str, str] = {}
    if not CSV_FILE.exists():
        return status
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            caminho = str(Path(row["pasta"]) / row["arquivo"])
            status[caminho] = row["status"]
    return status


# ---------------------------------------------------------------------------
# SCAN (função usada tanto pelo CLI quanto pelo dashboard Flask)
# ---------------------------------------------------------------------------

def executar_scan(reprocessar_pendentes: bool = False) -> list[Resultado]:
    """Varre as pastas, processa os arquivos novos (e, se
    `reprocessar_pendentes=True`, também os que já foram vistos mas ficaram
    como SEM_PECA_CADASTRADA/ERRO_OCR — útil quando alguém reimprime o mesmo
    arquivo depois de cadastrar a peça) e persiste CSV + estado.
    """
    conhecidos = carregar_estado()
    todos = varrer_pastas()
    caminhos_atuais = {str(p) for p in todos}

    a_processar = set(p for p in todos if str(p) not in conhecidos)

    if reprocessar_pendentes:
        pendentes = {
            c for c, s in _ultimo_status_por_caminho().items()
            if s in ("SEM_PECA_CADASTRADA", "ERRO_OCR")
        }
        a_processar |= {p for p in todos if str(p) in pendentes}

    resultados: list[Resultado] = []
    for caminho in sorted(a_processar):
        resultados.append(processar_arquivo(caminho))

    if resultados:
        gravar_csv(resultados)

    salvar_estado(caminhos_atuais)
    return resultados


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    log("=== Execução iniciada ===")

    resultados = executar_scan()

    sem_peca = sum(1 for r in resultados if r.status == "SEM_PECA_CADASTRADA")
    erros = sum(1 for r in resultados if r.status == "ERRO_OCR")
    log(
        f"{len(resultados)} arquivo(s) novo(s) processado(s) — "
        f"{sem_peca} sem peça cadastrada, {erros} com erro de OCR."
    )
    log("=== Execução finalizada ===\n")


if __name__ == "__main__":
    main()
