# Vigia Braile

Monitora pastas com folhas de peças (`*_BRAILE*.jpg`), extrai por OCR o
código da peça e classifica cada uma como **OK** ou **SEM PEÇA CADASTRADA**
(peça ainda não fechada — código ausente ou com placeholder tipo
`CBCG618???`).

O código aparece em pelo menos 4 templates visuais diferentes (com/sem
cabeçalho ACCUBRAILLE, com/sem caixa vermelha, texto na horizontal ou na
vertical), mas só em 2 formatos de fundo — o extrator reconhece os dois:

| Tipo | Como aparece | Exemplo |
|---|---|---|
| **COLAGEM** | código `CBCG...`, impresso em **vermelho**, numa caixa com o rótulo "CÓDIGO PEÇA" no canto superior direito | `CBCG500211596` |
| **CORTE_VINCO** | código `CB...` impresso em **preto**, colado à placa/padrão braile (às vezes na vertical) — não confundir com o código **azul** ao lado de "GENERICO FACA", que é o código da FACA (ferramenta), não da peça | `CB17G19A` |

Validado contra **922 arquivos reais de produção** (pasta de amostras, não
versionada): **897 OK (97,3%)**, **13 sem peça cadastrada (1,4%)**, **12
erro de OCR (1,3%)**. Ver "Baixa confiança" e "Limitações conhecidas"
abaixo pra saber quando desconfiar de um resultado.

Duas partes:

- **`vigia_braile.py`** — extrator/CLI. Varre as pastas configuradas,
  roda OCR nos arquivos novos e grava em `vigia_braile_resultado.csv` +
  `vigia_braile.log`. Pensado pra rodar 1-2x/dia via agendador (Task
  Scheduler no Windows, cron/launchd no Mac).
- **`app.py`** — dashboard web (Flask) que lê esse CSV e mostra o que está
  OK, sem peça cadastrada ou com erro de OCR, com filtro/busca e botões pra
  disparar um novo scan ou reprocessar os pendentes.

## Opção 1 — rodar o .exe pronto no Windows (sem instalar nada)

A cada tag `vX.Y.Z` enviada ao repositório, o GitHub Actions compila um
pacote Windows self-contained (Python + Flask + Tesseract, tudo embutido —
ver [`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml)).

1. Baixe `VigiaBraile-windows.zip` da aba **Releases** do repositório (ou de
   **Actions → Build Windows .exe → último run → Artifacts**, se ainda não
   houve tag).
2. Extraia o zip em qualquer pasta.
3. Dê duplo clique em `VigiaBraile.exe`. Ele abre uma janela de log e o
   navegador padrão já aponta pro dashboard (`http://127.0.0.1:5000`).
4. Na **primeira execução**, o programa cria `vigia_braile_config.json` ao
   lado do `.exe` já apontando pra pasta de produção padrão
   (`H:\IMAGENS_E_FACAS`) — não precisa editar nada pra usar. Se precisar
   apontar pra outro lugar, edite esse JSON e clique em **Buscar novos** no
   dashboard (o valor é relido a cada scan, não precisa reiniciar o
   programa).

Não precisa instalar Python, Tesseract nem nada — é só extrair e rodar.

Pra gerar uma nova versão: `git tag v1.0.0 && git push origin v1.0.0` dispara
o build automaticamente.

## Opção 2 — rodar a partir do código (dev/Mac/Linux)

```bash
brew install tesseract tesseract-lang   # macOS; traz o pacote de idioma "por"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
# abre em http://127.0.0.1:5000
```

O botão **Buscar novos** roda o mesmo scan do CLI. **Reprocessar pendentes**
refaz o OCR nos arquivos que ficaram como `SEM_PECA_CADASTRADA`/`ERRO_OCR` —
útil quando o mesmo arquivo é reimpresso depois que a peça é cadastrada.

Rodar só o extrator, sem dashboard: `python3 vigia_braile.py`.

## Configuração

`vigia_braile_config.json` (criado automaticamente ao lado do `.exe`/dos
`.py` na primeira execução, se não existir):

```json
{
  "pastas_raiz": ["H:\\IMAGENS_E_FACAS"],
  "prefixos_item": ["50021", "618"]
}
```

- `pastas_raiz` — pastas raiz varridas recursivamente, relidas a cada scan.
  Padrão de fábrica: `H:\IMAGENS_E_FACAS`.
- `prefixos_item` — só processa arquivos cujo nome começa com um desses
  prefixos; o resto da pasta é ignorado (a pasta tem muito mais itens do
  que a gente usa). Padrão de fábrica: `50021*` e `618*`.
- `AREA_BUSCA_CODIGO` / `FAIXAS_BUSCA_CORTE_VINCO` (em `vigia_braile.py`) —
  regiões onde o extrator procura, respectivamente, a caixa vermelha
  (COLAGEM) e o código junto da placa braile (CORTE_VINCO). São áreas de
  busca generosas, não um recorte fixo, então toleram variação de margem
  entre scans; ajustar só se o layout do template mudar de vez.

## Duplicados

- **Mesmo nome em duas pastas** (ex.: uma cópia antiga em `IMAGENS_ANTIGAS`
  e uma mais nova em `IMAGENS_ATUAIS`) — o scan usa só a versão com **data
  de modificação mais recente**; a outra é ignorada (aviso no log).
- **`_BRAILE` e `_RELEVO_BRAILE` do mesmo item** — só o `_BRAILE`
  (não-relevo) é processado. O `_RELEVO_BRAILE` costuma ser a folha de
  aprovação da FACA (sem código de peça individual), então aqui a
  preferência é sempre pelo não-relevo, **independente de qual dos dois é
  mais recente** — o RELEVO às vezes tem data mais nova, mas não é o que
  tem o código.

## Baixa confiança

A extração CORTE_VINCO tem 3 níveis internos, do mais rápido/confiável pro
mais lento/incerto: (1) localizar a palavra do código direto na região
principal, (2) o mesmo em tiles menores (texto pequeno demais pra região
inteira), (3) **força bruta** — OCR fino direto em cada tile, sem
localizar a palavra antes. Quando um código só é achado no nível 3, a
linha vem marcada **`baixa_confianca = True`** (badge roxo "baixa
confiança" no dashboard, filtro dedicado) — nas 922 amostras isso
aconteceu em 7 arquivos (0,8%). Não é motivo pra desconfiar do status
SEM_PECA_CADASTRADA/OK em si, mas o texto exato do código pode ter ruído
(ex.: dígito a mais/a menos colado do texto vizinho) — vale abrir a
imagem original antes de usar esse código em outro sistema.

## Limitações conhecidas

- `AAAAAA_RELEVO BRAILE.jpg` (espaço em vez de underscore antes de
  "BRAILE") não é reconhecido pelo filtro `*_BRAILE*.jpg`. Se isso
  acontecer com frequência em produção, é provável que seja um typo pontual
  na hora de salvar o arquivo — vale confirmar com quem gera os arquivos.
- Folhas de aprovação de FACA compartilhadas entre vários itens (várias
  peças/SKUs na mesma folha, sem um código de peça braile individual —
  só o código da FACA repetido) caem como `ERRO_OCR`: não tem como saber
  OK/SEM PEÇA CADASTRADA a partir desse tipo de arquivo, então fica pra
  conferência manual. Isso responde por boa parte dos 12 `ERRO_OCR`
  residuais nas 922 amostras.
- Erros típicos de OCR no CORTE_VINCO ficam entre dígitos parecidos
  (O/0, 1/I) — o extrator já tolera essa confusão na primeira letra do
  código, mas pode sobrar ruído no resto (ver "Baixa confiança" acima).
- Imagens muito grandes (>178 milhões de pixels — ex.: scan em DPI alto
  demais por engano) não são mais rejeitadas (`Image.MAX_IMAGE_PIXELS =
  None` em `vigia_braile.py`, desligando a proteção padrão do PIL contra
  "decompression bomb"): seguro aqui porque são sempre arquivos internos
  confiáveis, nunca upload de terceiro.

## Dados gerados (não versionados)

`vigia_braile_config.json`, `vigia_braile_estado.json`, `vigia_braile.log`
e `vigia_braile_resultado.csv` são gerados/editados ao lado do `.exe` (ou
dos `.py` em dev) e ficam de fora do git (ver `.gitignore`) — contêm
caminhos internos da rede da empresa.

`vigia_braile_estado.json` guarda só o **nome** de cada arquivo já
processado (não o caminho completo) — de propósito, pra funcionar igual
não importa em qual pasta/drive as imagens estão montadas. Na prática
isso permite "adiantar" um ambiente novo: gerar `vigia_braile_estado.json`
+ `vigia_braile_resultado.csv` num lote de teste (ou numa rodada manual) e
colocar os dois do lado do `.exe` em produção — o próximo "Buscar novos"
não reprocessa o que já está no estado, só o que for de fato novo. Um CSV
sem o estado correspondente ainda ajuda a enxergar o histórico no
dashboard, mas sozinho não evita reprocessamento — quem decide o que
pular é o `vigia_braile_estado.json`.
