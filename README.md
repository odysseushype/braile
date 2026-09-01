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
   lado do `.exe`. Edite esse arquivo pra apontar pras pastas de produção,
   por exemplo:
   ```json
   { "pastas_raiz": ["H:\\IMAGENS_E_FACAS"] }
   ```
   e clique em **Buscar novos** no dashboard (não precisa reiniciar o
   programa, o valor é lido a cada scan).

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
{ "pastas_raiz": ["H:\\IMAGENS_E_FACAS"] }
```

- `pastas_raiz` — pastas raiz varridas recursivamente. Padrão de teste:
  `~/vigia_braile_teste`.
- `CROP_LABEL` (em `vigia_braile.py`) — recorte (em % da imagem) onde fica
  o rótulo "CÓDIGO PEÇA". Calibrado para o template atual (2121x1349px);
  recalibrar se o layout mudar.

## Dados gerados (não versionados)

`vigia_braile_config.json`, `vigia_braile_estado.json`, `vigia_braile.log`
e `vigia_braile_resultado.csv` são gerados/editados ao lado do `.exe` (ou
dos `.py` em dev) e ficam de fora do git (ver `.gitignore`) — contêm
caminhos internos da rede da empresa.
