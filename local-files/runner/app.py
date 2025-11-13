import os, json, time, re
from unidecode import unidecode
import unicodedata
from tqdm import tqdm
from bs4 import BeautifulSoup
from urllib.parse import urlencode
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse, Response, FileResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import subprocess
import shutil
import uuid
from pathlib import Path
from typing import List
import requests

# Diretório para arquivos temporários
TEMP_DIR = Path("/tmp/video_processing")
TEMP_DIR.mkdir(exist_ok=True)

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

def remover_emojis_e_simbolos(texto):
    return ''.join(
        c for c in texto
        if not unicodedata.category(c).startswith("So")
        and not unicodedata.category(c).startswith("Sk")
    )

def remover_caracteres_invisiveis(texto):
    invisiveis = [
        '\u200b',  # zero width space
        '\u200c',  # zero width non-joiner
        '\u200d',  # zero width joiner
        '\uFEFF'   # zero width no-break space
    ]
    for c in invisiveis:
        texto = texto.replace(c, '')
    return texto

def limpar_texto(texto):
    texto = texto.strip()
    texto = re.sub(r"\s+", " ", texto)
    texto = remover_caracteres_invisiveis(texto)
    texto = remover_emojis_e_simbolos(texto)
    return texto
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

# =============================================================================================

def criar_video_com_transicoes(
    videos: List[str],
    audio_narracao: str,
    output: str,
    transicao_duracao: float = 0.5,
    transicao_tipo: str = "fade"
):
    """
    Junta vídeos com transições usando FFmpeg
    """
    if len(videos) == 0:
        raise ValueError("Nenhum vídeo fornecido")
    
    if len(videos) == 1:
        # Se só tem 1 vídeo, apenas adiciona o áudio
        cmd = [
            'ffmpeg', '-y',
            '-i', videos[0],
            '-i', audio_narracao,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',
            output
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return
    
    # Construir filtro complexo para xfade
    filter_parts = []
    last_label = "[0:v]"
    
    for i in range(len(videos) - 1):
        next_input = f"[{i+1}:v]"
        out_label = f"[v{i}]" if i < len(videos) - 2 else "[vout]"
        
        # Calcular offset (quando a transição deve começar)
        # Assumindo que cada vídeo tem ~5s, a transição começa 0.5s antes do fim
        offset = (i + 1) * 5 - transicao_duracao
        
        xfade = f"{last_label}{next_input}xfade=transition={transicao_tipo}:duration={transicao_duracao}:offset={offset}{out_label}"
        filter_parts.append(xfade)
        last_label = out_label
    
    # Concatenar áudios dos vídeos
    audio_inputs = ''.join([f"[{i}:a]" for i in range(len(videos))])
    audio_concat = f"{audio_inputs}concat=n={len(videos)}:v=0:a=1[a_video]"
    filter_parts.append(audio_concat)
    
    # Mixar áudio dos vídeos com narração
    audio_mix = "[a_video][{}:a]amix=inputs=2:duration=longest[aout]".format(len(videos))
    filter_parts.append(audio_mix)
    
    filter_complex = ";".join(filter_parts)
    
    # Montar comando FFmpeg
    cmd = ['ffmpeg', '-y']
    
    # Adicionar inputs dos vídeos
    for video in videos:
        cmd.extend(['-i', video])
    
    # Adicionar input do áudio da narração
    cmd.extend(['-i', audio_narracao])
    
    # Adicionar filtro complexo
    cmd.extend([
        '-filter_complex', filter_complex,
        '-map', '[vout]',
        '-map', '[aout]',
        '-c:v', 'libx264',
        '-preset', 'faster',  # mais rápido para processar na VPS
        '-c:a', 'aac',
        '-b:a', '192k',
        '-pix_fmt', 'yuv420p',
        output
    ])
    
    # Executar comando
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise Exception(f"Erro FFmpeg: {result.stderr}")

# =============================================================================================

def cleanup_job(job_dir: Path, delay_seconds: int = 3600):
    """Limpa arquivos temporários após um delay"""
    time.sleep(delay_seconds)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        print(f"🧹 Limpeza realizada: {job_dir}")

# =============================================================================================

def baixar_arquivo(url: str, destino: str):
    """Baixa um arquivo de uma URL"""
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    
    with open(destino, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    
    print(f"✅ Download concluído: {destino}")

# --------------------------------------------------------
class PesquisaPayload(BaseModel):
    query: str
    n_vagas: int

class Payload(BaseModel):
    nome_curso: str
    nome_instrutor: str
    tempo_curso: int

class IDPayload(BaseModel):
    id: str

class VideoURLProcessingPayload(BaseModel):
    video_urls: List[str]  # Lista de URLs dos vídeos
    audio_url: str  # URL do áudio da narração
    transicao_duracao: float = 0.5
    transicao_tipo: str = "fade"

# --------------------------------------------------------

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
@app.post("/processar_video_urls")
async def processar_video_urls(
    payload: VideoURLProcessingPayload,
    background_tasks: BackgroundTasks
):
    """
    Processa vídeos a partir de URLs, adicionando transições e áudio de narração.
    
    Body JSON:
    {
      "video_urls": ["https://exemplo.com/video1.mp4", "https://exemplo.com/video2.mp4"],
      "audio_url": "https://exemplo.com/audio.mp3",
      "transicao_duracao": 0.5,
      "transicao_tipo": "fade"
    }
    """
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        print(f"🎬 Iniciando processamento: {job_id}")
        print(f"📥 Baixando {len(payload.video_urls)} vídeos...")
        
        # Baixar vídeos
        video_paths = []
        for i, url in enumerate(payload.video_urls):
            video_path = job_dir / f"video_{i:03d}.mp4"
            baixar_arquivo(url, str(video_path))
            video_paths.append(str(video_path))
        
        print(f"✅ {len(video_paths)} vídeos baixados")
        
        # Baixar áudio
        print(f"📥 Baixando áudio da narração...")
        audio_path = job_dir / "audio_narracao.mp3"
        baixar_arquivo(payload.audio_url, str(audio_path))
        print(f"✅ Áudio baixado")
        
        # Processar
        output_path = job_dir / "video_final.mp4"
        
        print(f"🔄 Processando vídeo com transições {payload.transicao_tipo}...")
        criar_video_com_transicoes(
            video_paths,
            str(audio_path),
            str(output_path),
            transicao_duracao=payload.transicao_duracao,
            transicao_tipo=payload.transicao_tipo
        )
        
        print(f"✅ Processamento concluído: {output_path}")
        
        # Agendar limpeza após 1 hora
        background_tasks.add_task(cleanup_job, job_dir, 3600)
        
        # Retornar o vídeo
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename=f"video_final_{job_id[:8]}.mp4",
            headers={
                "Content-Disposition": f'attachment; filename="video_final_{job_id[:8]}.mp4"'
            }
        )
    
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro ao baixar arquivos: {str(e)}")
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro ao baixar arquivos: {str(e)}")
    
    except Exception as e:
        print(f"❌ Erro no processamento: {str(e)}")
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro ao processar vídeo: {str(e)}")
    
# --------------------------------------------------------
# Realizar pesquisa na plataforma do LinkedIn
# --------------------------------------------------------
@app.post("/processar_video")
async def processar_video(
    background_tasks: BackgroundTasks,
    videos: List[UploadFile] = File(..., description="Lista de vídeos (5s cada)"),
    audio: UploadFile = File(..., description="Áudio da narração"),
    transicao_duracao: float = 0.5,
    transicao_tipo: str = "fade"
):
    """
    Processa múltiplos vídeos adicionando transições e áudio de narração.
    
    Tipos de transição disponíveis:
    - fade (padrão)
    - wipeleft, wiperight, wipeup, wipedown
    - slideleft, slideright, slideup, slidedown
    - dissolve
    - pixelize
    """
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        print(f"🎬 Iniciando processamento: {job_id}")
        
        # Salvar vídeos recebidos
        video_paths = []
        for i, video in enumerate(videos):
            video_path = job_dir / f"video_{i:03d}.mp4"
            with open(video_path, "wb") as f:
                shutil.copyfileobj(video.file, f)
            video_paths.append(str(video_path))
        
        print(f"✅ {len(video_paths)} vídeos salvos")
        
        # Salvar áudio
        audio_path = job_dir / "audio_narracao.mp3"
        with open(audio_path, "wb") as f:
            shutil.copyfileobj(audio.file, f)
        
        print(f"✅ Áudio de narração salvo")
        
        # Processar
        output_path = job_dir / "video_final.mp4"
        
        print(f"🔄 Processando vídeo com transições {transicao_tipo}...")
        criar_video_com_transicoes(
            video_paths,
            str(audio_path),
            str(output_path),
            transicao_duracao=transicao_duracao,
            transicao_tipo=transicao_tipo
        )
        
        print(f"✅ Processamento concluído: {output_path}")
        
        # Agendar limpeza após 1 hora
        background_tasks.add_task(cleanup_job, job_dir, 3600)
        
        # Retornar o vídeo
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename=f"video_final_{job_id[:8]}.mp4",
            headers={
                "Content-Disposition": f'attachment; filename="video_final_{job_id[:8]}.mp4"'
            }
        )
    
    except Exception as e:
        # Limpar em caso de erro
        print(f"❌ Erro no processamento: {str(e)}")
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro ao processar vídeo: {str(e)}")

# --------------------------------------------------------
# Realizar pesquisa na plataforma do LinkedIn
# --------------------------------------------------------
@app.get("/processar_video/status")
def status_processamento():
    """Retorna informações sobre o serviço de processamento de vídeo"""
    return {
        "ok": True,
        "ffmpeg_disponivel": shutil.which("ffmpeg") is not None,
        "temp_dir": str(TEMP_DIR),
        "transicoes_disponiveis": [
            "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
            "slideleft", "slideright", "slideup", "slidedown",
            "dissolve", "pixelize"
        ]
    }

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
        # page.screenshot(path="/tmp/lnkd-debug.png", full_page=True)
        with open("/tmp/lnkd-debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        raise HTTPException(status_code=500, detail=f"Timeout Playwright: {e}")
    
    except Exception as e:
        # page.screenshot(path="/tmp/lnkd-debug.png", full_page=True)
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

# --------------------------------------------------------
# Obter a transcrição de um curso na plataforma da Alura
# --------------------------------------------------------
@app.post("/get_transcription_course")
def get_transcription_course(p: IDPayload):
    user = os.environ.get("ALURA_USER")
    passwd = os.environ.get("ALURA_PASS")

    if not user or not passwd:
        raise HTTPException(status_code=500, detail="Defina ALURA_USER e ALURA_PASS no ambiente do runner.")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            login_alura(page, user, passwd)

            page.goto(f"https://cursos.alura.com.br/admin/courses/v2/{p.id}")
            link = f"https://cursos.alura.com.br{page.get_attribute('text=Ver curso', 'href')}"

            page.goto(link, timeout=60000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(".courseSectionList", timeout=60000)
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
            except TimeoutError:
                print(f"[AVISO] Timeout em {link}. Pulando...")

            nome = soup.find("h1").strong.get_text()

            videos = []
            for item in soup.find_all("li", class_="courseSection-listItem"):
                aula = f"https://cursos.alura.com.br{item.find('a', class_='courseSectionList-section')['href']}"
                page.goto(aula, timeout=60000, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(".task-menu-sections-select", timeout=60000)
                    html = page.content()
                    soup_section = BeautifulSoup(html, "html.parser")
                    for video in soup_section.find_all("a", class_="task-menu-nav-item-link task-menu-nav-item-link-VIDEO"):
                        videos.append(f"https://cursos.alura.com.br{video['href']}")
                except TimeoutError:
                    print(f"[AVISO] Timeout em {aula}. Pulando...")
                    continue

            transcricoes = []
            for index, video in enumerate(videos):
                page.goto(video, timeout=60000, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector("#transcription", timeout=60000)
                    html = page.content()
                    soup_video = BeautifulSoup(html, "html.parser")
                    title = soup_video.find("h1", class_="task-body-header-title").span.get_text()
                    transcription = soup_video.find("section", id="transcription").get_text()
                    transcription = transcription.replace("Transcrição", f"Vídeo {index + 1} -{title}")
                    # curso[f"transcricao_video_{index + 1}"] = transcription
                    texto_limpo = limpar_texto(transcription)
                    transcricoes.append(texto_limpo)                        
                except TimeoutError:
                    print(f"[AVISO] Timeout em {video}. Pulando...")
                    # curso[f"transcricao_video_{index + 1}"] = None
                    transcricoes.append(None)                        

            browser.close()
            
        payload = {
            "id": p.id,
            "nome": nome,
            "link": link,
            "transcricao": transcricoes
        }
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
