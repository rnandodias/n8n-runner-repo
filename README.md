# Runner Playwright para n8n (Deploy automático via GitHub Actions)

## Subindo Alterações

git add -A
git commit -m "Ajustando app.py na tag a"
git push

OU

git add -A && git commit -m "Adiciona validators Pydantic e null checks defensivos" && git push

---

Este repo contém um serviço **runner** (FastAPI + Playwright/Chromium) usado como sidecar do seu **n8n**.
Deploy: **git push → GitHub Actions → (re)build + up** do runner na sua VPS.

## Estrutura

```bash
n8n-runner/
  runner/
    Dockerfile
  docker-compose.yml
local-files/
  runner/
    app.py
  data/
    instrutores.json (opcional, pode ficar só na VPS)
.github/workflows/
  deploy-runner.yml
```

## Pré-requisitos na VPS (uma vez)

- Docker + Docker Compose
- Pastas:

  ```bash
  sudo mkdir -p /opt/n8n-runner/runner
  sudo mkdir -p /local-files/{runner,data}
  ```

- **.env da VPS**: `/opt/n8n-runner/.env`

  ```env
  ALURA_USER=seu_usuario_alura
  ALURA_PASS=sua_senha_alura
  # (HTTPS opcional via Traefik)
  RUNNER_SUBDOMAIN=runner
  DOMAIN_NAME=seu-dominio.com.br
  TRAEFIK_NETWORK=root_default
  ```

  > Use a **mesma rede** do seu Traefik/n8n principal. Já deixamos `root_default` preenchido no compose.

- (Opcional) `instrutores.json` inicial em `/local-files/data/instrutores.json`:

  ```json
  [
    {"nome": "Fulano da Silva", "valor": "123..."},
    {"nome": "Ciclana Souza",  "valor": "211..."}
  ]
  ```

## GitHub Actions → VPS

1. Gere uma chave SSH local (ou use uma dedicada):

   ```bash
   ssh-keygen -t ed25519 -C "gh-actions@seu-dominio" -f ~/.ssh/id_ed25519_gh
   ```

   Adicione a **pública** na VPS (usuário com permissão):

   ```bash
   cat ~/.ssh/id_ed25519_gh.pub | ssh root@SEU_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
   ```

2. No GitHub (repo → Settings → Secrets and variables → Actions), crie os **Secrets**:
   - `VPS_HOST` (IP/host da VPS)
   - `VPS_PORT` (22, ou sua porta)
   - `VPS_USER` (ex.: root)
   - `SSH_PRIVATE_KEY` (conteúdo do `~/.ssh/id_ed25519_gh`)

Ao fazer **push** na **branch `main`** (arquivos em `n8n-runner/**` ou `local-files/**`), o workflow:

- Faz rsync do repositório para a VPS
- `docker compose build runner`
- `docker compose up -d runner`

## Primeira execução manual (opcional)

```bash
cd /opt/n8n-runner
docker compose --env-file /opt/n8n-runner/.env build runner
docker compose --env-file /opt/n8n-runner/.env up -d runner
docker compose --env-file /opt/n8n-runner/.env logs -f runner
```

## Testes

- Dentro do container do n8n principal:

  ```bash
  docker exec -it $(docker ps --format '{{.Names}}' | grep n8n | head -n1)         sh -lc "apk add --no-cache curl || true; curl -i -m 30 http://runner:8000/ping"
  ```

- Se publicar HTTPS (Traefik + DNS):

  ```bash
  curl -i https://runner.seu-dominio.com.br/ping
  ```

## Uso no n8n

- **Interno**: `POST http://runner:8000/cadastrar_curso`
- **HTTPS (Traefik)**: `POST https://runner.seu-dominio.com.br/cadastrar_curso`

Body JSON esperado:

```json
{
  "nome_curso": "Exemplo",
  "nome_instrutor": "Fulano da Silva",
  "tempo_curso": 8
}
```

## O que você ainda precisa preencher

- **/opt/n8n-runner/.env** na VPS:
  - `ALURA_USER`, `ALURA_PASS`
  - (opcional para HTTPS) `RUNNER_SUBDOMAIN`, `DOMAIN_NAME` (DNS deve apontar para a VPS)
- **GitHub Secrets**: `SSH_PRIVATE_KEY`, `VPS_HOST`, `VPS_PORT`, `VPS_USER`
- **instrutores.json**: lista real de instrutores/valores
- (se quiser traduzir de verdade) implementar `traduzir()` no `app.py`

## Debug rápido

```bash
docker compose --env-file /opt/n8n-runner/.env -f /opt/n8n-runner/docker-compose.yml logs -f runner
docker exec -it $(docker ps --format '{{.Names}}' | grep runner | head -n1) bash
```

## Notas

- O runner usa a rede `root_default` (mesma do Traefik/n8n).
- Não expomos portas no host; o n8n acessa via DNS interno `runner:8000`. Para HTTPS público, habilite as labels no compose e configure DNS.
