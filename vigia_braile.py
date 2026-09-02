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

# Desliga o limite de "decompression bomb" do PIL: achado real (item
# 500212555, scan de 19602x14473px = ~284 milhões de pixels) — o padrão do
# PIL (~178 milhões) rejeita esse arquivo com ERRO_OCR mesmo sendo um scan
# legítimo, só em resolução alta demais. São sempre arquivos internos
# confiáveis da rede da empresa, não uploads de terceiros, então não faz
# sentido manter essa proteção (pensada pra imagem vinda de fonte não
# confiável) ligada aqui.
Image.MAX_IMAGE_PIXELS = None

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
# CORTE_VINCO, usado quando não há caixa vermelha. Nas amostras reais o
# código aparece tanto perto do topo quanto perto do rodapé da folha — por
# isso duas faixas em vez de uma área única: rodar o OCR na página inteira
# de uma vez faz o Tesseract errar a segmentação e "perder" texto que teria
# lido bem numa região menor. Achado real (item 50021833, template sem
# cabeçalho ACCUBRAILLE): nesse layout o código fica bem mais à esquerda
# (x≈18% da largura) que nos outros (x≈28-30%) — por isso a borda esquerda
# é bem mais generosa aqui do que nos primeiros templates calibrados.
FAIXAS_BUSCA_CORTE_VINCO = [
    (0.15, 0.0, 1.0, 0.60),
    (0.15, 0.30, 1.0, 1.0),
]

# Tamanho mínimo de um "candidato a código" (depois de tirar espaço/pontuação)
# achado na varredura grosseira de palavras da faixa.
TAMANHO_MIN_CANDIDATO = 5

# Padding (px) e fator de ampliação usados no recorte fino em volta da
# palavra-candidata, antes do segundo passe de OCR (ver `_ler_codigo_fino`).
PAD_CANDIDATO_X = 25
PAD_CANDIDATO_Y = 4
AMPLIACAO_CANDIDATO = 6

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

# Formato do código depois do OCR fino (ver `_ler_codigo_fino`): "CB" +
# OBRIGATORIAMENTE um dígito (ou "I"/"O", que o OCR troca com "1"/"0" o
# tempo todo — sem aceitar essas duas o fix abaixo rejeitava leituras
# corretas tipo "CBI7K16A", só porque o "1" saiu "I") logo após "CB" + até
# 7 caracteres alfanuméricos/"?". Achado real: sem essa exigência, o
# fallback de força bruta ocasionalmente "lia" ruído puro como código
# (ex.: "CBLINOLAWC", "CBLOI?77TS" — nenhum código real começa com uma
# letra qualquer logo depois do CB), e isso virava "OK" com um código
# errado — pior que reportar erro, porque parece confiável e não é.
RE_CODIGO_PLACA = re.compile(r"CB[0-9IO][0-9A-Z?]{2,7}")


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
    baixa_confianca: bool  # True = achado só no fallback de força bruta; vale conferir manualmente


# ---------------------------------------------------------------------------
# ESTADO (persistido entre execuções, já que roda 1-2x/dia e não fica de pé)
# ---------------------------------------------------------------------------

def carregar_estado() -> set[str]:
    """Retorna o conjunto de NOMES de arquivo (não caminho completo) já
    processados. Guardar só o nome (em vez do caminho absoluto) é
    proposital: assim o mesmo `vigia_braile_estado.json` funciona
    independente de onde a pasta de imagens está montada (H:\\... em
    produção, uma cópia local em teste, etc.) — inclusive dá pra gerar um
    estado "adiantado" fora daqui (ex.: depois de um lote de testes) e
    simplesmente colocá-lo do lado do .exe pra não reprocessar tudo nele."""
    if not ESTADO_FILE.exists():
        return set()
    try:
        return set(json.loads(ESTADO_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        log(f"AVISO: estado corrompido em {ESTADO_FILE}, começando do zero.")
        return set()


def salvar_estado(nomes: set[str]) -> None:
    ESTADO_FILE.write_text(
        json.dumps(sorted(nomes), ensure_ascii=False, indent=2),
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


def _candidato_valido(txt: str) -> bool:
    """Filtro grosseiro pra decidir se uma palavra achada pelo OCR de texto
    livre vale a pena ser refinada: precisa ter tamanho mínimo e misturar
    letra com número (só números = provavelmente o número do item; só
    letras = provavelmente uma palavra comum, tipo "GENERICO")."""
    limpo = re.sub(r"[^A-Z0-9?]", "", txt.upper())
    if len(limpo) < TAMANHO_MIN_CANDIDATO:
        return False
    return any(c.isalpha() for c in limpo) and any(c.isdigit() for c in limpo)


def _ler_codigo_fino(palavra: Image.Image, vertical: bool) -> str | None:
    """Segundo passe de OCR, bem ampliado e com charset restrito, numa
    palavra-candidata já isolada. Crucial pra não confundir "???"
    (placeholder de peça não fechada) com dígitos tipo "222" — o que
    acontecia com o texto pequeno/vertical desse template rodando OCR de
    texto livre direto.

    A orientação (o texto às vezes vem de baixo pra cima, rotacionado) e o
    PSM que funciona melhor dependem de a palavra ser mais alta que larga
    (vertical) ou não — calibrado em cima de amostras reais dos dois casos.
    """
    rot = palavra.rotate(-90, expand=True) if vertical else palavra
    psms = (11, 7) if vertical else (8, 6, 7, 13)
    for psm in psms:
        texto = pytesseract.image_to_string(
            rot,
            lang="eng",
            config=f"--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789?",
        ).strip()
        m = RE_CODIGO_PLACA.search(texto.replace("\n", " "))
        if m:
            return m.group(0)
    return None


def _achar_codigo_corte_vinco(img: Image.Image) -> str | None:
    """Procura o código da peça nos templates sem caixa vermelha: o código
    impresso ao lado da placa/padrão braile (preto, às vezes vertical).

    Importante: NÃO é o código azul junto de "GENERICO FACA" — esse é o
    código da FACA (ferramenta de corte-vinco), não da peça. O código da
    peça normalmente aparece antes dele na folha, por isso ele é
    explicitamente excluído (`proximas` abaixo) quando encontrado colado a
    "GENERICO"/"FACA".

    Estratégia em duas passadas: (1) OCR de texto livre, grosseiro, só pra
    localizar a posição aproximada da palavra-candidata (`image_to_data`
    dá a caixa de cada palavra); (2) recorta bem apertado em volta dela,
    amplia bastante e refaz o OCR com charset restrito (`_ler_codigo_fino`)
    pra uma leitura limpa.
    """
    w, h = img.size
    for l, t, r, b in FAIXAS_BUSCA_CORTE_VINCO:
        regiao_l, regiao_t = int(w * l), int(h * t)
        regiao = img.crop((regiao_l, regiao_t, int(w * r), int(h * b)))
        data = pytesseract.image_to_data(regiao, lang="por+eng", output_type=pytesseract.Output.DICT)

        for i, txt in enumerate(data["text"]):
            if not _candidato_valido(txt.strip()):
                continue
            proximas = " ".join(data["text"][i + 1:i + 4]).upper()
            if "GENERIC" in proximas or "FACA" in proximas:
                continue

            lx, ty = data["left"][i], data["top"][i]
            largura, altura = data["width"][i], data["height"][i]
            if largura < 3 or altura < 3:
                continue

            x0 = max(regiao_l + lx - PAD_CANDIDATO_X, 0)
            y0 = max(regiao_t + ty - PAD_CANDIDATO_Y, 0)
            x1 = min(regiao_l + lx + largura + PAD_CANDIDATO_X, w)
            y1 = min(regiao_t + ty + altura + PAD_CANDIDATO_Y, h)
            palavra = img.crop((x0, y0, x1, y1))
            palavra = palavra.resize(
                (palavra.width * AMPLIACAO_CANDIDATO, palavra.height * AMPLIACAO_CANDIDATO),
                Image.LANCZOS,
            )

            resultado = _ler_codigo_fino(palavra, vertical=altura > largura)
            if resultado:
                return resultado
    return None


LARGURA_TILE_BRUTO = 60
PASSO_TILE_BRUTO = 30
# Padding horizontal bem menor que `PAD_CANDIDATO_X` (25px) -- achado real
# (item 618022): nesse sub-template as palavras vizinhas (código, número
# do item, nome do produto) ficam só ~8px uma da outra, então o padding
# do caminho principal (calibrado pro layout mais espaçado do COLAGEM)
# engolia o começo da palavra seguinte no recorte, contaminando o código
# lido (ex.: "CB17K16A" virava "CB17K16A61", grudando o "61" de "618022").
PAD_CANDIDATO_X_BRUTO = 3


def _achar_codigo_corte_vinco_bruto(img: Image.Image) -> str | None:
    """Fallback pra quando `_achar_codigo_corte_vinco` não acha nada --
    achado real 2026-09-02, calibrando contra 953 amostras reais (família
    EMS 618xxx/50021xxx): nesse sub-template o texto do código fica bem
    pequeno, e o Tesseract simplesmente não marca essa região como
    "palavra" quando o passe de `image_to_data` roda na FAIXA INTEIRA de
    uma vez (região grande demais, mistura demais coisa) -- não é
    confusão com os pontinhos braile (testado isolado: o texto está limpo,
    separado dos pontos por uma linha), é escala/quantidade de conteúdo no
    mesmo `image_to_data`.

    BUG achado na 1ª versão desse fallback (usava `image_to_string`, texto
    corrido, em vez de `image_to_data`): sem separação de palavra, o
    código colava direto no texto vizinho (ex. real: item 618022, código
    certo "CB17K16A" virou "CBI7K16A61" -- os 2 primeiros dígitos de
    "618022", o item logo depois na mesma linha, grudaram no fim por
    causa do regex sem fronteira de palavra). CONFIRMADO com o mesmo
    recorte: `image_to_data` SEPARA "CBI7K16A" de "610022" como palavras
    distintas -- é só uma questão de rodar em tiles menores que a faixa
    inteira (não em texto corrido). Por isso esse fallback reusa a MESMA
    lógica de candidato/exclusão de `_achar_codigo_corte_vinco` (não
    reinventa), só que iterando tiles (`LARGURA_TILE_BRUTO`, sobrepostos
    por `PASSO_TILE_BRUTO` pra não cortar uma palavra bem na borda do
    tile) em vez da faixa inteira de uma vez."""
    w, h = img.size
    for l, t, r, b in FAIXAS_BUSCA_CORTE_VINCO:
        regiao_l, regiao_t = int(w * l), int(h * t)
        regiao_r, regiao_b = int(w * r), int(h * b)
        x = regiao_l
        while x < regiao_r:
            tile = img.crop((x, regiao_t, min(x + LARGURA_TILE_BRUTO, regiao_r), regiao_b))
            # Rotaciona o TILE inteiro antes do image_to_data -- achado
            # real: sem isso, o Tesseract não separa as palavras verticais
            # (texto vira "sopa" sem word-boxes). Roda o candidato/recorte
            # todo em cima do tile já rotacionado (coordenadas do tile
            # rotacionado, não precisa mapear de volta pro original -- a
            # palavra recortada daqui já sai na orientação certa pro
            # `_ler_codigo_fino`, por isso `vertical=False` abaixo).
            rot = tile.rotate(-90, expand=True)
            data = pytesseract.image_to_data(rot, lang="por+eng", output_type=pytesseract.Output.DICT)

            for i, txt in enumerate(data["text"]):
                if not _candidato_valido(txt.strip()):
                    continue
                proximas = " ".join(data["text"][i + 1:i + 4]).upper()
                if "GENERIC" in proximas or "FACA" in proximas:
                    continue

                lx, ty = data["left"][i], data["top"][i]
                largura, altura = data["width"][i], data["height"][i]
                if largura < 3 or altura < 3:
                    continue

                x0 = max(lx - PAD_CANDIDATO_X_BRUTO, 0)
                y0 = max(ty - PAD_CANDIDATO_Y, 0)
                x1 = min(lx + largura + PAD_CANDIDATO_X_BRUTO, rot.width)
                y1 = min(ty + altura + PAD_CANDIDATO_Y, rot.height)
                palavra = rot.crop((x0, y0, x1, y1))
                palavra = palavra.resize(
                    (palavra.width * AMPLIACAO_CANDIDATO, palavra.height * AMPLIACAO_CANDIDATO),
                    Image.LANCZOS,
                )

                resultado = _ler_codigo_fino(palavra, vertical=False)
                if resultado:
                    return resultado
            x += PASSO_TILE_BRUTO
    return None


LARGURA_TILE_FORCA_BRUTA = 70
PASSO_TILE_FORCA_BRUTA = 35


def _achar_codigo_corte_vinco_forca_bruta(img: Image.Image) -> str | None:
    """Último fallback, só quando nem `_achar_codigo_corte_vinco` nem
    `_achar_codigo_corte_vinco_bruto` acham nada -- achado real (item
    50021833, template sem cabeçalho ACCUBRAILLE, aprovação estilo "SANOFI
    AVENTIS" antigo): mesmo rodando em tiles, o `image_to_data` às vezes
    simplesmente falha em separar a palavra do código como um "word box"
    nesse template (a rotação + o texto vizinho — "NATURETTI", o range de
    item "50021833 - 50021834" — confundem a segmentação), mesmo a palavra
    estando nitidamente legível quando isolada manualmente. Contornado
    pulando o passo de localização por palavra inteiramente: roda o OCR
    FINO (mesmo charset restrito de `_ler_codigo_fino`) direto em cada
    tile, e procura o código no texto resultante via regex. Mais lento (um
    OCR fino de verdade por tile, não só quando acha candidato) — por isso
    é o último recurso, não o primeiro.
    """
    w, h = img.size
    for l, t, r, b in FAIXAS_BUSCA_CORTE_VINCO:
        regiao_l, regiao_t = int(w * l), int(h * t)
        regiao_r, regiao_b = int(w * r), int(h * b)
        x = regiao_l
        while x < regiao_r:
            tile = img.crop((x, regiao_t, min(x + LARGURA_TILE_FORCA_BRUTA, regiao_r), regiao_b))
            rot = tile.rotate(-90, expand=True)
            for psm in (6, 11):
                texto = pytesseract.image_to_string(
                    rot,
                    lang="eng",
                    config=f"--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789?",
                ).strip()
                if "GENERIC" in texto.upper() or "FACA" in texto.upper():
                    continue
                m = RE_CODIGO_PLACA.search(texto.replace("\n", " "))
                if m:
                    return m.group(0)
            x += PASSO_TILE_FORCA_BRUTA
    return None


def extrair_codigo_peca(caminho: Path) -> tuple[str, str | None, str, bool]:
    """Localiza e lê por OCR o código da peça, tentando os dois templates.

    Retorna (motivo, código, tipo, baixa_confianca):
      - ("ok", codigo, "COLAGEM", False)     — caixa vermelha "CÓDIGO PEÇA"
                                                 (CBCG...).
      - ("ok", codigo, "CORTE_VINCO", False) — código junto da placa/padrão
                                                 braile, achado pelo caminho
                                                 rápido (nunca o código azul
                                                 da FACA).
      - ("ok", codigo, "CORTE_VINCO", True)  — mesma coisa, mas só achado no
                                                 último fallback (força
                                                 bruta): texto pequeno
                                                 demais/scan ruim o
                                                 bastante pra valer a pena
                                                 uma conferência manual —
                                                 ver `baixa_confianca` no
                                                 dashboard.
      - ("erro", None, "", False)            — nenhum dos dois padrões foi
                                                 reconhecido (ou falha ao
                                                 abrir a imagem) — inclui
                                                 folhas de aprovação de FACA
                                                 compartilhadas entre vários
                                                 itens, que não têm um
                                                 código de peça individual.
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
            # Bug real achado (item 618116 e outros): em alguns arquivos não
            # existe caixa "CÓDIGO PEÇA" nenhuma, e o texto vermelho mais
            # próximo do quadrante de busca é o rótulo "CORTE E VINCO"
            # (categoria da peça, não um código) — sem essa checagem ele
            # virava "ok" com codigo_peca="CORTEEVINCO". Um código de
            # verdade sempre tem dígito; texto puramente alfabético não é
            # código, então cai pro fallback CORTE_VINCO abaixo.
            if texto and any(c.isdigit() for c in texto):
                return "ok", texto, "COLAGEM", False

        codigo_cv = _achar_codigo_corte_vinco(img)
        if codigo_cv:
            return "ok", codigo_cv, "CORTE_VINCO", False

        codigo_cv_bruto = _achar_codigo_corte_vinco_bruto(img)
        if codigo_cv_bruto:
            return "ok", codigo_cv_bruto, "CORTE_VINCO", False

        codigo_cv_forca = _achar_codigo_corte_vinco_forca_bruta(img)
        if codigo_cv_forca:
            return "ok", codigo_cv_forca, "CORTE_VINCO", True
    except Exception as e:
        log(f"ERRO_OCR em {caminho}: {e}")
        return "erro", None, "", False

    return "erro", None, "", False


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

    Quando o mesmo item tem um arquivo "_BRAILE" e um "_RELEVO_BRAILE", só o
    "_BRAILE" é mantido — o "_RELEVO_BRAILE" costuma ser a folha de
    aprovação da FACA (sem código de peça individual), então dar prioridade
    à data de modificação erraria aqui (o RELEVO às vezes é o mais recente
    dos dois, mas não é o que tem o código).
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

    por_item: dict[str, list[Path]] = {}
    for p in encontrados.values():
        m_item = RE_ITEM_DO_NOME.match(p.stem)
        chave_item = m_item.group(1) if m_item else p.stem
        por_item.setdefault(chave_item, []).append(p)

    resultado: list[Path] = []
    for item, arquivos in por_item.items():
        nao_relevo = [p for p in arquivos if "relevo" not in p.stem.lower()]
        if nao_relevo and len(nao_relevo) < len(arquivos):
            for p in arquivos:
                if p not in nao_relevo:
                    log(f"AVISO: ignorando '{p.name}' (RELEVO) — item {item} já tem um _BRAILE não-relevo.")
            resultado.extend(nao_relevo)
        else:
            resultado.extend(arquivos)
    return resultado


def processar_arquivo(caminho: Path) -> Resultado:
    motivo, codigo, tipo, baixa_confianca = extrair_codigo_peca(caminho)
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
        baixa_confianca=baixa_confianca,
    )

    sinalizador = " [BAIXA CONFIANÇA]" if baixa_confianca else ""
    log(f"[{status}]{sinalizador} {caminho.name}  item={item}  tipo={tipo or '?'}  código={codigo!r}")
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


# Valor default pra cada coluna quando ela não existe no CSV (arquivo
# gerado por uma versão mais antiga do programa, de antes da coluna
# existir — ex.: `tipo` e `baixa_confianca` foram adicionadas depois).
# Sem isso, ler um CSV velho quebra o dashboard inteiro com KeyError na
# primeira linha que faltar a coluna nova (bug real: usuário testou um
# .exe atualizado apontando pra uma pasta com CSV de uma versão anterior).
_PADRAO_POR_COLUNA = {"tipo": "", "baixa_confianca": "False"}


def ler_resultados() -> list[dict]:
    """Lê o CSV completo (usado pelo dashboard). Retorna lista de dicts, mais
    recente primeiro — sempre com todas as colunas atuais preenchidas,
    mesmo que o CSV em disco seja de uma versão mais antiga do programa."""
    if not CSV_FILE.exists():
        return []
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    for linha in linhas:
        for campo in FIELDNAMES:
            if not linha.get(campo):
                linha[campo] = _PADRAO_POR_COLUNA.get(campo, "")
    linhas.reverse()
    return linhas


def _ultimo_status_por_nome() -> dict[str, str]:
    """Pra cada NOME de arquivo já registrado no CSV, o status da última
    passada (linhas mais recentes por último no arquivo, então elas
    vencem). Chave por nome, não caminho completo — mesmo motivo do
    estado (ver `carregar_estado`)."""
    status: dict[str, str] = {}
    if not CSV_FILE.exists():
        return status
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status[row["arquivo"]] = row["status"]
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
    nomes_atuais = {p.name for p in todos}

    a_processar = set(p for p in todos if p.name not in conhecidos)

    if reprocessar_pendentes:
        pendentes = {
            nome for nome, s in _ultimo_status_por_nome().items()
            if s in ("SEM_PECA_CADASTRADA", "ERRO_OCR")
        }
        a_processar |= {p for p in todos if p.name in pendentes}

    resultados: list[Resultado] = []
    for caminho in sorted(a_processar):
        resultados.append(processar_arquivo(caminho))

    if resultados:
        gravar_csv(resultados)

    salvar_estado(nomes_atuais)
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
