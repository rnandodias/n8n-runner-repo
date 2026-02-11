# Runner Alura - FastAPI + Playwright + LibreOffice

git add -A && git commit -m "Criando testes local" && git push

Servico de automacao para processamento de conteudo, integrado ao n8n via containers Docker.

## Visao Geral

Este projeto fornece um runner FastAPI que combina:
- **Playwright/Chromium** para scraping web e automacao de navegador
- **LibreOffice UNO** para manipulacao avancada de documentos
- **Agentes de IA** (Anthropic Claude e OpenAI GPT) para revisao automatizada de artigos
- **FFmpeg** para processamento de video

Usado como sidecar do n8n para automacoes envolvendo extracao de conteudo, geracao de documentos DOCX, revisao com IA, e processamento de video.

---

## Arquitetura

```
┌─────────────────────────────────────────────────────────────────┐
│                           n8n                                   │
│                    (orquestrador de workflows)                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP (runner:8000)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Runner FastAPI                             │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌───────────┐  │
│  │ Playwright  │ │ LibreOffice │ │  LLM APIs   │ │   FFmpeg  │  │
│  │ (Chromium)  │ │    (UNO)    │ │Claude/GPT   │ │   Video   │  │
│  └─────────────┘ └─────────────┘ └─────────────┘ └───────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Endpoints

### Utilitarios

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/ping` | GET | Health check do servico |

### Extracao e Conversao de Artigos

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/extract-article` | POST | Extrai artigo de URL (scraping Playwright) |
| `/html-to-docx` | POST | Converte URL de artigo para DOCX binario |
| `/generate-docx` | POST | Gera DOCX a partir de JSON estruturado |

### LibreOffice (Manipulacao Avancada)

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/libreoffice/status` | GET | Status do servico LibreOffice UNO |
| `/libreoffice/extrair-texto` | POST | Extrai texto de DOCX via upload |
| `/libreoffice/extrair-texto-url` | POST | Extrai texto de DOCX via URL |
| `/libreoffice/aplicar-revisoes` | POST | Aplica revisoes via LibreOffice |
| `/libreoffice/aplicar-revisoes-json` | POST | Aplica revisoes JSON via LibreOffice |
| `/libreoffice/reset` | POST | Reinicia conexao LibreOffice |

### Revisao com Agentes de IA

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/revisao/extrair-texto` | POST | Extrai texto de DOCX para revisao |
| `/revisao/aplicar` | POST | Aplica revisoes com Track Changes (OOXML) |
| `/revisao/aplicar-json` | POST | Aplica revisoes JSON com Track Changes |
| `/revisao/aplicar-form` | POST | Aplica revisoes via multipart form |
| `/revisao/aplicar-comentarios-form` | POST | Aplica revisoes como comentarios DOCX |
| `/revisao/agente-seo` | POST | Agente de revisao SEO/GEO |
| `/revisao/agente-tecnico` | POST | Agente de revisao tecnica |
| `/revisao/agente-texto` | POST | Agente de revisao textual/didatica |
| `/revisao/agente-seo-form` | POST | Agente SEO via multipart form |
| `/revisao/agente-tecnico-form` | POST | Agente tecnico via multipart form |
| `/revisao/agente-texto-form` | POST | Agente texto via multipart form |
| `/revisao/agente-imagem` | POST | Agente de revisao de imagens (visao multimodal) |
| `/revisao/agente-imagem-form` | POST | Agente imagem via multipart form |

### Processamento de Video

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/processar_video_urls` | POST | Processa video a partir de URLs |
| `/processar_video` | POST | Processa video com upload binario |
| `/processar_video/status` | GET | Status do processamento de video |

### Integracoes Externas

| Endpoint | Metodo | Descricao |
|----------|--------|-----------|
| `/pesquisa_mercado_linkedin` | POST | Pesquisa de vagas no LinkedIn |
| `/cadastrar_curso` | POST | Cadastra curso na plataforma Alura |
| `/get_transcription_course` | POST | Obtem transcricao de curso Alura |

---

## Agentes de Revisao de Artigos

O sistema inclui quatro agentes especializados de IA para revisao de artigos:

### Agente SEO
- Analisa intencao de busca e resposta do conteudo
- Avalia distribuicao de palavras-chave (densidade 5-8%)
- Verifica estrutura de titulos e escaneabilidade
- Sugere links internos/externos
- Recomenda CTAs estrategicos

### Agente Tecnico
- Valida correcao e atualizacao de informacoes
- Verifica versoes de bibliotecas/frameworks (com busca web)
- Avalia exemplos de codigo e boas praticas
- Identifica recursos deprecados ou problemas de seguranca
- Sugere referencias e evidencias

### Agente Texto
- Melhora clareza, didatica e fluidez
- Corrige gramatica e ortografia (PT-BR)
- Avalia progressao logica e transicoes
- Sugere ajustes de tom e nivel do publico
- Recomenda listas, tabelas e elementos visuais

### Agente Imagem
- Analisa relevancia e contexto das imagens
- Verifica qualidade e legibilidade de screenshots
- Detecta interfaces desatualizadas (com busca web)
- Avalia alt text para acessibilidade
- Identifica textos presos em imagens que deveriam estar no artigo
- Sugere onde adicionar imagens faltantes
- Usa visao multimodal (Claude Vision ou GPT-4 Vision)

**Nota:** O agente de imagem requer `url_artigo` para extrair as imagens via scraping.
Com Anthropic, usa visao + busca web. Com OpenAI, usa apenas visao.

### Formato de Saida

Todos os agentes retornam JSON estruturado:

```json
[
  {
    "tipo": "SEO|TECNICO|TEXTO|IMAGEM",
    "acao": "substituir|deletar|inserir|comentario",
    "texto_original": "texto exato encontrado no documento",
    "texto_novo": "texto substituto",
    "justificativa": "explicacao clara da mudanca"
  }
]
```

---

## Estrutura do Projeto

```
n8n-runner-repo/
├── .github/
│   └── workflows/
│       └── deploy-runner.yml      # CI/CD para VPS
├── local-files/
│   └── runner/
│       ├── app.py                 # Aplicacao FastAPI principal
│       ├── llm_client.py          # Cliente unificado LLM (Anthropic/OpenAI)
│       ├── prompts_revisao.py     # Prompts dos agentes de revisao
│       └── track_changes.py       # Implementacao OOXML Track Changes
├── n8n-runner/
│   ├── docker-compose.yml         # Compose do runner
│   └── runner/
│       ├── Dockerfile             # Playwright + FFmpeg + LibreOffice
│       ├── requirements.txt       # Dependencias Python
│       └── start.sh               # Script de inicializacao
├── workflows/
│   └── *.json                     # Workflows n8n exportados
├── ENV.EXAMPLE.txt                # Template de variaveis
└── README.md
```

---

## Configuracao

### Variaveis de Ambiente

Criar `/opt/n8n-runner/.env` na VPS:

```env
# Credenciais Alura
ALURA_USER=seu_usuario
ALURA_PASS=sua_senha

# Credenciais LinkedIn (opcional)
LINKEDIN_USER=seu_email
LINKEDIN_PASS=sua_senha

# APIs de LLM
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Provider padrao (anthropic ou openai)
LLM_PROVIDER=anthropic

# Modelo padrao (opcional)
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
OPENAI_MODEL=gpt-4.1

# HTTPS via Traefik (opcional)
RUNNER_SUBDOMAIN=runner
DOMAIN_NAME=seu-dominio.com.br
TRAEFIK_NETWORK=root_default
```

---

## Deploy

### Pre-requisitos na VPS

1. Docker + Docker Compose instalados
2. Criar diretorios:
   ```bash
   sudo mkdir -p /opt/n8n-runner/runner
   sudo mkdir -p /local-files/{runner,data}
   ```

### GitHub Actions (CI/CD automatico)

1. Gerar chave SSH:
   ```bash
   ssh-keygen -t ed25519 -C "gh-actions" -f ~/.ssh/id_ed25519_gh
   ```

2. Adicionar chave publica na VPS:
   ```bash
   cat ~/.ssh/id_ed25519_gh.pub | ssh root@VPS_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

3. Configurar Secrets no GitHub:
   - `VPS_HOST` - IP/hostname da VPS
   - `VPS_PORT` - Porta SSH (padrao 22)
   - `VPS_USER` - Usuario SSH
   - `SSH_PRIVATE_KEY` - Conteudo da chave privada

4. Push para `main` aciona deploy automatico

### Deploy Manual

```bash
cd /opt/n8n-runner
docker compose --env-file .env build runner
docker compose --env-file .env up -d runner
docker compose --env-file .env logs -f runner
```

---

## Workflow n8n - Revisao de Artigos

O projeto inclui workflow n8n para revisao automatizada:

```
[Trigger] → [Config] → [HTML to DOCX] → [Agentes IA em paralelo] → [Merge] → [Aplicar Comentarios] → [Output]
                              ↓
                      ┌───────┴───────┐
                      ↓       ↓       ↓
                   [SEO] [Tecnico] [Texto]
```

### Fluxo:
1. **Input**: URL do artigo + palavras-chave (opcional)
2. **Conversao**: HTML do artigo vira DOCX
3. **Revisao**: Tres agentes rodam em paralelo
4. **Merge**: Combina todas as sugestoes
5. **Output**: DOCX com comentarios aplicados

### Importar Workflow

1. Acesse n8n → Settings → Import Workflow
2. Cole o JSON de `workflows/Revisão de Artigo - Agentes Paralelos com SEO.json`
3. Configure credenciais (Google Drive, se usado)

---

## Uso

### Interno (de dentro do container n8n)

```bash
curl -X POST http://runner:8000/extract-article \
  -H "Content-Type: application/json" \
  -d '{"url": "https://exemplo.com/artigo"}'
```

### HTTPS (com Traefik)

```bash
curl -X POST https://runner.seu-dominio.com.br/html-to-docx \
  -H "Content-Type: application/json" \
  -d '{"url": "https://exemplo.com/artigo"}'
```

### Exemplo: Revisao SEO

```bash
curl -X POST http://runner:8000/revisao/agente-seo-form \
  -F "file=@artigo.docx" \
  -F "palavras_chave=python, machine learning, ia" \
  -F "provider=anthropic"
```

### Exemplo: Aplicar Comentarios

```bash
curl -X POST http://runner:8000/revisao/aplicar-comentarios-form \
  -F "file=@artigo.docx" \
  -F 'revisoes=[{"tipo":"SEO","acao":"substituir","texto_original":"texto antigo","texto_novo":"texto novo","justificativa":"melhora SEO"}]'
```

---

## Debug

```bash
# Logs do container
docker compose --env-file .env logs -f runner

# Acesso ao container
docker exec -it $(docker ps --format '{{.Names}}' | grep runner | head -n1) bash

# Testar conectividade (de dentro do n8n)
docker exec -it $(docker ps --format '{{.Names}}' | grep n8n | head -n1) \
  sh -lc "curl -i http://runner:8000/ping"
```

---

## Dependencias Principais

- **FastAPI** - Framework web
- **Playwright** - Automacao de browser
- **python-docx** - Geracao de DOCX
- **python-uno** - LibreOffice UNO bridge
- **anthropic** - SDK Anthropic Claude
- **openai** - SDK OpenAI
- **BeautifulSoup4** - Parsing HTML
- **FFmpeg** - Processamento de video

---

## Notas Tecnicas

### Track Changes OOXML

O sistema implementa Track Changes nativo OOXML (sem depender de LibreOffice):
- Manipulacao direta de `document.xml`
- Suporte a insercoes, delecoes e modificacoes
- Preservacao de formatacao original

### Comentarios DOCX

Comentarios sao inseridos com:
- Ranges sobrepostos para multiplos comentarios no mesmo trecho
- Formatacao visual com emojis por tipo (SEO, TECNICO, TEXTO)
- Estrutura multi-paragrafo para corpo do comentario

### Busca Web (Agente Tecnico)

O agente tecnico usa `web_search` da Anthropic para verificar:
- Versoes atuais de bibliotecas/frameworks
- Documentacao oficial atualizada
- Validade de informacoes tecnicas

---

## Licenca

Projeto interno Alura.
