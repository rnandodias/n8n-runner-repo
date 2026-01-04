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
from typing import List, Optional, Union
import requests
from openai import OpenAI

# Imports para geração de DOCX
import httpx
from io import BytesIO
from docx import Document
from docx.shared import Pt, Inches, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Diretório para arquivos temporários
TEMP_DIR = Path("/tmp/video_processing")
TEMP_DIR.mkdir(exist_ok=True)

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

class LegendaConfig(BaseModel):
    """Configurações customizáveis para legendas - OPCIONAL"""
    font_size: int = 24
    font_name: str = "Arial"
    bold: bool = False
    primary_colour: str = "&HFFFFFF"
    outline_colour: str = "&H000000"
    back_colour: str = "&H80000000"
    outline: int = 2
    shadow: int = 1
    margin_v: int = 30

class VideoURLProcessingPayload(BaseModel):
    video_urls: List[str]
    audio_url: str
    transicao_duracao: float = 0.5
    transicao_tipo: str = "fade"
    output_filename: str = "video_final.mp4"
    adicionar_legendas: bool = False
    estilo_legenda: str = "youtube"
    legenda_config: Optional[LegendaConfig] = None

# --------------------------------------------------------
# Models para geração de DOCX
# --------------------------------------------------------
class TextSegment(BaseModel):
    text: str
    link: Optional[str] = None
    bold: Optional[bool] = False
    italic: Optional[bool] = False

class ListItemSegment(BaseModel):
    text: str
    link: Optional[str] = None
    bold: Optional[bool] = False
    italic: Optional[bool] = False

class ContentItem(BaseModel):
    type: str  # heading, paragraph, list, code, image, table, blockquote
    # Para heading
    level: Optional[int] = None
    text: Optional[str] = None
    # Para paragraph com segments
    segments: Optional[List[TextSegment]] = None
    # Para list
    ordered: Optional[bool] = False
    items: Optional[List] = None  # Pode ser List[str], List[dict], ou ter sublistas
    # Para code
    language: Optional[str] = None
    content: Optional[str] = None
    # Para image
    url: Optional[str] = None
    alt: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    # Para table
    headers: Optional[List[str]] = None
    rows: Optional[List[List[str]]] = None
    # Para blockquote
    cite: Optional[str] = None  # Fonte da citação (opcional)

class ArticleMetadata(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    publishDate: Optional[str] = None

class GenerateDocxPayload(BaseModel):
    metadata: ArticleMetadata
    content: List[ContentItem]
    filename: Optional[str] = "documento.docx"
    base_url: Optional[str] = None

# --------- helpers ----------
def gerar_codigo_cursos(nome_curso: str) -> str:
    nome = unidecode(nome_curso)
    nome = nome.lower()
    nome = re.sub(r'[^a-z0-9 ]', '', nome)
    codigo = re.sub(r'\s+', '-', nome).strip('-')
    return codigo

def rolar_e_coletar_vagas(page, container_locator, max_rolagens=30, pausa=1.0):
    vagas_coletadas = set()
    for _ in range(max_rolagens):
        container_locator.evaluate("el => el.scrollBy(0, 1000)")
        time.sleep(pausa)
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

def remover_emojis_e_simbolos(texto):
    return ''.join(
        c for c in texto
        if not unicodedata.category(c).startswith("So")
        and not unicodedata.category(c).startswith("Sk")
    )

def remover_caracteres_invisiveis(texto):
    invisiveis = ['\u200b', '\u200c', '\u200d', '\uFEFF']
    for c in invisiveis:
        texto = texto.replace(c, '')
    return texto

def limpar_texto(texto):
    texto = texto.strip()
    texto = re.sub(r"\s+", " ", texto)
    texto = remover_caracteres_invisiveis(texto)
    texto = remover_emojis_e_simbolos(texto)
    return texto

def login_alura(page, user: str, password: str):
    page.goto("https://cursos.alura.com.br/loginForm")
    page.fill("#login-email", user)
    page.fill("#password", password)
    page.click("button:has-text('Entrar')")
    time.sleep(10)
    print("✅ Login realizado com sucesso na Alura.")

def login_linkedin(page, user: str, password: str):
    page.goto("https://www.linkedin.com/checkpoint/lg/sign-in-another-account")
    page.fill("input#username", user)
    page.fill("input#password", password)
    page.click("button[type='submit']")
    time.sleep(10)
    print("✅ Login realizado com sucesso no LinkedIn.")

def criar_video_com_transicoes(
    videos: List[str],
    audio_narracao: str,
    output: str,
    transicao_duracao: float = 0.5,
    transicao_tipo: str = "fade",
    legendas_srt: str = None,
    estilo_legenda: str = "youtube",
    legenda_config: LegendaConfig = None
):
    if len(videos) == 0:
        raise ValueError("Nenhum vídeo fornecido")
    
    temp_video_sem_audio = output.replace('.mp4', '_temp.mp4')
    
    try:
        if len(videos) == 1:
            shutil.copy(videos[0], temp_video_sem_audio)
        else:
            print(f"🔄 Juntando {len(videos)} vídeos com transições...")
            filter_parts = []
            last_label = "[0:v]"
            for i in range(len(videos) - 1):
                next_input = f"[{i+1}:v]"
                out_label = f"[v{i}]" if i < len(videos) - 2 else "[vout]"
                offset = (i + 1) * 5 - transicao_duracao
                xfade = f"{last_label}{next_input}xfade=transition={transicao_tipo}:duration={transicao_duracao}:offset={offset}{out_label}"
                filter_parts.append(xfade)
                last_label = out_label
            filter_complex = ";".join(filter_parts)
            cmd = ['ffmpeg', '-y']
            for video in videos:
                cmd.extend(['-i', video])
            cmd.extend([
                '-filter_complex', filter_complex,
                '-map', '[vout]',
                '-c:v', 'libx264',
                '-preset', 'faster',
                '-pix_fmt', 'yuv420p',
                '-an',
                temp_video_sem_audio
            ])
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"Erro ao juntar vídeos: {result.stderr}")
        
        print(f"🔄 Adicionando áudio da narração...")
        
        def get_duration(file_path: str) -> float:
            cmd = [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                file_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return float(result.stdout.strip())
        
        video_duration = get_duration(temp_video_sem_audio)
        audio_duration = get_duration(audio_narracao)
        
        print(f"📊 Duração do vídeo: {video_duration:.2f}s | Áudio: {audio_duration:.2f}s")
        
        estilos_predefinidos = {
            "youtube": (
                "FontName=Arial Black,"
                "FontSize=28,"
                "Bold=1,"
                "PrimaryColour=&HFFFFFF,"
                "OutlineColour=&H000000,"
                "BackColour=&H80000000,"
                "Outline=3,"
                "Shadow=2,"
                "MarginV=40"
            ),
            "discreto": (
                "FontName=Arial,"
                "FontSize=18,"
                "PrimaryColour=&HFFFFFF,"
                "OutlineColour=&H000000,"
                "Outline=1,"
                "MarginV=20"
            )
        }
        
        if estilo_legenda == "custom" and legenda_config:
            print(f"📝 Usando configuração customizada de legenda (FontSize={legenda_config.font_size})")
            style = (
                f"FontName={legenda_config.font_name},"
                f"FontSize={legenda_config.font_size},"
                f"Bold={1 if legenda_config.bold else 0},"
                f"PrimaryColour={legenda_config.primary_colour},"
                f"OutlineColour={legenda_config.outline_colour},"
                f"BackColour={legenda_config.back_colour},"
                f"Outline={legenda_config.outline},"
                f"Shadow={legenda_config.shadow},"
                f"MarginV={legenda_config.margin_v}"
            )
        else:
            style = estilos_predefinidos.get(estilo_legenda, estilos_predefinidos["youtube"])
            print(f"📝 Usando estilo pré-definido: {estilo_legenda}")
        
        if audio_duration > video_duration:
            diff = audio_duration - video_duration
            fade_duration = min(1.0, diff)
            fade_start = video_duration - fade_duration
            print(f"🎬 Adicionando fade out e {diff:.2f}s de tela preta...")
            if legendas_srt:
                print(f"📝 Adicionando legendas ao vídeo...")
                srt_escaped = legendas_srt.replace('\\', '/').replace(':', '\\:')
                filter_complex = (
                    f'[0:v]fade=t=out:st={fade_start}:d={fade_duration},'
                    f'tpad=stop_mode=add:stop_duration={diff}:color=black,'
                    f"subtitles={srt_escaped}:force_style='{style}'[v]"
                )
            else:
                filter_complex = f'[0:v]fade=t=out:st={fade_start}:d={fade_duration},tpad=stop_mode=add:stop_duration={diff}:color=black[v]'
            cmd = [
                'ffmpeg', '-y',
                '-i', temp_video_sem_audio,
                '-i', audio_narracao,
                '-filter_complex', filter_complex,
                '-map', '[v]',
                '-map', '1:a:0',
                '-c:v', 'libx264',
                '-preset', 'faster',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-pix_fmt', 'yuv420p',
                output
            ]
        else:
            print(f"✅ Áudio cabe no vídeo, processando normalmente...")
            if legendas_srt:
                print(f"📝 Adicionando legendas ao vídeo...")
                srt_escaped = legendas_srt.replace('\\', '/').replace(':', '\\:')
                cmd = [
                    'ffmpeg', '-y',
                    '-i', temp_video_sem_audio,
                    '-i', audio_narracao,
                    '-vf', f"subtitles={srt_escaped}:force_style='{style}'",
                    '-c:v', 'libx264',
                    '-preset', 'faster',
                    '-c:a', 'aac',
                    '-b:a', '192k',
                    '-pix_fmt', 'yuv420p',
                    '-shortest',
                    output
                ]
            else:
                cmd = [
                    'ffmpeg', '-y',
                    '-i', temp_video_sem_audio,
                    '-i', audio_narracao,
                    '-c:v', 'copy',
                    '-c:a', 'aac',
                    '-b:a', '192k',
                    '-shortest',
                    output
                ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Erro ao adicionar áudio: {result.stderr}")
        
        print(f"✅ Vídeo processado!")

    finally:
        if os.path.exists(temp_video_sem_audio):
            os.remove(temp_video_sem_audio)

def gerar_legendas_srt(audio_path: str, output_srt: str):
    print(f"🎙️ Transcrevendo áudio com Whisper...")
    try:
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="srt",
                language="pt"
            )
        with open(output_srt, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"✅ Legendas geradas: {output_srt}")
        return output_srt
    except Exception as e:
        print(f"❌ Erro ao gerar legendas: {str(e)}")
        raise Exception(f"Erro ao transcrever áudio: {str(e)}")

def cleanup_job(job_dir: Path, delay_seconds: int = 3600):
    time.sleep(delay_seconds)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        print(f"🧹 Limpeza realizada: {job_dir}")

def baixar_arquivo(url: str, destino: str):
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    with open(destino, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"✅ Download concluído: {destino}")

# =============================================================================================
# Helpers para geração de DOCX
# =============================================================================================

def add_hyperlink(paragraph, text, url):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    color = OxmlElement('w:color')
    color.set(qn('w:val'), '0066CC')
    rPr.append(color)
    underline = OxmlElement('w:u')
    underline.set(qn('w:val'), 'single')
    rPr.append(underline)
    rFonts = OxmlElement('w:rFonts')
    rFonts.set(qn('w:ascii'), 'Arial')
    rFonts.set(qn('w:hAnsi'), 'Arial')
    rPr.append(rFonts)
    sz = OxmlElement('w:sz')
    sz.set(qn('w:val'), '24')
    rPr.append(sz)
    new_run.append(rPr)
    text_elem = OxmlElement('w:t')
    text_elem.text = text
    new_run.append(text_elem)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink

def convert_relative_url(url: str, base_url: str) -> str:
    if not url:
        return url
    if url.startswith('http://') or url.startswith('https://'):
        return url
    if not base_url:
        return url
    try:
        from urllib.parse import urljoin, urlparse
        parsed_base = urlparse(base_url)
        base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
        if url.startswith('/'):
            return base_domain + url
        if url.startswith('../') or url.startswith('./'):
            return urljoin(base_url + '/', url)
        base_path = base_url.rsplit('/', 1)[0] if '/' in parsed_base.path else base_url
        return base_path + '/' + url
    except Exception as e:
        print(f"Erro ao converter URL {url}: {e}")
        return url

def download_image(url: str) -> Optional[BytesIO]:
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return BytesIO(response.content)
    except Exception as e:
        print(f"❌ Erro ao baixar imagem {url}: {e}")
        return None

def get_image_dimensions_from_bytes(image_bytes: BytesIO) -> tuple:
    try:
        from PIL import Image
        image_bytes.seek(0)
        img = Image.open(image_bytes)
        width, height = img.size
        image_bytes.seek(0)
        return width, height
    except:
        return None, None

def set_paragraph_shading(paragraph, color: str):
    shading = OxmlElement('w:shd')
    shading.set(qn('w:fill'), color)
    paragraph._p.get_or_add_pPr().append(shading)

def add_left_border(paragraph, color: str = '0066CC', width: int = 24):
    """Adiciona borda esquerda ao parágrafo (para blockquotes)"""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    left = OxmlElement('w:left')
    left.set(qn('w:val'), 'single')
    left.set(qn('w:sz'), str(width))
    left.set(qn('w:space'), '4')
    left.set(qn('w:color'), color)
    pBdr.append(left)
    pPr.append(pBdr)

def process_list_item_content(doc, li, paragraph):
    """
    Processa o conteúdo de um item de lista, adicionando ao parágrafo.
    """
    if isinstance(li, dict):
        if 'segments' in li and li['segments']:
            for seg in li['segments']:
                seg_text = seg.get('text', '')
                seg_link = seg.get('link')
                seg_bold = seg.get('bold', False)
                seg_italic = seg.get('italic', False)
                
                if seg_link:
                    add_hyperlink(paragraph, seg_text, seg_link)
                else:
                    run = paragraph.add_run(seg_text)
                    run.font.name = 'Arial'
                    run.font.size = Pt(12)
                    if seg_bold:
                        run.bold = True
                    if seg_italic:
                        run.italic = True
        elif 'text' in li:
            run = paragraph.add_run(li['text'])
            run.font.name = 'Arial'
            run.font.size = Pt(12)
    else:
        run = paragraph.add_run(str(li))
        run.font.name = 'Arial'
        run.font.size = Pt(12)

def process_nested_list(doc, items, ordered=False, indent_level=0):
    """
    Processa uma lista, incluindo sublistas aninhadas.
    """
    markers = ["• ", "◦ ", "▪ ", "- "]
    
    for idx, li in enumerate(items):
        # Cria o parágrafo do item
        list_para = doc.add_paragraph()
        list_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        # Define o prefixo
        if ordered:
            prefix = f"{idx + 1}. "
        else:
            prefix = markers[min(indent_level, len(markers) - 1)]
        
        # Adiciona o prefixo
        prefix_run = list_para.add_run(prefix)
        prefix_run.font.name = 'Arial'
        prefix_run.font.size = Pt(12)
        
        # Processa o conteúdo do item
        process_list_item_content(doc, li, list_para)
        
        # Aplica indentação baseada no nível
        base_indent = 0.5
        list_para.paragraph_format.left_indent = Inches(base_indent + (indent_level * 0.3))
        list_para.space_after = Pt(3)
        
        # Verifica se o item tem uma sublista
        if isinstance(li, dict) and 'sublist' in li and li['sublist']:
            sublist = li['sublist']
            sub_ordered = sublist.get('ordered', False)
            sub_items = sublist.get('items', [])
            if sub_items:
                process_nested_list(doc, sub_items, sub_ordered, indent_level + 1)

# --------------------------------------------------------

app = FastAPI()

# --------------------------------------------------------
@app.get("/ping")
def ping():
    return {"ok": True, "service": "runner"}

# --------------------------------------------------------
@app.post("/generate-docx")
async def generate_docx(payload: GenerateDocxPayload):
    """
    Gera um documento Word (.docx) a partir de JSON estruturado.
    
    Suporta:
    - Títulos e metadados (autor, data)
    - Headings (h2, h3, h4)
    - Parágrafos com hyperlinks e formatação
    - Listas (ordenadas e não-ordenadas, incluindo aninhadas)
    - Blocos de código
    - Imagens (baixadas automaticamente)
    - Tabelas
    - Citações (blockquote)
    """
    try:
        print(f"📝 Gerando DOCX: {payload.metadata.title or 'Sem título'}")
        
        doc = Document()
        
        style = doc.styles['Normal']
        style.font.name = 'Arial'
        style.font.size = Pt(12)
        
        # TÍTULO
        if payload.metadata.title:
            title_para = doc.add_paragraph()
            title_run = title_para.add_run(payload.metadata.title)
            title_run.bold = True
            title_run.font.size = Pt(28)
            title_run.font.name = 'Arial'
            title_para.alignment = WD_ALIGN_PARAGRAPH.LEFT
            title_para.space_after = Pt(6)
        
        # METADADOS
        meta_parts = []
        if payload.metadata.author:
            meta_parts.append(f"Por {payload.metadata.author}")
        if payload.metadata.publishDate:
            meta_parts.append(payload.metadata.publishDate)
        
        if meta_parts:
            meta_para = doc.add_paragraph()
            meta_run = meta_para.add_run(" • ".join(meta_parts))
            meta_run.italic = True
            meta_run.font.size = Pt(11)
            meta_run.font.color.rgb = RGBColor(102, 102, 102)
            meta_para.space_after = Pt(12)
        
        doc.add_paragraph("_" * 80)
        
        # PROCESSAR CONTEÚDO
        for item in payload.content:
            
            # HEADING
            if item.type == "heading" and item.text:
                heading_para = doc.add_paragraph()
                heading_run = heading_para.add_run(item.text)
                heading_run.bold = True
                heading_run.font.name = 'Arial'
                
                if item.level == 2:
                    heading_run.font.size = Pt(16)
                    heading_run.font.color.rgb = RGBColor(44, 62, 80)
                elif item.level == 3:
                    heading_run.font.size = Pt(14)
                    heading_run.font.color.rgb = RGBColor(52, 73, 94)
                else:
                    heading_run.font.size = Pt(13)
                    heading_run.font.color.rgb = RGBColor(60, 80, 100)
                
                heading_para.space_before = Pt(12)
                heading_para.space_after = Pt(6)
            
            # PARAGRAPH
            elif item.type == "paragraph":
                para = doc.add_paragraph()
                para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                
                if item.segments:
                    for seg in item.segments:
                        if seg.link:
                            add_hyperlink(para, seg.text, seg.link)
                        else:
                            run = para.add_run(seg.text)
                            run.font.name = 'Arial'
                            run.font.size = Pt(12)
                            if seg.bold:
                                run.bold = True
                            if seg.italic:
                                run.italic = True
                elif item.text:
                    run = para.add_run(item.text)
                    run.font.name = 'Arial'
                    run.font.size = Pt(12)
                
                para.space_after = Pt(6)
            
            # LIST (com suporte a aninhamento)
            elif item.type == "list" and item.items:
                process_nested_list(doc, item.items, item.ordered or False, indent_level=0)
                doc.add_paragraph()
            
            # BLOCKQUOTE
            elif item.type == "blockquote":
                print(f"💬 Adicionando citação...")
                
                # Texto da citação
                if item.segments:
                    quote_para = doc.add_paragraph()
                    quote_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                    
                    for seg in item.segments:
                        if seg.link:
                            add_hyperlink(quote_para, seg.text, seg.link)
                        else:
                            run = quote_para.add_run(seg.text)
                            run.font.name = 'Arial'
                            run.font.size = Pt(12)
                            run.italic = True
                            run.font.color.rgb = RGBColor(85, 85, 85)
                            if seg.bold:
                                run.bold = True
                    
                    add_left_border(quote_para, color='0066CC', width=24)
                    quote_para.paragraph_format.left_indent = Inches(0.3)
                    quote_para.space_before = Pt(6)
                    quote_para.space_after = Pt(6)
                    
                elif item.text:
                    quote_para = doc.add_paragraph()
                    quote_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                    
                    run = quote_para.add_run(item.text)
                    run.font.name = 'Arial'
                    run.font.size = Pt(12)
                    run.italic = True
                    run.font.color.rgb = RGBColor(85, 85, 85)
                    
                    add_left_border(quote_para, color='0066CC', width=24)
                    quote_para.paragraph_format.left_indent = Inches(0.3)
                    quote_para.space_before = Pt(6)
                    quote_para.space_after = Pt(6)
                
                # Fonte da citação (se houver)
                if item.cite:
                    cite_para = doc.add_paragraph()
                    cite_run = cite_para.add_run(f"— {item.cite}")
                    cite_run.font.name = 'Arial'
                    cite_run.font.size = Pt(10)
                    cite_run.italic = True
                    cite_run.font.color.rgb = RGBColor(120, 120, 120)
                    cite_para.paragraph_format.left_indent = Inches(0.5)
                    cite_para.space_after = Pt(12)
                else:
                    doc.add_paragraph().space_after = Pt(6)
            
            # CODE
            elif item.type == "code" and item.content:
                if item.language:
                    lang_para = doc.add_paragraph()
                    lang_run = lang_para.add_run(f" {item.language.upper()} ")
                    lang_run.font.name = 'Consolas'
                    lang_run.font.size = Pt(9)
                    lang_run.font.color.rgb = RGBColor(255, 255, 255)
                    set_paragraph_shading(lang_para, '2d2d2d')
                    lang_para.space_after = Pt(0)
                
                for line in item.content.split('\n'):
                    code_para = doc.add_paragraph()
                    code_run = code_para.add_run(line if line else ' ')
                    code_run.font.name = 'Consolas'
                    code_run.font.size = Pt(10)
                    code_run.font.color.rgb = RGBColor(51, 51, 51)
                    set_paragraph_shading(code_para, 'F8F8F8')
                    code_para.paragraph_format.left_indent = Inches(0.2)
                    code_para.space_after = Pt(0)
                    code_para.space_before = Pt(0)
                
                doc.add_paragraph().space_after = Pt(12)
            
            # IMAGE
            elif item.type == "image" and item.url:
                image_url = convert_relative_url(item.url, payload.base_url)
                print(f"🖼️ Baixando imagem: {image_url[:80]}...")
                image_data = download_image(image_url)
                
                if image_data:
                    try:
                        orig_width, orig_height = get_image_dimensions_from_bytes(image_data)
                        max_width_cm = 15
                        
                        if orig_width and orig_height:
                            width_cm = orig_width / 96 * 2.54
                            height_cm = orig_height / 96 * 2.54
                            if width_cm > max_width_cm:
                                ratio = max_width_cm / width_cm
                                width_cm = max_width_cm
                                height_cm = height_cm * ratio
                            img_para = doc.add_paragraph()
                            img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            run = img_para.add_run()
                            run.add_picture(image_data, width=Cm(width_cm))
                        else:
                            img_para = doc.add_paragraph()
                            img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            run = img_para.add_run()
                            run.add_picture(image_data, width=Cm(max_width_cm))
                        
                        img_para.space_after = Pt(6)
                        
                        if item.alt and len(item.alt) > 5:
                            caption_para = doc.add_paragraph()
                            caption_run = caption_para.add_run(item.alt)
                            caption_run.italic = True
                            caption_run.font.size = Pt(10)
                            caption_run.font.color.rgb = RGBColor(102, 102, 102)
                            caption_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            caption_para.space_after = Pt(12)
                        
                        print(f"✅ Imagem adicionada")
                    except Exception as img_error:
                        print(f"❌ Erro ao processar imagem: {img_error}")
                else:
                    print(f"⚠️ Não foi possível baixar a imagem")
            
            # TABLE
            elif item.type == "table" and item.headers and item.rows:
                print(f"📊 Adicionando tabela com {len(item.rows)} linhas...")
                
                num_cols = len(item.headers)
                num_rows = len(item.rows) + 1
                
                table = doc.add_table(rows=num_rows, cols=num_cols)
                table.style = 'Table Grid'
                
                header_row = table.rows[0]
                for idx, header_text in enumerate(item.headers):
                    cell = header_row.cells[idx]
                    cell.text = header_text
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
                            run.font.name = 'Arial'
                            run.font.size = Pt(11)
                    shading = OxmlElement('w:shd')
                    shading.set(qn('w:fill'), 'E0E0E0')
                    cell._tc.get_or_add_tcPr().append(shading)
                
                for row_idx, row_data in enumerate(item.rows):
                    row = table.rows[row_idx + 1]
                    for col_idx, cell_text in enumerate(row_data):
                        if col_idx < num_cols:
                            cell = row.cells[col_idx]
                            cell.text = str(cell_text) if cell_text else ""
                            for paragraph in cell.paragraphs:
                                for run in paragraph.runs:
                                    run.font.name = 'Arial'
                                    run.font.size = Pt(10)
                
                doc.add_paragraph().space_after = Pt(12)
                print(f"✅ Tabela adicionada")
        
        # Salvar documento
        doc_buffer = BytesIO()
        doc.save(doc_buffer)
        doc_buffer.seek(0)
        
        filename = payload.filename
        if not filename.endswith('.docx'):
            filename += '.docx'
        filename = re.sub(r'[^a-zA-Z0-9\s\-_.]', '', filename)
        
        print(f"✅ DOCX gerado: {filename}")
        
        return Response(
            content=doc_buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    
    except Exception as e:
        print(f"❌ Erro ao gerar DOCX: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erro ao gerar DOCX: {str(e)}")

# --------------------------------------------------------
# Demais endpoints (vídeo, LinkedIn, Alura) permanecem iguais
# --------------------------------------------------------

@app.post("/processar_video_urls")
async def processar_video_urls(
    payload: VideoURLProcessingPayload,
    background_tasks: BackgroundTasks
):
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        print(f"🎬 Iniciando processamento: {job_id}")
        print(f"📥 Baixando {len(payload.video_urls)} vídeos...")
        
        video_paths = []
        for i, url in enumerate(payload.video_urls):
            video_path = job_dir / f"video_{i:03d}.mp4"
            baixar_arquivo(url, str(video_path))
            video_paths.append(str(video_path))
        
        print(f"✅ {len(video_paths)} vídeos baixados")
        
        print(f"📥 Baixando áudio da narração...")
        audio_path = job_dir / "audio_narracao.mp3"
        baixar_arquivo(payload.audio_url, str(audio_path))
        print(f"✅ Áudio baixado")
        
        srt_path = None
        if payload.adicionar_legendas:
            srt_path = str(job_dir / "legendas.srt")
            gerar_legendas_srt(str(audio_path), srt_path)
        
        output_path = job_dir / "video_final.mp4"
        
        print(f"🔄 Processando vídeo com transições {payload.transicao_tipo}...")
        criar_video_com_transicoes(
            video_paths,
            str(audio_path),
            str(output_path),
            transicao_duracao=payload.transicao_duracao,
            transicao_tipo=payload.transicao_tipo,
            legendas_srt=srt_path,
            estilo_legenda=payload.estilo_legenda,
            legenda_config=payload.legenda_config
        )
        
        print(f"✅ Processamento concluído: {output_path}")
        
        background_tasks.add_task(cleanup_job, job_dir, 3600)
        
        filename = payload.output_filename if payload.output_filename.endswith('.mp4') else f"{payload.output_filename}.mp4"
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
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

@app.post("/processar_video")
async def processar_video(
    background_tasks: BackgroundTasks,
    videos: List[UploadFile] = File(..., description="Lista de vídeos (5s cada)"),
    audio: UploadFile = File(..., description="Áudio da narração"),
    transicao_duracao: float = 0.5,
    transicao_tipo: str = "fade"
):
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        print(f"🎬 Iniciando processamento: {job_id}")
        
        video_paths = []
        for i, video in enumerate(videos):
            video_path = job_dir / f"video_{i:03d}.mp4"
            with open(video_path, "wb") as f:
                shutil.copyfileobj(video.file, f)
            video_paths.append(str(video_path))
        
        print(f"✅ {len(video_paths)} vídeos salvos")
        
        audio_path = job_dir / "audio_narracao.mp3"
        with open(audio_path, "wb") as f:
            shutil.copyfileobj(audio.file, f)
        
        print(f"✅ Áudio de narração salvo")
        
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
        
        background_tasks.add_task(cleanup_job, job_dir, 3600)
        
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename=f"video_final_{job_id[:8]}.mp4",
            headers={
                "Content-Disposition": f'attachment; filename="video_final_{job_id[:8]}.mp4"'
            }
        )
    
    except Exception as e:
        print(f"❌ Erro no processamento: {str(e)}")
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro ao processar vídeo: {str(e)}")

@app.get("/processar_video/status")
def status_processamento():
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

@app.post("/pesquisa_mercado_linkedin")
def pesquisa_mercado_linkedin(p: PesquisaPayload):
    params = {"keywords": p.query, "location": "Brasil", "start": 0}
    user = os.environ.get("LINKEDIN_USER")
    passwd = os.environ.get("LINKEDIN_PASS")

    if not user or not passwd:
        raise HTTPException(status_code=500, detail="Defina LINKEDIN_USER e LINKEDIN_PASS no ambiente do runner.")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu", "--disable-software-rasterizer", "--disable-blink-features=AutomationControlled"]
            )
            page = browser.new_page()
            login_linkedin(page, user, passwd)
            links = []
            for i in tqdm(range(0, int(p.n_vagas), 25)):
                params["start"] = i
                page.goto(f"https://www.linkedin.com/jobs/search/?{urlencode(params)}")
                lista = page.locator("div.scaffold-layout__list")
                lista.first.wait_for(state="visible", timeout=60000)
                results = page.locator("div.jobs-search-results-list").first
                container = results if results.count() > 0 else lista.first
                page.wait_for_selector('a[href^="/jobs/view/"]', timeout=60000)
                vagas = rolar_e_coletar_vagas(page, container, max_rolagens=10, pausa=1.2)
                links = list(dict.fromkeys(links + vagas))
            print(f"{len(links)} vagas coletadas")
            page.goto("https://www.linkedin.com/m/logout/")
            page.wait_for_timeout(2000)
            browser.close()

        payload = {"ok": True, "mensagem": "Busca finalizada com sucesso!", "data": links}
        body = json.dumps(payload, ensure_ascii=False)
        return Response(content=body, media_type="application/json", headers={"Connection": "close"})
    
    except PlaywrightTimeout as e:
        with open("/tmp/lnkd-debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        raise HTTPException(status_code=500, detail=f"Timeout Playwright: {e}")
    except Exception as e:
        with open("/tmp/lnkd-debug.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        raise HTTPException(status_code=500, detail=f"Falha Playwright: {e}")

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
            page.select_option('select[name="authors"]', value=autor_valor)
            print("Curso cadastrado com sucesso! Apenas um teste")
            browser.close()
        payload = {"ok": True, "mensagem": "Curso cadastrado com sucesso!", "code": code}
        body = json.dumps(payload, ensure_ascii=False)
        return Response(content=body, media_type="application/json", headers={"Connection": "close"})
    except PlaywrightTimeout as e:
        raise HTTPException(status_code=500, detail=f"Timeout Playwright: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha Playwright: {e}")

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
            link = "https://cursos.alura.com.br" + page.locator('a:has-text("Ver curso")').get_attribute('href')
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
                    texto_limpo = limpar_texto(transcription)
                    transcricoes.append(texto_limpo)
                except TimeoutError:
                    print(f"[AVISO] Timeout em {video}. Pulando...")
                    transcricoes.append(None)
            browser.close()
        payload = {"id": p.id, "nome": nome, "link": link, "transcricao": transcricoes}
        body = json.dumps(payload, ensure_ascii=False)
        return Response(content=body, media_type="application/json", headers={"Connection": "close"})
    except PlaywrightTimeout as e:
        raise HTTPException(status_code=500, detail=f"Timeout Playwright: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha Playwright: {e}")