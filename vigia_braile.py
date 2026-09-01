#!/usr/bin/env python3
"""
vigia_braile.py

1. Varre (recursivamente) uma ou mais pastas atrás de arquivos *_BRAILE*.jpg NOVOS
   (compara contra um estado persistido em disco, então funciona rodando 1-2x/dia
   via agendador, sem precisar ficar de pé o tempo todo).
2. Pra cada arquivo novo, faz OCR no código da peça, que aparece em dois formatos
   de template diferentes:
     - COLAGEM: código "CBCG..." impresso em vermelho, numa caixa com o rótulo
       "CÓDIGO PEÇA" no canto superior direito.
     - CORTE_VINCO: código "CB..." (sem o G) impresso em azul, na frase
       "<código> GENERICO FACA <n° faca>", posição mais variável na folha.
3. Classifica como OK (código válido) ou SEM PEÇA CADASTRADA (código ausente ou
   vindo com placeholder tipo "CBCG618???" — peça ainda não fechada).
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
from PIL import Image, ImageChops

from paths import base_dir, resource_path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Pastas raiz a varrer (recursivo — já cobre IMAGENS_ANTIGAS, IMAGENS_ATUAIS
# e qualquer outra subpasta dentro delas). Lido de `vigia_braile_config.json`,
# ao lado do .exe/.py — editável sem precisar recompilar/reinstalar nada, e
# relido a cada scan (não precisa reiniciar o programa depois de editar).
# Na primeira execução, se o arquivo não existir, ele é criado já com o
# padrão de produção abaixo.
#
# PRODUÇÃO (rodando no Windows, onde H: já está mapeado) — padrão de fábrica:
#     {"pastas_raiz": ["H:\\IMAGENS_E_FACAS"]}
#
# TESTE (rodando no Mac): H: é um drive de rede mapeado dentro da rede
# Windows da empresa e normalmente NÃO fica acessível direto do Mac sem
# VPN/permissão de domínio. Mais simples pra testar: copiar algumas amostras
# reais de _BRAILE.jpg (incluindo pelo menos um caso com "???") pra uma pasta
# local e apontar o config pra ela, ex. `{"pastas_raiz": ["~/vigia_braile_teste"]}`.
CONFIG_FILE = base_dir() / "vigia_braile_config.json"

# `prefixos_item`: só itens cujo número (início do nome do arquivo) começa
# com um desses prefixos são processados — o resto da pasta é ignorado.
# Hoje só usamos os itens 50021* e 618*; ajustar aqui (ou no
# vigia_braile_config.json direto) se a faixa de itens mudar.
_CONFIG_PADRAO = {
    "pastas_raiz": [r"H:\IMAGENS_E_FACAS"],
    "prefixos_item": ["50021", "618"],
}


def _carregar_config() -> dict:
    """Lê `vigia_braile_config.json` do disco a cada chamada — assim, editar
    o arquivo com o programa já aberto tem efeito no próximo scan, sem
    precisar reiniciar o .exe. Cria o arquivo com os valores padrão na
    primeira execução, se ele ainda não existir."""
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(_CONFIG_PADRAO, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return dict(_CONFIG_PADRAO)
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log(f"AVISO: {CONFIG_FILE.name} corrompido/ilegível, usando padrão de fábrica.")
        return dict(_CONFIG_PADRAO)
    # completa com o padrão qualquer chave ausente (ex.: config salvo antes
    # de 'prefixos_item' existir).
    return {**_CONFIG_PADRAO, **cfg}


def pastas_raiz() -> list[Path]:
    pastas = _carregar_config().get("pastas_raiz") or _CONFIG_PADRAO["pastas_raiz"]
    return [Path(p).expanduser() for p in pastas]


def prefixos_item() -> list[str]:
    prefixos = _carregar_config().get("prefixos_item") or _CONFIG_PADRAO["prefixos_item"]
    return [str(p) for p in prefixos]

FILTRO_NOME = "*_BRAILE*.jpg"   # glob, case-sensitive no Linux/macOS -> ver nota abaixo

# Área de busca pelo código (canto superior direito da folha), em frações da
# largura/altura da imagem. Não é o recorte final — é só a região onde
# procuramos pixels vermelhos; por isso pode ser generosa o bastante pra
# tolerar a margem em branco variável entre um scan e outro (ver
# `_achar_caixa_vermelha`).
AREA_BUSCA_CODIGO = (0.55, 0.0, 1.0, 0.25)  # (esquerda, topo, direita, baixo) em % da imagem

# Pixel é considerado "vermelho" (tinta do código impresso) com esses limiares.
LIMIAR_VERMELHO_MIN = 120
LIMIAR_VERMELHO_DIF = 60

# Faixas de busca (esquerda, topo, direita, baixo em % da imagem) pro código
# CORTE_VINCO ("CB... GENERICO FACA"), usado quando não há caixa vermelha.
# Nas amostras reais essa frase aparece tanto perto do topo quanto perto do
# rodapé da folha — por isso duas faixas em vez de uma área única: rodar o
# OCR na página inteira de uma vez faz o Tesseract errar a segmentação e
# "perder" o texto que teria lido bem numa região menor.
FAIXAS_BUSCA_CORTE_VINCO = [
    (0.30, 0.0, 1.0, 0.60),
    (0.28, 0.30, 1.0, 1.0),
]

ESTADO_FILE = base_dir() / "vigia_braile_estado.json"
LOG_FILE = base_dir() / "vigia_braile.log"
CSV_FILE = base_dir() / "vigia_braile_resultado.csv"

# Tesseract embutido no pacote .exe (ver .github/workflows/build-windows.yml):
# se existir, usa ele em vez de depender de uma instalação do sistema.
_TESSERACT_EMBUTIDO = resource_path("tesseract", "tesseract.exe")
if _TESSERACT_EMBUTIDO.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_TESSERACT_EMBUTIDO)

# Regex pra extrair o "item" a partir do nome do arquivo, só pra referência no log.
# Ajustar quando souber o padrão exato do nome.
RE_ITEM_DO_NOME = re.compile(r"^(\d+)")

# Código CORTE_VINCO: "CB" + código, seguido (com espaço/quebra de linha no
# meio, tolerado pelo \s*) da frase "GENERICO FACA" — é essa frase ao lado que
# distingue o código da peça de outros códigos "CB..." que aparecem na folha
# (ex.: o código da matriz braile, que não é o que nos interessa aqui).
RE_CORTE_VINCO = re.compile(r"(CB[0-9A-Z?]{4,12})\s*GEN[EÉ]RICO\s*FACA", re.IGNORECASE)


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
    tipo: str    # "COLAGEM" | "CORTE_VINCO" | "" (não identificado)
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

def _achar_caixa_vermelha(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Localiza a caixa do código impresso em vermelho dentro de
    `AREA_BUSCA_CODIGO`, procurando pixels vermelhos em vez de recortar uma
    fração fixa da imagem.

    Por que: a margem em branco em volta do template varia de scan pra scan
    (a mesma folha às vezes é digitalizada com mais ou menos borda), então um
    recorte fixo erra a posição da caixa em boa parte dos arquivos reais.
    Buscar pela cor é mais robusto — acha a caixa onde quer que ela esteja
    dentro do quadrante superior direito — e como bônus filtra sozinho o
    texto preto do rótulo "CÓDIGO PEÇA" ao redor, isolando só o código.

    Retorna None quando não há pixels vermelhos o bastante na área de busca
    — sinal de que este arquivo é do template CORTE_VINCO, que não tem essa
    caixa (ver `_achar_codigo_corte_vinco`).
    """
    w, h = img.size
    l, t, r, b = AREA_BUSCA_CODIGO
    l, t, r, b = int(w * l), int(h * t), int(w * r), int(h * b)
    regiao = img.crop((l, t, r, b)).convert("RGB")

    # Feito só com PIL (sem numpy) de propósito: numpy empacotado com
    # PyInstaller deu problema pra carregar no .exe em produção (erro de DLL
    # logo na abertura), e essa conta é simples o bastante pra não precisar
    # dele. ImageChops.subtract já clampa negativos em 0, então cada máscara
    # abaixo corresponde exatamente ao equivalente numpy "canal > limiar".
    red, green, blue = regiao.split()
    diff_rg = ImageChops.subtract(red, green)
    diff_rb = ImageChops.subtract(red, blue)

    mask_r = red.point(lambda p: 255 if p > LIMIAR_VERMELHO_MIN else 0)
    mask_rg = diff_rg.point(lambda p: 255 if p > LIMIAR_VERMELHO_DIF else 0)
    mask_rb = diff_rb.point(lambda p: 255 if p > LIMIAR_VERMELHO_DIF else 0)
    mask = ImageChops.darker(ImageChops.darker(mask_r, mask_rg), mask_rb)

    if mask.histogram()[255] < 20:  # poucos pixels = ruído, não uma caixa real
        return None
    caixa = mask.getbbox()
    if caixa is None:
        return None

    pad = 12
    x0, y0, x1, y1 = caixa
    x0, x1 = max(x0 - pad, 0), min(x1 + pad, regiao.width)
    y0, y1 = max(y0 - pad, 0), min(y1 + pad, regiao.height)
    return (l + x0, t + y0, l + x1, t + y1)


def _achar_codigo_corte_vinco(img: Image.Image) -> str | None:
    """Procura o código CORTE_VINCO ("CB..." seguido de "GENERICO FACA"),
    usado quando não há caixa vermelha na folha.

    Roda OCR em texto livre (não é uma caixa isolada como no caso da
    colagem) em duas faixas verticais — nas amostras reais essa frase
    aparece tanto perto do topo quanto perto do rodapé, dependendo do
    template — e usa `RE_CORTE_VINCO` pra achar o código certo em meio ao
    resto do texto da folha (incluindo outros códigos "CB..." que não são o
    código da peça, como o da matriz braile).
    """
    w, h = img.size
    for l, t, r, b in FAIXAS_BUSCA_CORTE_VINCO:
        crop = img.crop((int(w * l), int(h * t), int(w * r), int(h * b)))
        texto = pytesseract.image_to_string(crop, lang="por+eng")
        m = RE_CORTE_VINCO.search(texto)
        if m:
            return m.group(1).strip().upper()
    return None


def extrair_codigo_peca(caminho: Path) -> tuple[str, str | None, str]:
    """Localiza e lê por OCR o código da peça, tentando os dois templates.

    Retorna (motivo, código, tipo):
      - ("ok", codigo, "COLAGEM")     — caixa vermelha "CÓDIGO PEÇA" (CBCG...).
      - ("ok", codigo, "CORTE_VINCO") — código "CB..." + "GENERICO FACA".
      - ("erro", None, "")            — nenhum dos dois padrões foi
                                          reconhecido (ou falha ao abrir a
                                          imagem).
    """
    try:
        img = Image.open(caminho)

        caixa = _achar_caixa_vermelha(img)
        if caixa is not None:
            crop = img.crop(caixa)
            crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
            texto = pytesseract.image_to_string(
                crop,
                lang="eng",
                config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789?",
            ).strip()
            if texto:
                return "ok", texto, "COLAGEM"

        codigo_cv = _achar_codigo_corte_vinco(img)
        if codigo_cv:
            return "ok", codigo_cv, "CORTE_VINCO"
    except Exception as e:
        log(f"ERRO_OCR em {caminho}: {e}")
        return "erro", None, ""

    return "erro", None, ""


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
    """Retorna todos os *_BRAILE*.jpg encontrados nas pastas raiz, recursivo,
    restrito aos itens cujo nome começa com um dos `prefixos_item` do
    config (ex.: 50021*, 618*) — o resto da pasta é ignorado.

    Quando o mesmo nome de arquivo aparece em mais de uma pasta (ex.: uma
    cópia antiga em IMAGENS_ANTIGAS e uma mais nova em IMAGENS_ATUAIS),
    mantém só a versão com data de modificação mais recente — as demais são
    ignoradas, não processadas as duas.
    """
    prefixos = tuple(prefixos_item())
    encontrados: dict[str, Path] = {}
    for pasta in pastas_raiz():
        if not pasta.exists():
            log(f"AVISO: pasta não encontrada, pulando: {pasta}")
            continue
        # rglob é case-sensitive no Linux/macOS; no Windows não importa.
        # Pra garantir cobertura cross-platform, casamos com FILTRO_NOME em
        # minúsculo manualmente em vez de depender do case do glob.
        for p in pasta.rglob("*.jpg"):
            if not fnmatch.fnmatch(p.name.lower(), FILTRO_NOME.lower()):
                continue
            if prefixos and not p.name.startswith(prefixos):
                continue
            chave = p.name.lower()
            existente = encontrados.get(chave)
            if existente is None:
                encontrados[chave] = p
            elif p.stat().st_mtime > existente.stat().st_mtime:
                log(f"AVISO: '{p.name}' duplicado em duas pastas — usando o mais recente: {p.parent}")
                encontrados[chave] = p
    return list(encontrados.values())


def processar_arquivo(caminho: Path) -> Resultado:
    motivo, codigo, tipo = extrair_codigo_peca(caminho)
    m_item = RE_ITEM_DO_NOME.match(caminho.stem)
    item = m_item.group(1) if m_item else caminho.stem

    if motivo == "erro":
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
        tipo=tipo,
        status=status,
    )

    log(f"[{status}] {caminho.name}  item={item}  tipo={tipo or '?'}  código={codigo!r}")
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
