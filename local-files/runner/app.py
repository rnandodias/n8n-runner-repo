import os, json, time, re
from unidecode import unidecode
from tqdm import tqdm
from bs4 import BeautifulSoup
from urllib.parse import urlencode
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# --------- helpers (adicione suas regras reais) ----------
def gerar_codigo_cursos(nome_curso: str) -> str:
    """
    Transforma o nome do curso em um código necessário para o cadastramento do curso na plataforma da Alura
    """
    # Remove acentos
    nome = unidecode(nome_curso)
    # Minúsculo
    nome = nome.lower()
    # Remove caracteres especiais exceto espaço
    nome = re.sub(r'[^a-z0-9 ]', '', nome)
    # Substitui espaços por -
    codigo = re.sub(r'\s+', '-', nome).strip('-')
    return codigo
# =============================================================================================

def rolar_e_coletar_vagas(page, container_locator, max_rolagens=30, pausa=1.0):
    vagas_coletadas = set()

    for _ in range(max_rolagens):
        # rola o container com o handle (sem reconstruir seletor)
        container_locator.evaluate("el => el.scrollBy(0, 1000)")
        time.sleep(pausa)

        # pegue links por padrão estável
        # /jobs/view/ é o padrão de detalhe de vaga no LinkedIn
        soup = BeautifulSoup(page.content(), "html.parser")
        novos_links = {
            a["href"].split("?")[0]
            for a in soup.select('a[href^="/jobs/view/"]')
            if "href" in a.attrs
        }

        antes = len(vagas_coletadas)
        vagas_coletadas.update(novos_links)
        if len(vagas_coletadas) == antes:
            break

    return list(vagas_coletadas)
# =============================================================================================

def login_alura(page, user: str, password: str):
    page.goto("https://cursos.alura.com.br/loginForm")
    page.fill("#login-email", user)
    page.fill("#password", password)
    page.click("button.btn-login.btn-principal-form-dark")
    time.sleep(10)
    print("✅ Login realizado com sucesso na Alura.")
# =============================================================================================

def login_linkedin(page, user: str, password: str):
    page.goto("https://www.linkedin.com/checkpoint/lg/sign-in-another-account")
    page.fill("input#username", user)
    page.fill("input#password", password)
    page.click("button[type='submit']")
    time.sleep(10)
    print("✅ Login realizado com sucesso no LinkedIn.")

# --------------------------------------------------------

class PesquisaPayload(BaseModel):
    query: str
    n_vagas: int

class Payload(BaseModel):
    nome_curso: str
    nome_instrutor: str
    tempo_curso: int

app = FastAPI()

# --------------------------------------------------------
# Recurso de teste
# --------------------------------------------------------
@app.get("/ping")
def ping():
    return {"ok": True, "service": "runner"}

# --------------------------------------------------------
# Realizar pesquisa na plataforma do LinkedIn
# --------------------------------------------------------
@app.post("/pesquisa_mercado_linkedin")
def pesquisa_mercado_linkedin(p: PesquisaPayload):
    params = {
        "keywords": p.query,
        "location": "Brasil",
        "start": 0
    }
    user = os.environ.get("LINKEDIN_USER")
    passwd = os.environ.get("LINKEDIN_PASS")

    if not user or not passwd:
        raise HTTPException(status_code=500, detail="Defina LINKEDIN_USER e LINKEDIN_PASS no ambiente do runner.")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-blink-features=AutomationControlled"
                ]
            )

            page = browser.new_page()
            login_linkedin(page, user, passwd)

            links = []
            for i in tqdm(range(0, int(p.n_vagas), 25)):
                params["start"] = i
                page.goto(f"https://www.linkedin.com/jobs/search/?{urlencode(params)}")
                # header_xpath = "//header[contains(@class, 'scaffold-layout__list-header') and contains(@class, 'jobs-search-results-list__header')]"

                # page.wait_for_selector(f"xpath={header_xpath}", timeout=60000)
                # # time.sleep(10)
                # dynamic_div_xpath = f"{header_xpath}/following-sibling::div[1]"
                # dynamic_div = page.locator(f"xpath={dynamic_div_xpath}")
                # dynamic_div.wait_for(state="visible", timeout=60000)
                # # time.sleep(10)
                # class_value = dynamic_div.get_attribute("class").strip()
                # # class_value = "scaffold-layout__list "
                # page.wait_for_selector(f".{class_value}")

                # html = page.content()
                # soup = BeautifulSoup(html, "html.parser")

                # vagas = rolar_e_coletar_vagas(
                #     page,
                #     selector=f".{class_value}",
                #     max_rolagens=10,
                #     pausa=1.5
                # )
                # links = list(dict.fromkeys(links + vagas))

                # 1) aguarda o layout da lista (classe “raiz” estável)
                # Use CSS por parte do nome em vez de XPath por header frágil
                lista = page.locator("div.scaffold-layout__list")
                lista.first.wait_for(state="visible", timeout=60000)

                # 2) às vezes o container que rola é um filho (results list)
                # Tente algo mais específico se existir; senão, use o pai mesmo
                results = page.locator("div.jobs-search-results-list").first
                container = results if results.count() > 0 else lista.first

                # 3) carrega os primeiros itens (evita variações de tempo no Linux headless)
                page.wait_for_selector('a[href^="/jobs/view/"]', timeout=60000)

                # 4) rolar e coletar usando o HANDLE (não string de classe)
                vagas = rolar_e_coletar_vagas(page, container, max_rolagens=10, pausa=1.2)
                links = list(dict.fromkeys(links + vagas))

            print(f"{len(links)} vagas coletadas")
            # with open("output/data/steps/step_00_entendendo_o_mercado_links_vagas_linkedin.json", "w", encoding="utf-8") as f:
            #     json.dump(links, f, indent=2, ensure_ascii=False)

            # descricoes = []
            # error = []
            # for link in tqdm(links):
            #     link = f"https://www.linkedin.com{link}"
            #     try:
            #         page.goto(link, timeout=60000, wait_until="domcontentloaded")
            #         page.wait_for_timeout(2000)
            #         soup = BeautifulSoup(page.content(), "html.parser")
            #     except Exception as e:
            #         error.append(e)
            #         continue

            #     bloco = soup.find("div", id="job-details")
            #     if bloco:
            #         titulo = soup.find("h1", class_="t-24 t-bold inline").get_text()
            #         texto = bloco.get_text()
            #         descricoes.append({
            #             "url": link, 
            #             "titulo": titulo, 
            #             "descricao": texto
            #         })
            #     else:
            #         print(f"Falha ao extrair: {link}")

            page.goto("https://www.linkedin.com/m/logout/")
            page.wait_for_timeout(2000)
            browser.close()

        payload = {"ok": True, "mensagem": "Busca finalizada com sucesso!", "data": links}
        body = json.dumps(payload, ensure_ascii=False)

        return Response(
            content=body,
            media_type="application/json",
            headers={"Connection": "close"}
        )
    
    except PlaywrightTimeout as e:
        page.screenshot(path="/tmp/lnkd-debug.png", full_page=True)
        with open("/tmp/lnkd-debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        raise HTTPException(status_code=500, detail=f"Timeout Playwright: {e}")
    
    except Exception as e:
        page.screenshot(path="/tmp/lnkd-debug.png", full_page=True)
        with open("/tmp/lnkd-debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        raise HTTPException(status_code=500, detail=f"Falha Playwright: {e}")

# --------------------------------------------------------
# Cadastrar um curso na plataforma da Alura
# --------------------------------------------------------
@app.post("/cadastrar_curso")
def cadastrar(p: Payload):
    instrutores_path = "/files/data/instrutores.json"
    if not os.path.exists(instrutores_path):
        raise HTTPException(status_code=500, detail=f"Arquivo não encontrado: {instrutores_path}")

    with open(instrutores_path, "r", encoding="utf-8") as f:
        instrutores = json.load(f)

    autor_valor = next((a["valor"] for a in instrutores if a["nome"] == p.nome_instrutor), None)
    if not autor_valor:
        raise HTTPException(status_code=404, detail="Instrutor não localizado.")

    user = os.environ.get("ALURA_USER")
    passwd = os.environ.get("ALURA_PASS")

    if not user or not passwd:
        raise HTTPException(status_code=500, detail="Defina ALURA_USER e ALURA_PASS no ambiente do runner.")

    code = gerar_codigo_cursos(p.nome_curso)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            login_alura(page, user, passwd)

            page.goto("https://cursos.alura.com.br/admin/v2/newCourse")

            page.fill('input[name="name"]', p.nome_curso)
            page.fill('input[name="code"]', code)
            page.fill('input[name="metaTitle"]', '')
            page.fill('input[name="estimatedTimeToFinish"]', str(int(p.tempo_curso)))
            page.fill('input[name="metadescription"]', 'Será atualizado pelo(a) instrutor(a).')

            # authors select
            page.select_option('select[name="authors"]', value=autor_valor)

            #page.click('form.form-course input[type="submit"]')
            print("Curso cadastrado com sucesso! Apenas um teste")

            browser.close()

        payload = {"ok": True, "mensagem": "Curso cadastrado com sucesso!", "code": code}
        body = json.dumps(payload, ensure_ascii=False)

        return Response(
            content=body,
            media_type="application/json",
            headers={"Connection": "close"}
        )
    
    except PlaywrightTimeout as e:
        raise HTTPException(status_code=500, detail=f"Timeout Playwright: {e}")
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha Playwright: {e}")
