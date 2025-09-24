import os, json, time, re
from unidecode import unidecode
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

def login_alura(page, user: str, password: str):
    page.goto("https://cursos.alura.com.br/loginForm")
    page.fill("#login-email", user)
    page.fill("#password", password)
    page.click("button.btn-login.btn-principal-form-dark")
    time.sleep(10)
    print("✅ Login realizado com sucesso.")

# --------------------------------------------------------

class Payload(BaseModel):
    nome_curso: str
    nome_instrutor: str
    tempo_curso: int

app = FastAPI()

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
            browser = pw.chromium.launch(headless=True, args=["--start-maximized"])
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
