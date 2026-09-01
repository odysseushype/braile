# Vigia Braile

Monitora pastas com folhas de peças (`*_BRAILE*.jpg`), extrai por OCR o
"CÓDIGO PEÇA" impresso na etiqueta e classifica cada peça como **OK** ou
**SEM PEÇA CADASTRADA** (código ausente ou com placeholder tipo `CBCG618???`).

Duas partes:

- **`vigia_braile.py`** — extrator/CLI. Varre as pastas configuradas,
  roda OCR nos arquivos novos e grava em `vigia_braile_resultado.csv` +
  `vigia_braile.log`. Pensado pra rodar 1-2x/dia via agendador (Task
  Scheduler no Windows, cron/launchd no Mac).
- **`app.py`** — dashboard web (Flask) que lê esse CSV e mostra o que está
  OK, sem peça cadastrada ou com erro de OCR, com filtro/busca e botões pra
  disparar um novo scan ou reprocessar os pendentes.

## Setup

```bash
brew install tesseract tesseract-lang   # macOS; traz o pacote de idioma "por"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Rodar o dashboard

```bash
python3 app.py
# abre em http://127.0.0.1:5000
```

O botão **Buscar novos** roda o mesmo scan do CLI. **Reprocessar pendentes**
refaz o OCR nos arquivos que ficaram como `SEM_PECA_CADASTRADA`/`ERRO_OCR` —
útil quando o mesmo arquivo é reimpresso depois que a peça é cadastrada.

## Rodar só o extrator (sem dashboard)

```bash
python3 vigia_braile.py
```

## Configuração

Em `vigia_braile.py`:

- `PASTAS_RAIZ` — pastas raiz varridas recursivamente. Em produção
  (Windows, com `H:` mapeado): `[Path(r"H:\IMAGENS_E_FACAS")]`.
- `CROP_LABEL` — recorte (em % da imagem) onde fica o rótulo "CÓDIGO PEÇA".
  Calibrado para o template atual (2121x1349px); recalibrar se o layout
  mudar.

## Dados gerados (não versionados)

`vigia_braile_estado.json`, `vigia_braile.log` e
`vigia_braile_resultado.csv` são gerados em tempo de execução e ficam de
fora do git (ver `.gitignore`) — contêm caminhos internos da rede da
empresa.
