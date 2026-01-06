import os, json, time, re
from unidecode import unidecode
import unicodedata
from tqdm import tqdm
from bs4 import BeautifulSoup, NavigableString
from urllib.parse import urlencode, urljoin, urlparse
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
# Models para requests
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

class ContentItem(BaseModel):
    type: str  # heading, paragraph, list, code, image, table, blockquote, youtube, podcast
    level: Optional[int] = None
    text: Optional[str] = None
    segments: Optional[List[TextSegment]] = None
    ordered: Optional[bool] = False
    items: Optional[List] = None
    language: Optional[str] = None
    content: Optional[str] = None
    url: Optional[str] = None
    alt: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    headers: Optional[List[str]] = None
    rows: Optional[List[List[str]]] = None
    cite: Optional[str] = None

class ArticleMetadata(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    publishDate: Optional[str] = None

class GenerateDocxPayload(BaseModel):
    metadata: ArticleMetadata
    content: List[ContentItem]
    filename: Optional[str] = "documento.docx"
    base_url: Optional[str] = None

class ExtractArticlePayload(BaseModel):
    url: str

# --------------------------------------------------------
# Helpers gerais
# --------------------------------------------------------
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

# =============================================================================================
# EXTRATOR DE ARTIGOS ALURA - BeautifulSoup (SEM IA!)
# 100% determinístico, custo ZERO, ~1-2 segundos por artigo
# =============================================================================================

def is_banner_or_promotional(element):
    """
    Verifica se um elemento é um banner promocional ou propaganda.
    Retorna True se deve ser IGNORADO.
    """
    # Verifica se é ou está dentro de um link promocional
    parent_a = element.find_parent('a') if element.name != 'a' else element
    if parent_a and parent_a.get('href'):
        href = parent_a.get('href', '')
        promo_patterns = [
            '/escola-', '/formacao-', '/planos-', '/curso-online',
            '/empresas', 'cursos.alura.com.br/loginForm',
            'utm_source=blog', 'utm_medium=banner', 'utm_campaign=',
            '/carreiras/', '/pos-tech'
        ]
        for pattern in promo_patterns:
            if pattern in href:
                return True
    
    # Verifica src da imagem
    if element.name == 'img':
        src = element.get('src', '').lower()
        alt = element.get('alt', '').lower()
        
        # Padrões de banner
        if any(x in src for x in ['matricula-escola', 'saiba-mais', 'banner']):
            return True
        if 'banner' in alt:
            return True
    
    return False

def is_site_chrome(element):
    """
    Verifica se um elemento faz parte do "chrome" do site (header, nav, footer, etc.)
    e não do conteúdo do artigo.
    CORRIGIDO v6.2: Inclui seção de autor e botões de compartilhamento
    """
    # Elementos de navegação e estrutura do site
    if element.find_parent(['nav', 'footer', 'aside']):
        return True
    
    # Header do site (não o h1 do artigo)
    parent_header = element.find_parent('header')
    if parent_header:
        # Se o header contém links de navegação, é chrome do site
        if parent_header.find('a', href=lambda x: x and '/carreiras' in x):
            return True
    
    # CORRIGIDO v6.2: Seção de autor no final do artigo
    if element.find_parent(class_=lambda x: x and 'cosmos-author' in str(x)):
        return True
    
    # CORRIGIDO v6.2: Botões de compartilhamento social
    if element.find_parent(class_=lambda x: x and 'social-media' in str(x)):
        return True
    if element.find_parent(class_=lambda x: x and 'cosmos-container-social' in str(x)):
        return True
    
    # CORRIGIDO v6.2: Texto "Compartilhe" isolado
    if element.name == 'p':
        text = element.get_text(strip=True).lower()
        if text == 'compartilhe':
            return True
    
    return False

def is_decorative_element(element):
    """
    Verifica se é um elemento decorativo (ícones, logos, setas, etc.)
    CORRIGIDO v6.3: Não filtra imagens do CDN de conteúdo
    """
    if element.name == 'img':
        src = element.get('src', '').lower()
        alt = element.get('alt', '').lower()
        classes = element.get('class', [])
        
        # CORRIGIDO v6.3: Imagens com classe cosmos-image são SEMPRE conteúdo
        if 'cosmos-image' in classes:
            return False
        
        # CORRIGIDO v6.3: Imagens do CDN de conteúdo são SEMPRE conteúdo
        if 'cdn-wcsm.alura.com.br' in src:
            return False
        
        # Padrões de elementos decorativos do SITE (não de conteúdo)
        decorative_patterns = [
            '/assets/img/header/', '/assets/img/home/', '/assets/img/caelum',
            '/assets/img/footer/', '/assets/img/ecossistema/',
            'arrow-', 'return-', 'icon', 'avatar', 
            'gravatar.com/avatar', 'gnarususercontent.com.br'
        ]
        
        for pattern in decorative_patterns:
            if pattern in src:
                return True
        
        # SVGs do site (não de conteúdo) são geralmente ícones
        if '.svg' in src and '/assets/' in src:
            return True
        
        # Imagens muito pequenas (provavelmente ícones)
        width = element.get('width')
        if width:
            try:
                if int(width) < 50:
                    return True
            except ValueError:
                pass
    
    return False

def is_related_articles_section(element):
    """
    Verifica se estamos na seção "Leia também" ou artigos relacionados
    """
    # Verifica o texto dos headings anteriores
    prev_siblings = element.find_all_previous(['h2', 'h3', 'h4'])
    for sib in prev_siblings[:3]:  # Checa os 3 headings anteriores
        text = sib.get_text(strip=True).lower()
        if any(x in text for x in ['leia também', 'artigos relacionados', 'veja outros artigos']):
            return True
    
    return False

def get_text_preserving_spaces(element):
    """
    Extrai texto de um elemento preservando espaços entre elementos inline.
    Resolve: "Significado da palavra<em>kanban</em>" → "Significado da palavra kanban"
    """
    texts = []
    for child in element.descendants:
        if isinstance(child, NavigableString):
            texts.append(str(child))
    
    # Junta e normaliza espaços
    result = ''.join(texts)
    result = re.sub(r'\s+', ' ', result)
    return result.strip()


def extract_text_with_formatting(element, base_url):
    """
    Extrai texto de um elemento preservando formatação (links, bold, italic).
    Retorna lista de segments.
    CORRIGIDO v6.2: Processa recursivamente <p> dentro de <li>, links dentro de <em>, etc.
    """
    segments = []
    
    for child in element.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text.strip():
                segments.append({"text": text})
        
        elif child.name == 'a':
            href = child.get('href', '')
            text = child.get_text()
            if text.strip():
                # Converte URL relativa para absoluta
                if href and not href.startswith('http') and not href.startswith('#'):
                    href = urljoin(base_url, href)
                segments.append({"text": text, "link": href if href else None})
        
        elif child.name in ['strong', 'b']:
            # CORRIGIDO: Processa conteúdo interno para capturar <em> e <a> dentro
            inner_a = child.find('a')
            inner_em = child.find(['em', 'i'])
            
            if inner_a:
                # Tem link dentro do bold
                href = inner_a.get('href', '')
                if href and not href.startswith('http') and not href.startswith('#'):
                    href = urljoin(base_url, href)
                text = child.get_text()
                if text.strip():
                    segments.append({"text": text, "link": href, "bold": True})
            elif inner_em:
                # Tem itálico dentro do bold - processa recursivamente
                for subchild in child.children:
                    if isinstance(subchild, NavigableString):
                        text = str(subchild)
                        if text.strip():
                            segments.append({"text": text, "bold": True})
                    elif subchild.name in ['em', 'i']:
                        # Bold + Italic
                        em_a = subchild.find('a')
                        if em_a:
                            href = em_a.get('href', '')
                            if href and not href.startswith('http') and not href.startswith('#'):
                                href = urljoin(base_url, href)
                            segments.append({"text": subchild.get_text(), "link": href, "bold": True, "italic": True})
                        else:
                            segments.append({"text": subchild.get_text(), "bold": True, "italic": True})
                    elif subchild.name == 'a':
                        href = subchild.get('href', '')
                        if href and not href.startswith('http') and not href.startswith('#'):
                            href = urljoin(base_url, href)
                        segments.append({"text": subchild.get_text(), "link": href, "bold": True})
            else:
                text = child.get_text()
                if text.strip():
                    segments.append({"text": text, "bold": True})
        
        elif child.name in ['em', 'i']:
            # CORRIGIDO: Verifica se tem link dentro do itálico
            inner_a = child.find('a')
            if inner_a:
                href = inner_a.get('href', '')
                if href and not href.startswith('http') and not href.startswith('#'):
                    href = urljoin(base_url, href)
                text = child.get_text()
                if text.strip():
                    segments.append({"text": text, "link": href, "italic": True})
            else:
                text = child.get_text()
                if text.strip():
                    segments.append({"text": text, "italic": True})
        
        elif child.name == 'code':
            text = child.get_text()
            if text.strip():
                segments.append({"text": f"`{text}`", "bold": True})
        
        elif child.name == 'p':
            # CORRIGIDO v6.2: Processa <p> recursivamente (comum em <li><p>...</p></li>)
            inner_segments = extract_text_with_formatting(child, base_url)
            segments.extend(inner_segments)
        
        elif child.name in ['span', 'mark', 'u']:
            # Processa recursivamente
            inner_segments = extract_text_with_formatting(child, base_url)
            segments.extend(inner_segments)
        
        elif child.name == 'br':
            segments.append({"text": "\n"})
        
        elif child.name in ['sup', 'sub']:
            text = child.get_text()
            if text.strip():
                segments.append({"text": text})
        
        else:
            # Outros elementos: extrai texto simples
            text = child.get_text()
            if text.strip():
                segments.append({"text": text})
    
    return segments

def process_list_items(ul_or_ol, base_url, ordered=False):
    """
    Processa itens de lista, incluindo listas aninhadas.
    CORRIGIDO v6.1: Preserva hyperlinks corretamente em todos os casos.
    """
    items = []
    
    for li in ul_or_ol.find_all('li', recursive=False):
        item = {}
        
        # Verifica se tem sublista
        sublist = li.find(['ul', 'ol'], recursive=False)
        
        if sublist:
            # Remove temporariamente a sublista para processar o texto do li
            sublist_copy = sublist.extract()
            
            # Agora processa o conteúdo do li SEM a sublista
            segments = extract_text_with_formatting(li, base_url)
            
            # Restaura a sublista
            li.append(sublist_copy)
            
            if segments:
                # Verifica se tem formatação
                has_formatting = any(
                    seg.get('link') or seg.get('bold') or seg.get('italic')
                    for seg in segments
                )
                
                if has_formatting:
                    item['segments'] = segments
                elif len(segments) == 1:
                    item['text'] = segments[0].get('text', '').strip()
                else:
                    # Junta múltiplos segments de texto simples
                    item['text'] = ''.join(seg.get('text', '') for seg in segments).strip()
            
            # Processa sublista
            sub_ordered = sublist_copy.name == 'ol'
            sub_items = process_list_items(sublist_copy, base_url, sub_ordered)
            if sub_items:
                item['sublist'] = {
                    'ordered': sub_ordered,
                    'items': sub_items
                }
        else:
            # Sem sublista - processa normalmente com extração completa
            segments = extract_text_with_formatting(li, base_url)
            if segments:
                # Verifica se tem formatação
                has_formatting = any(
                    seg.get('link') or seg.get('bold') or seg.get('italic')
                    for seg in segments
                )
                
                if has_formatting:
                    item['segments'] = segments
                elif len(segments) == 1:
                    item['text'] = segments[0].get('text', '').strip()
                else:
                    # Junta múltiplos segments de texto simples
                    item['text'] = ''.join(seg.get('text', '') for seg in segments).strip()
        
        if item:
            items.append(item)
    
    return items

def extract_table(table_tag):
    """
    Extrai dados de uma tabela HTML.
    """
    headers = []
    rows = []
    
    # Tenta pegar headers do thead
    thead = table_tag.find('thead')
    if thead:
        header_row = thead.find('tr')
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(['th', 'td'])]
    
    # Se não tem thead, pega da primeira linha com th
    if not headers:
        first_row = table_tag.find('tr')
        if first_row:
            ths = first_row.find_all('th')
            if ths:
                headers = [th.get_text(strip=True) for th in ths]
    
    # Pega as linhas de dados
    tbody = table_tag.find('tbody') or table_tag
    for tr in tbody.find_all('tr'):
        # Pula a linha de headers se já processamos
        if tr.find('th') and not rows and headers:
            continue
        
        cells = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
        if cells and any(c for c in cells):
            rows.append(cells)
    
    return headers, rows

def detect_youtube_url(text_or_href):
    """
    Detecta e extrai URL do YouTube de texto ou href.
    """
    patterns = [
        r'(https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+)',
        r'(https?://youtu\.be/[\w-]+)',
        r'(https?://(?:www\.)?youtube\.com/embed/[\w-]+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text_or_href)
        if match:
            return match.group(1)
    
    return None

def detect_podcast_url(text_or_href):
    """
    Detecta URL de podcast (Spotify, Hipsters, etc.)
    """
    podcast_patterns = [
        r'(https?://open\.spotify\.com/[^\s]+)',
        r'(https?://(?:www\.)?hipsters\.tech/[^\s]+)',
        r'(https?://(?:www\.)?hipsters\.network/[^\s]+)',
        r'(spotify:[\w:]+)'  # URI do Spotify
    ]
    
    for pattern in podcast_patterns:
        match = re.search(pattern, text_or_href)
        if match:
            return match.group(1)
    
    return None

def extract_article_content(html: str, base_url: str) -> dict:
    """
    Extrai conteúdo estruturado de um artigo da Alura usando BeautifulSoup.
    100% determinístico, sem IA!
    
    Retorna:
    {
        "metadata": {"title": str, "author": str, "publishDate": str},
        "content": [lista de elementos],
        "filename": str,
        "base_url": str,
        "stats": {contagem por tipo}
    }
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove scripts, styles, e elementos não visíveis
    for tag in soup.find_all(['script', 'style', 'noscript', 'svg', 'iframe']):
        tag.decompose()
    
    metadata = {
        'title': None,
        'author': None,
        'publishDate': None
    }
    content = []
    processed_elements = set()
    
    # ===== EXTRAI METADADOS =====
    
    # Título - primeiro h1 da página
    h1 = soup.find('h1')
    if h1:
        metadata['title'] = h1.get_text(strip=True)
        processed_elements.add(id(h1))
    
    # Autor e Data - geralmente estão próximos um do outro após o h1
    # Padrão Alura: imagem do autor + nome + data
    
    # Procura por padrão de data DD/MM/YYYY
    date_pattern = re.compile(r'\d{2}/\d{2}/\d{4}')
    page_text = soup.get_text()
    date_match = date_pattern.search(page_text)
    if date_match:
        metadata['publishDate'] = date_match.group()
    
    # Procura autor - geralmente está em um container com a imagem do avatar
    # Na Alura, o autor aparece perto do h1 e antes do conteúdo
    author_candidates = []
    
    # Procura por padrões de autor na estrutura
    for img in soup.find_all('img'):
        src = img.get('src', '')
        alt = img.get('alt', '')
        
        # Avatar do gravatar ou imagem de perfil
        if 'gravatar.com' in src or 'gnarususercontent.com.br' in src:
            if alt and len(alt) > 2 and not any(x in alt.lower() for x in ['logo', 'banner', 'alura']):
                author_candidates.append(alt)
    
    if author_candidates:
        metadata['author'] = author_candidates[0]
    
    # ===== LOCALIZA ÁREA DE CONTEÚDO PRINCIPAL =====
    
    # Tenta encontrar o container principal do artigo
    # Na Alura, o conteúdo geralmente vem após o h1 e metadados
    
    main_content = soup.find('body') or soup
    
    # Flag para ignorar conteúdo após "Leia também"
    stop_processing = False
    
    # CORRIGIDO v6.1: Coleta todos os textos de itens de lista para evitar duplicação
    list_item_texts = set()
    for li in main_content.find_all('li'):
        li_text = li.get_text(strip=True)
        if li_text and len(li_text) > 10:  # Só textos significativos
            list_item_texts.add(li_text)
    
    # ===== EXTRAI CONTEÚDO =====
    
    # Processa elementos na ordem em que aparecem
    for element in main_content.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'p', 'ul', 'ol', 
                                           'blockquote', 'pre', 'table', 'img', 'figure']):
        # Pula elementos já processados
        elem_id = id(element)
        if elem_id in processed_elements:
            continue
        processed_elements.add(elem_id)
        
        # Ignora elementos do chrome do site
        if is_site_chrome(element):
            continue
        
        # Ignora banners e promoções
        if is_banner_or_promotional(element):
            continue
        
        # Ignora elementos decorativos
        if is_decorative_element(element):
            continue
        
        # Verifica se chegamos na seção "Leia também"
        if element.name in ['h2', 'h3']:
            text = element.get_text(strip=True).lower()
            if any(x in text for x in ['leia também', 'artigos relacionados', 'veja outros artigos']):
                stop_processing = True
        
        if stop_processing:
            continue
        
        # ===== H1 (já processado como título, pula) =====
        if element.name == 'h1':
            continue
        
        # ===== HEADINGS (h2, h3, h4, h5) =====
        if element.name in ['h2', 'h3', 'h4', 'h5']:
            # CORRIGIDO v6.1: Usa função que preserva espaços entre elementos inline
            text = get_text_preserving_spaces(element)
            if text and len(text) > 1:
                # Ignora headings que são do sumário/TOC
                if element.find_parent(class_=lambda x: x and 'toc' in x.lower() if x else False):
                    continue
                
                level = int(element.name[1])
                content.append({
                    'type': 'heading',
                    'level': level,
                    'text': text
                })
        
        # ===== PARÁGRAFOS =====
        elif element.name == 'p':
            text = element.get_text(strip=True)
            if not text:
                continue
            
            # CORRIGIDO v6.1: Pula se este texto já foi processado como item de lista
            if text in list_item_texts:
                continue
            
            # CORRIGIDO v6.3: Removido tratamento especial de YouTube/Podcast
            # Todos os links são tratados normalmente como hyperlinks
            
            # Parágrafo normal - preserva toda formatação incluindo links
            segments = extract_text_with_formatting(element, base_url)
            if segments:
                # Simplifica se for texto simples
                has_formatting = any(
                    seg.get('link') or seg.get('bold') or seg.get('italic') 
                    for seg in segments
                )
                
                if not has_formatting and len(segments) == 1:
                    content.append({
                        'type': 'paragraph',
                        'text': segments[0].get('text', '').strip()
                    })
                else:
                    content.append({
                        'type': 'paragraph',
                        'segments': segments
                    })
        
        # ===== LISTAS =====
        elif element.name in ['ul', 'ol']:
            # Pula se é sublista (será processada pelo pai)
            if element.find_parent(['ul', 'ol']):
                continue
            
            ordered = element.name == 'ol'
            items = process_list_items(element, base_url, ordered)
            
            if items:
                content.append({
                    'type': 'list',
                    'ordered': ordered,
                    'items': items
                })
        
        # ===== BLOCKQUOTES =====
        elif element.name == 'blockquote':
            # CORRIGIDO v6.3: Removido tratamento especial de YouTube/Podcast
            # Blockquotes são sempre citações com formatação preservada
            
            segments = extract_text_with_formatting(element, base_url)
            
            cite_tag = element.find('cite')
            cite = cite_tag.get_text(strip=True) if cite_tag else None
            
            if segments:
                blockquote_item = {'type': 'blockquote', 'segments': segments}
                if cite:
                    blockquote_item['cite'] = cite
                content.append(blockquote_item)
        
        # ===== CÓDIGO =====
        elif element.name == 'pre':
            code_tag = element.find('code')
            if code_tag:
                code_content = code_tag.get_text()
                
                # Detecta linguagem pela classe
                classes = code_tag.get('class', [])
                language = None
                for cls in classes:
                    if isinstance(cls, str):
                        if cls.startswith('language-'):
                            language = cls.replace('language-', '')
                            break
                        elif cls in ['python', 'javascript', 'java', 'sql', 'bash', 
                                    'html', 'css', 'json', 'typescript', 'jsx', 'ruby',
                                    'go', 'rust', 'php', 'csharp', 'kotlin', 'swift']:
                            language = cls
                            break
                
                content.append({
                    'type': 'code',
                    'language': language,
                    'content': code_content
                })
            else:
                # Pre sem code
                content.append({
                    'type': 'code',
                    'content': element.get_text()
                })
        
        # ===== TABELAS =====
        elif element.name == 'table':
            headers, rows = extract_table(element)
            if headers or rows:
                content.append({
                    'type': 'table',
                    'headers': headers,
                    'rows': rows
                })
        
        # ===== IMAGENS =====
        elif element.name == 'img':
            src = element.get('src', '')
            if not src:
                continue
            
            # Ignora novamente banners (double-check)
            if is_banner_or_promotional(element):
                continue
            
            if is_decorative_element(element):
                continue
            
            # Converte URL relativa
            if not src.startswith('http'):
                src = urljoin(base_url, src)
            
            alt = element.get('alt', '')
            
            # Pega dimensões se disponíveis
            width = element.get('width')
            height = element.get('height')
            
            img_item = {
                'type': 'image',
                'url': src,
                'alt': alt
            }
            
            if width:
                try:
                    img_item['width'] = int(width)
                except:
                    pass
            if height:
                try:
                    img_item['height'] = int(height)
                except:
                    pass
            
            content.append(img_item)
        
        # ===== FIGURE (imagem com caption) =====
        elif element.name == 'figure':
            img = element.find('img')
            if img:
                src = img.get('src', '')
                if not src:
                    continue
                
                if not src.startswith('http'):
                    src = urljoin(base_url, src)
                
                # Pega caption do figcaption
                figcaption = element.find('figcaption')
                alt = figcaption.get_text(strip=True) if figcaption else img.get('alt', '')
                
                content.append({
                    'type': 'image',
                    'url': src,
                    'alt': alt
                })
                
                # Marca como processado para não duplicar
                processed_elements.add(id(img))
    
    # ===== PÓS-PROCESSAMENTO =====
    
    # Remove itens vazios
    content = [item for item in content if item]
    
    # ===== GERA ESTATÍSTICAS =====
    stats = {}
    for item in content:
        item_type = item.get('type', 'unknown')
        stats[item_type] = stats.get(item_type, 0) + 1
    
    # ===== GERA FILENAME =====
    filename = metadata.get('title', 'documento') or 'documento'
    filename = unidecode(filename)  # Remove acentos
    filename = re.sub(r'[^a-zA-Z0-9\s-]', '', filename)
    filename = re.sub(r'\s+', '-', filename).strip('-')
    filename = filename[:80]  # Limita tamanho
    filename = f"{filename}.docx"
    
    return {
        'metadata': metadata,
        'content': content,
        'filename': filename,
        'base_url': base_url,
        'stats': stats
    }

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
    return urljoin(base_url, url)

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
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    left = OxmlElement('w:left')
    left.set(qn('w:val'), 'single')
    left.set(qn('w:sz'), str(width))
    left.set(qn('w:space'), '4')
    left.set(qn('w:color'), color)
    pBdr.append(left)
    pPr.append(pBdr)

def process_list_item_content_docx(doc, li, paragraph):
    """Processa o conteúdo de um item de lista no DOCX."""
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

def process_nested_list_docx(doc, items, ordered=False, indent_level=0):
    """Processa lista aninhada no DOCX."""
    markers = ["• ", "◦ ", "▪ ", "- "]
    
    for idx, li in enumerate(items):
        list_para = doc.add_paragraph()
        list_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        if ordered:
            prefix = f"{idx + 1}. "
        else:
            prefix = markers[min(indent_level, len(markers) - 1)]
        
        prefix_run = list_para.add_run(prefix)
        prefix_run.font.name = 'Arial'
        prefix_run.font.size = Pt(12)
        
        process_list_item_content_docx(doc, li, list_para)
        
        base_indent = 0.5
        list_para.paragraph_format.left_indent = Inches(base_indent + (indent_level * 0.3))
        list_para.space_after = Pt(3)
        
        if isinstance(li, dict) and 'sublist' in li and li['sublist']:
            sublist = li['sublist']
            sub_ordered = sublist.get('ordered', False)
            sub_items = sublist.get('items', [])
            if sub_items:
                process_nested_list_docx(doc, sub_items, sub_ordered, indent_level + 1)

# --------------------------------------------------------
# Video helpers
# --------------------------------------------------------
def criar_video_com_transicoes(videos, audio_narracao, output, transicao_duracao=0.5, transicao_tipo="fade", legendas_srt=None, estilo_legenda="youtube", legenda_config=None):
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
            cmd.extend(['-filter_complex', filter_complex, '-map', '[vout]', '-c:v', 'libx264', '-preset', 'faster', '-pix_fmt', 'yuv420p', '-an', temp_video_sem_audio])
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"Erro ao juntar vídeos: {result.stderr}")
        
        print(f"🔄 Adicionando áudio da narração...")
        
        def get_duration(file_path):
            cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return float(result.stdout.strip())
        
        video_duration = get_duration(temp_video_sem_audio)
        audio_duration = get_duration(audio_narracao)
        
        estilos_predefinidos = {
            "youtube": "FontName=Arial Black,FontSize=28,Bold=1,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BackColour=&H80000000,Outline=3,Shadow=2,MarginV=40",
            "discreto": "FontName=Arial,FontSize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=1,MarginV=20"
        }
        
        if estilo_legenda == "custom" and legenda_config:
            style = f"FontName={legenda_config.font_name},FontSize={legenda_config.font_size},Bold={1 if legenda_config.bold else 0},PrimaryColour={legenda_config.primary_colour},OutlineColour={legenda_config.outline_colour},BackColour={legenda_config.back_colour},Outline={legenda_config.outline},Shadow={legenda_config.shadow},MarginV={legenda_config.margin_v}"
        else:
            style = estilos_predefinidos.get(estilo_legenda, estilos_predefinidos["youtube"])
        
        if audio_duration > video_duration:
            diff = audio_duration - video_duration
            fade_duration = min(1.0, diff)
            fade_start = video_duration - fade_duration
            if legendas_srt:
                srt_escaped = legendas_srt.replace('\\', '/').replace(':', '\\:')
                filter_complex = f"[0:v]fade=t=out:st={fade_start}:d={fade_duration},tpad=stop_mode=add:stop_duration={diff}:color=black,subtitles={srt_escaped}:force_style='{style}'[v]"
            else:
                filter_complex = f'[0:v]fade=t=out:st={fade_start}:d={fade_duration},tpad=stop_mode=add:stop_duration={diff}:color=black[v]'
            cmd = ['ffmpeg', '-y', '-i', temp_video_sem_audio, '-i', audio_narracao, '-filter_complex', filter_complex, '-map', '[v]', '-map', '1:a:0', '-c:v', 'libx264', '-preset', 'faster', '-c:a', 'aac', '-b:a', '192k', '-pix_fmt', 'yuv420p', output]
        else:
            if legendas_srt:
                srt_escaped = legendas_srt.replace('\\', '/').replace(':', '\\:')
                cmd = ['ffmpeg', '-y', '-i', temp_video_sem_audio, '-i', audio_narracao, '-vf', f"subtitles={srt_escaped}:force_style='{style}'", '-c:v', 'libx264', '-preset', 'faster', '-c:a', 'aac', '-b:a', '192k', '-pix_fmt', 'yuv420p', '-shortest', output]
            else:
                cmd = ['ffmpeg', '-y', '-i', temp_video_sem_audio, '-i', audio_narracao, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', output]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Erro ao adicionar áudio: {result.stderr}")
        
        print(f"✅ Vídeo processado!")
    finally:
        if os.path.exists(temp_video_sem_audio):
            os.remove(temp_video_sem_audio)

def gerar_legendas_srt(audio_path, output_srt):
    print(f"🎙️ Transcrevendo áudio com Whisper...")
    try:
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file, response_format="srt", language="pt")
        with open(output_srt, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"✅ Legendas geradas: {output_srt}")
        return output_srt
    except Exception as e:
        raise Exception(f"Erro ao transcrever áudio: {str(e)}")

def cleanup_job(job_dir, delay_seconds=3600):
    time.sleep(delay_seconds)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)

def baixar_arquivo(url, destino):
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    with open(destino, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

# --------------------------------------------------------
# FastAPI App
# --------------------------------------------------------
app = FastAPI()

@app.get("/ping")
def ping():
    return {"ok": True, "service": "runner"}

# --------------------------------------------------------
# ENDPOINT: Extrai artigo com BeautifulSoup (SEM IA!)
# --------------------------------------------------------
@app.post("/extract-article")
async def extract_article(payload: ExtractArticlePayload):
    """
    Extrai conteúdo estruturado de um artigo da Alura usando BeautifulSoup.
    
    ✅ 100% determinístico
    ✅ Custo ZERO (sem IA)
    ✅ ~1-2 segundos por artigo
    
    Retorna JSON compatível com /generate-docx
    """
    try:
        print(f"📥 Extraindo artigo: {payload.url}")
        
        # Fetch do HTML
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(payload.url)
            response.raise_for_status()
            html = response.text
        
        print(f"📄 HTML recebido: {len(html)} bytes")
        
        # Extrai conteúdo
        result = extract_article_content(html, payload.url)
        
        print(f"✅ Extração concluída!")
        print(f"📊 Estatísticas: {result['stats']}")
        print(f"📝 Título: {result['metadata'].get('title', 'N/A')}")
        print(f"👤 Autor: {result['metadata'].get('author', 'N/A')}")
        print(f"📅 Data: {result['metadata'].get('publishDate', 'N/A')}")
        
        return result
    
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Erro ao buscar URL: {str(e)}")
    except Exception as e:
        print(f"❌ Erro na extração: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao extrair artigo: {str(e)}")

# --------------------------------------------------------
# ENDPOINT: Pipeline completo HTML → DOCX
# --------------------------------------------------------
@app.post("/html-to-docx")
async def html_to_docx(payload: ExtractArticlePayload):
    """
    Pipeline completo: extrai artigo e gera DOCX em uma única chamada.
    
    ✅ Sem IA
    ✅ Custo ZERO
    ✅ Retorna arquivo .docx pronto
    """
    try:
        print(f"🚀 Pipeline HTML → DOCX: {payload.url}")
        
        # 1. Fetch HTML
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(payload.url)
            response.raise_for_status()
            html = response.text
        
        # 2. Extrai conteúdo
        article_data = extract_article_content(html, payload.url)
        
        print(f"📊 Extraído: {article_data['stats']}")
        
        # 3. Gera DOCX
        docx_payload = GenerateDocxPayload(
            metadata=ArticleMetadata(**article_data['metadata']),
            content=[ContentItem(**item) for item in article_data['content']],
            filename=article_data['filename'],
            base_url=article_data['base_url']
        )
        
        return await generate_docx(docx_payload)
    
    except Exception as e:
        print(f"❌ Erro no pipeline: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro no pipeline: {str(e)}")

# --------------------------------------------------------
# ENDPOINT: Gera DOCX a partir de JSON estruturado
# --------------------------------------------------------
@app.post("/generate-docx")
async def generate_docx(payload: GenerateDocxPayload):
    """
    Gera um documento Word (.docx) a partir de JSON estruturado.
    """
    try:
        print(f"📝 Gerando DOCX: {payload.metadata.title or 'Sem título'}")
        
        doc = Document()
        
        style = doc.styles['Normal']
        style.font.name = 'Arial'
        style.font.size = Pt(12)
        
        # TÍTULO - CORRIGIDO v6.4: Usa estilo Título 1 do Word + formatação customizada
        if payload.metadata.title:
            title_para = doc.add_heading(payload.metadata.title, level=1)
            
            # Aplica formatação customizada
            for run in title_para.runs:
                run.bold = True
                run.font.size = Pt(28)
                run.font.name = 'Arial'
                run.font.color.rgb = RGBColor(33, 37, 41)
            
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
                # CORRIGIDO v6.3: Adiciona linha extra antes de títulos para melhor separação visual
                spacer = doc.add_paragraph()
                spacer.space_after = Pt(0)
                spacer.space_before = Pt(6)
                
                # CORRIGIDO v6.4: Usa estilos nativos de Título do Word (para navegação e sumário)
                # E aplica formatação customizada por cima
                level = item.level if item.level else 2
                heading_para = doc.add_heading(item.text, level=level)
                
                # Aplica formatação customizada mantendo o estilo do Word
                for run in heading_para.runs:
                    run.bold = True
                    run.font.name = 'Arial'
                    
                    if level == 2:
                        run.font.size = Pt(16)
                        run.font.color.rgb = RGBColor(44, 62, 80)
                    elif level == 3:
                        run.font.size = Pt(14)
                        run.font.color.rgb = RGBColor(52, 73, 94)
                    elif level == 4:
                        run.font.size = Pt(13)
                        run.font.color.rgb = RGBColor(60, 80, 100)
                    else:
                        run.font.size = Pt(12)
                        run.font.color.rgb = RGBColor(70, 90, 110)
                
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
            
            # LIST
            elif item.type == "list" and item.items:
                process_nested_list_docx(doc, item.items, item.ordered or False, indent_level=0)
                doc.add_paragraph()
            
            # BLOCKQUOTE
            elif item.type == "blockquote":
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
                
                if item.cite:
                    cite_para = doc.add_paragraph()
                    cite_run = cite_para.add_run(f"— {item.cite}")
                    cite_run.font.name = 'Arial'
                    cite_run.font.size = Pt(10)
                    cite_run.italic = True
                    cite_run.font.color.rgb = RGBColor(120, 120, 120)
                    cite_para.paragraph_format.left_indent = Inches(0.5)
                    cite_para.space_after = Pt(12)
            
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
            
            # CORRIGIDO v6.3: Removido tratamento especial de YOUTUBE e PODCAST
            # Links de vídeos e podcasts são tratados como links normais nos parágrafos
            
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
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    
    except Exception as e:
        print(f"❌ Erro ao gerar DOCX: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao gerar DOCX: {str(e)}")

# --------------------------------------------------------
# Outros endpoints (vídeo, LinkedIn, Alura)
# --------------------------------------------------------

@app.post("/processar_video_urls")
async def processar_video_urls(payload: VideoURLProcessingPayload, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        video_paths = []
        for i, url in enumerate(payload.video_urls):
            video_path = job_dir / f"video_{i:03d}.mp4"
            baixar_arquivo(url, str(video_path))
            video_paths.append(str(video_path))
        
        audio_path = job_dir / "audio_narracao.mp3"
        baixar_arquivo(payload.audio_url, str(audio_path))
        
        srt_path = None
        if payload.adicionar_legendas:
            srt_path = str(job_dir / "legendas.srt")
            gerar_legendas_srt(str(audio_path), srt_path)
        
        output_path = job_dir / "video_final.mp4"
        
        criar_video_com_transicoes(video_paths, str(audio_path), str(output_path), transicao_duracao=payload.transicao_duracao, transicao_tipo=payload.transicao_tipo, legendas_srt=srt_path, estilo_legenda=payload.estilo_legenda, legenda_config=payload.legenda_config)
        
        background_tasks.add_task(cleanup_job, job_dir, 3600)
        
        filename = payload.output_filename if payload.output_filename.endswith('.mp4') else f"{payload.output_filename}.mp4"
        return FileResponse(path=str(output_path), media_type="video/mp4", filename=filename, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    
    except Exception as e:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro ao processar vídeo: {str(e)}")

@app.post("/processar_video")
async def processar_video(background_tasks: BackgroundTasks, videos: List[UploadFile] = File(...), audio: UploadFile = File(...), transicao_duracao: float = 0.5, transicao_tipo: str = "fade"):
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        video_paths = []
        for i, video in enumerate(videos):
            video_path = job_dir / f"video_{i:03d}.mp4"
            with open(video_path, "wb") as f:
                shutil.copyfileobj(video.file, f)
            video_paths.append(str(video_path))
        
        audio_path = job_dir / "audio_narracao.mp3"
        with open(audio_path, "wb") as f:
            shutil.copyfileobj(audio.file, f)
        
        output_path = job_dir / "video_final.mp4"
        
        criar_video_com_transicoes(video_paths, str(audio_path), str(output_path), transicao_duracao=transicao_duracao, transicao_tipo=transicao_tipo)
        
        background_tasks.add_task(cleanup_job, job_dir, 3600)
        
        return FileResponse(path=str(output_path), media_type="video/mp4", filename=f"video_final_{job_id[:8]}.mp4")
    
    except Exception as e:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro ao processar vídeo: {str(e)}")

@app.get("/processar_video/status")
def status_processamento():
    return {"ok": True, "ffmpeg_disponivel": shutil.which("ffmpeg") is not None, "temp_dir": str(TEMP_DIR), "transicoes_disponiveis": ["fade", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft", "slideright", "slideup", "slidedown", "dissolve", "pixelize"]}

@app.post("/pesquisa_mercado_linkedin")
def pesquisa_mercado_linkedin(p: PesquisaPayload):
    params = {"keywords": p.query, "location": "Brasil", "start": 0}
    user = os.environ.get("LINKEDIN_USER")
    passwd = os.environ.get("LINKEDIN_PASS")
    if not user or not passwd:
        raise HTTPException(status_code=500, detail="Defina LINKEDIN_USER e LINKEDIN_PASS")
    
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = browser.new_page()
            login_linkedin(page, user, passwd)
            links = []
            for i in range(0, int(p.n_vagas), 25):
                params["start"] = i
                page.goto(f"https://www.linkedin.com/jobs/search/?{urlencode(params)}")
                lista = page.locator("div.scaffold-layout__list")
                lista.first.wait_for(state="visible", timeout=60000)
                results = page.locator("div.jobs-search-results-list").first
                container = results if results.count() > 0 else lista.first
                page.wait_for_selector('a[href^="/jobs/view/"]', timeout=60000)
                vagas = rolar_e_coletar_vagas(page, container, max_rolagens=10, pausa=1.2)
                links = list(dict.fromkeys(links + vagas))
            page.goto("https://www.linkedin.com/m/logout/")
            browser.close()
        return Response(content=json.dumps({"ok": True, "data": links}, ensure_ascii=False), media_type="application/json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha: {e}")

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
        raise HTTPException(status_code=500, detail="Defina ALURA_USER e ALURA_PASS")
    code = gerar_codigo_cursos(p.nome_curso)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            login_alura(page, user, passwd)
            page.goto("https://cursos.alura.com.br/admin/v2/newCourse")
            page.fill('input[name="name"]', p.nome_curso)
            page.fill('input[name="code"]', code)
            page.fill('input[name="estimatedTimeToFinish"]', str(int(p.tempo_curso)))
            page.fill('input[name="metadescription"]', 'Será atualizado pelo(a) instrutor(a).')
            page.select_option('select[name="authors"]', value=autor_valor)
            browser.close()
        return Response(content=json.dumps({"ok": True, "code": code}, ensure_ascii=False), media_type="application/json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha: {e}")

@app.post("/get_transcription_course")
def get_transcription_course(p: IDPayload):
    user = os.environ.get("ALURA_USER")
    passwd = os.environ.get("ALURA_PASS")
    if not user or not passwd:
        raise HTTPException(status_code=500, detail="Defina ALURA_USER e ALURA_PASS")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            login_alura(page, user, passwd)
            
            page.goto(f"https://cursos.alura.com.br/admin/courses/v2/{p.id}", timeout=60000, wait_until="networkidle")
            page.wait_for_selector('div.form-group', timeout=60000)
            link_href = page.evaluate('''() => {
                const links = document.querySelectorAll('a.btn-default');
                for (let link of links) {
                    if (link.href.includes('/course/') && link.textContent.includes('Ver curso')) {
                        return link.getAttribute('href');
                    }
                }
                return null;
            }''')

            if not link_href:
                raise Exception("Porra, não achou o link 'Ver curso'")
            link = "https://cursos.alura.com.br" + link_href
            # link = "https://cursos.alura.com.br" + page.locator('a:has-text("Ver curso")').get_attribute('href')

            page.goto(link, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_selector(".courseSectionList", timeout=60000)
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            nome = soup.find("h1").strong.get_text()
            videos = []
            for item in soup.find_all("li", class_="courseSection-listItem"):
                aula = f"https://cursos.alura.com.br{item.find('a', class_='courseSectionList-section')['href']}"
                page.goto(aula, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_selector(".task-menu-sections-select", timeout=60000)
                html = page.content()
                soup_section = BeautifulSoup(html, "html.parser")
                for video in soup_section.find_all("a", class_="task-menu-nav-item-link task-menu-nav-item-link-VIDEO"):
                    videos.append(f"https://cursos.alura.com.br{video['href']}")
            transcricoes = []
            for index, video in enumerate(videos):
                page.goto(video, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_selector("#transcription", timeout=60000)
                html = page.content()
                soup_video = BeautifulSoup(html, "html.parser")
                title = soup_video.find("h1", class_="task-body-header-title").span.get_text()
                transcription = soup_video.find("section", id="transcription").get_text()
                transcription = transcription.replace("Transcrição", f"Vídeo {index + 1} -{title}")
                transcricoes.append(limpar_texto(transcription))
            browser.close()
        return Response(content=json.dumps({"id": p.id, "nome": nome, "link": link, "transcricao": transcricoes}, ensure_ascii=False), media_type="application/json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha: {e}")