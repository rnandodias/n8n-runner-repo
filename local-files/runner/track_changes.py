"""
Modulo para aplicar Track Changes em documentos DOCX.
Usa manipulacao OOXML direta para criar revisoes rastreaveis.

Suporta:
- Texto dividido em multiplos runs (w:r)
- Texto dentro de hyperlinks (w:hyperlink)
- Normalizacao para matching flexivel (bullets, smart quotes, whitespace)
- Pre-processamento de revisoes conflitantes
- Preservacao de formatacao (w:rPr) em insercoes e reconstrucoes
- Preservacao de hyperlinks em trechos nao afetados
"""
import os
import re
import shutil
import tempfile
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from lxml import etree

# Namespaces do Word/OOXML
NAMESPACES = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'xml': 'http://www.w3.org/XML/1998/namespace',
}

W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
XML_NS = '{http://www.w3.org/XML/1998/namespace}'
R_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'

# Caracteres de bullet/lista que LLMs incluem do texto renderizado
# mas que nao existem no XML do DOCX (sao formatacao de paragrafo)
BULLET_CHARS = set('\u2022\u00b7\u25aa\u25b8\u25ba\u25c6\u25c7\u25cb\u25cf\u25a0\u25a1')


# =============================================================================
# FUNCOES DE NORMALIZACAO
# =============================================================================

def normalizar_texto(texto: str) -> str:
    """Normaliza texto para matching flexivel."""
    # Smart quotes -> retas
    texto = texto.replace('\u201c', '"').replace('\u201d', '"')
    texto = texto.replace('\u2018', "'").replace('\u2019', "'")
    # Dashes
    texto = texto.replace('\u2013', '-').replace('\u2014', '-')
    # Espacos especiais
    texto = texto.replace('\u00a0', ' ')
    # Zero-width chars
    texto = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', texto)
    # Colapsar whitespace
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto


def strip_bullets(texto: str) -> str:
    """Remove caracteres de bullet do inicio do texto."""
    texto = texto.lstrip()
    while texto and texto[0] in BULLET_CHARS:
        texto = texto[1:].lstrip()
    return texto


def normalizar_com_mapa(texto: str):
    """
    Normaliza texto e constroi mapeamento de posicoes normalizadas -> originais.
    Retorna (texto_normalizado, mapa) onde mapa[norm_pos] = orig_pos.
    """
    resultado = []
    mapa = []
    prev_space = True

    for orig_idx, ch in enumerate(texto):
        out_ch = ch
        if ch in '\u201c\u201d':
            out_ch = '"'
        elif ch in '\u2018\u2019':
            out_ch = "'"
        elif ch in '\u2013\u2014':
            out_ch = '-'
        elif ch == '\u00a0':
            out_ch = ' '
        elif ch in '\u200b\u200c\u200d\ufeff':
            continue

        if out_ch in ' \t\n\r':
            if not prev_space:
                resultado.append(' ')
                mapa.append(orig_idx)
                prev_space = True
        else:
            resultado.append(out_ch)
            mapa.append(orig_idx)
            prev_space = False

    # Strip leading
    while resultado and resultado[0] == ' ':
        resultado.pop(0)
        mapa.pop(0)
    # Strip trailing
    while resultado and resultado[-1] == ' ':
        resultado.pop()
        mapa.pop()

    return ''.join(resultado), mapa


# =============================================================================
# CLASSE PRINCIPAL
# =============================================================================

class TrackChangesApplicator:
    """Aplica revisoes com Track Changes em documentos DOCX."""

    def __init__(self, input_path: str, output_path: str):
        self.input_path = Path(input_path)
        self.output_path = Path(output_path)

        if not self.input_path.exists():
            raise FileNotFoundError(f"Arquivo nao encontrado: {input_path}")

        self.temp_dir = None
        self.doc_root = None
        self.revision_id = 1
        self.comments = []
        self.resultados = []

    # =========================================================================
    # API PUBLICA
    # =========================================================================

    def aplicar_revisoes(self, revisoes: list, autor: str = "Agente IA Revisor") -> dict:
        """
        Aplica lista de revisoes ao documento.

        Args:
            revisoes: Lista de dicts com as revisoes
            autor: Nome do autor das revisoes

        Returns:
            dict com estatisticas e detalhes
        """
        self.autor = autor
        self.revision_id = 1
        self.comments = []
        self.resultados = []

        # Copia arquivo para output
        shutil.copy(self.input_path, self.output_path)

        # Extrai DOCX para diretorio temporario
        self.temp_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(self.output_path, 'r') as zip_ref:
            zip_ref.extractall(self.temp_dir)

        try:
            # Carrega document.xml
            doc_xml_path = os.path.join(self.temp_dir, 'word', 'document.xml')
            tree = etree.parse(doc_xml_path)
            self.doc_root = tree.getroot()

            # Habilita Track Changes
            self._habilitar_track_changes()

            # Pre-processa revisoes conflitantes
            revisoes = self._pre_processar_revisoes(revisoes)

            # Processa cada revisao
            for idx, rev in enumerate(revisoes):
                try:
                    self._processar_revisao(idx, rev)
                except Exception as e:
                    self.resultados.append({
                        "idx": idx,
                        "ok": False,
                        "erro": f"Excecao: {str(e)}",
                        "revisao": rev
                    })

            # Adiciona comentarios ao documento
            if self.comments:
                self._adicionar_comments()

            # Salva document.xml
            tree.write(doc_xml_path, xml_declaration=True, encoding='UTF-8', standalone=True)

            # Recompacta DOCX
            self._recompactar_docx()

        finally:
            if self.temp_dir:
                shutil.rmtree(self.temp_dir, ignore_errors=True)

        total_ok = sum(1 for r in self.resultados if r.get("ok"))
        total_falhas = sum(1 for r in self.resultados if not r.get("ok"))

        return {
            "arquivo_saida": str(self.output_path),
            "total_revisoes": len(revisoes),
            "aplicadas": total_ok,
            "falhas": total_falhas,
            "comentarios": len(self.comments),
            "detalhes": self.resultados
        }

    # =========================================================================
    # PRE-PROCESSAMENTO DE CONFLITOS
    # =========================================================================

    def _pre_processar_revisoes(self, revisoes: list) -> list:
        """
        Trata revisoes conflitantes: quando multiplos agentes selecionam
        o mesmo texto_original, a primeira mantem sua acao e as demais
        sao convertidas em comentarios.
        """
        vistos = {}
        processadas = []

        for idx, rev in enumerate(revisoes):
            texto_orig = rev.get('texto_original', '')
            if not texto_orig:
                processadas.append(rev)
                continue

            chave = normalizar_texto(texto_orig)

            if chave in vistos:
                rev_copia = dict(rev)
                acao_orig = rev.get('acao', 'substituir')
                texto_novo = rev.get('texto_novo', '')
                justificativa = rev.get('justificativa', '')

                partes = [f"[Revisao conflitante - {acao_orig}]"]
                if texto_novo:
                    partes.append(f"Sugestao: {texto_novo}.")
                partes.append(f"Motivo: {justificativa}")

                rev_copia['acao'] = 'comentario'
                rev_copia['justificativa'] = ' '.join(partes)
                processadas.append(rev_copia)
            else:
                vistos[chave] = idx
                processadas.append(rev)

        return processadas

    # =========================================================================
    # BUSCA DE TEXTO (nivel de paragrafo, multi-run, com hyperlinks)
    # =========================================================================

    def _obter_segmentos_paragrafo(self, paragraph):
        """
        Obtem todos os segmentos de texto de um paragrafo com suas posicoes.
        Inclui texto de w:r diretos E de w:hyperlink.
        Cada segmento e um dict com: element, text, start, end, rPr, type.
        """
        segments = []
        current_pos = 0

        for child in paragraph:
            if child.tag == f'{W_NS}r':
                run_text = ''
                for t in child.findall(f'{W_NS}t'):
                    run_text += (t.text or '')
                if run_text:
                    segments.append({
                        'element': child,
                        'text': run_text,
                        'start': current_pos,
                        'end': current_pos + len(run_text),
                        'rPr': child.find(f'{W_NS}rPr'),
                        'type': 'run',
                    })
                    current_pos += len(run_text)

            elif child.tag == f'{W_NS}hyperlink':
                hl_text = ''
                hl_rPr = None
                for r in child.findall(f'{W_NS}r'):
                    for t in r.findall(f'{W_NS}t'):
                        hl_text += (t.text or '')
                    if hl_rPr is None:
                        hl_rPr = r.find(f'{W_NS}rPr')
                if hl_text:
                    segments.append({
                        'element': child,
                        'text': hl_text,
                        'start': current_pos,
                        'end': current_pos + len(hl_text),
                        'rPr': hl_rPr,
                        'type': 'hyperlink',
                    })
                    current_pos += len(hl_text)

        return segments

    def _encontrar_texto(self, texto_busca: str):
        """
        Encontra texto_busca no documento, buscando no nivel de paragrafo.
        Suporta texto dividido em multiplos runs e dentro de hyperlinks.

        Estrategias de busca (em ordem):
        1. Match exato no texto concatenado do paragrafo
        2. Match normalizado (smart quotes, whitespace, etc)
        3. Match com bullets removidos
        4. Match normalizado + sem bullets
        """
        texto_norm = normalizar_texto(texto_busca)
        texto_sem_bullet = strip_bullets(texto_busca)
        texto_sem_bullet_norm = normalizar_texto(texto_sem_bullet)

        for paragraph in self.doc_root.iter(f'{W_NS}p'):
            segments = self._obter_segmentos_paragrafo(paragraph)
            if not segments:
                continue

            full_text = ''.join(s['text'] for s in segments)
            if not full_text.strip():
                continue

            # Estrategia 1: match exato
            idx = full_text.find(texto_busca)
            if idx >= 0:
                return self._montar_resultado_match(
                    paragraph, segments, full_text, idx, idx + len(texto_busca)
                )

            # Estrategia 2: match normalizado
            full_norm, full_mapa = normalizar_com_mapa(full_text)
            idx_norm = full_norm.find(texto_norm)
            if idx_norm >= 0:
                orig_start, orig_end = self._mapear_posicao(
                    full_mapa, idx_norm, len(texto_norm)
                )
                if orig_start is not None:
                    return self._montar_resultado_match(
                        paragraph, segments, full_text, orig_start, orig_end
                    )

            # Estrategia 3: sem bullets (match exato)
            if texto_sem_bullet and texto_sem_bullet != texto_busca:
                idx = full_text.find(texto_sem_bullet)
                if idx >= 0:
                    return self._montar_resultado_match(
                        paragraph, segments, full_text, idx, idx + len(texto_sem_bullet)
                    )

            # Estrategia 4: sem bullets + normalizado
            if texto_sem_bullet_norm and texto_sem_bullet_norm != texto_norm:
                idx_norm = full_norm.find(texto_sem_bullet_norm)
                if idx_norm >= 0:
                    orig_start, orig_end = self._mapear_posicao(
                        full_mapa, idx_norm, len(texto_sem_bullet_norm)
                    )
                    if orig_start is not None:
                        return self._montar_resultado_match(
                            paragraph, segments, full_text, orig_start, orig_end
                        )

        return None

    def _mapear_posicao(self, mapa, norm_start, norm_len):
        """Mapeia posicao no texto normalizado de volta para o texto original."""
        if not mapa:
            return None, None
        if norm_start + norm_len > len(mapa):
            return None, None

        orig_start = mapa[norm_start]
        orig_last = mapa[norm_start + norm_len - 1]
        orig_end = orig_last + 1

        return orig_start, orig_end

    def _montar_resultado_match(self, paragraph, segments, full_text, match_start, match_end):
        """Monta o dict de resultado com segmentos afetados."""
        affected = []
        for s in segments:
            if s['end'] <= match_start or s['start'] >= match_end:
                continue

            clip_start = max(match_start - s['start'], 0)
            clip_end = min(match_end - s['start'], len(s['text']))

            affected.append({
                'element': s['element'],
                'text': s['text'],
                'rPr': s.get('rPr'),
                'type': s.get('type', 'run'),
                'clip_start': clip_start,
                'clip_end': clip_end,
                'before_text': s['text'][:clip_start],
                'after_text': s['text'][clip_end:],
                'matched_text': s['text'][clip_start:clip_end],
            })

        if not affected:
            return None

        return {
            'paragraph': paragraph,
            'match_start': match_start,
            'match_end': match_end,
            'segments': segments,
            'full_text': full_text,
            'matched_original': full_text[match_start:match_end],
            'affected': affected,
        }

    # =========================================================================
    # BUSCA DE TEXTO PARA COMENTARIOS (inclui w:ins e w:hyperlink)
    # =========================================================================

    def _encontrar_texto_para_comentario(self, texto_busca: str):
        """
        Busca texto incluindo dentro de w:ins e w:hyperlink.
        Usado para marcar comentarios.
        """
        texto_norm = normalizar_texto(texto_busca)

        for paragraph in self.doc_root.iter(f'{W_NS}p'):
            elements_info = []
            current_pos = 0

            for child in paragraph:
                if child.tag == f'{W_NS}r':
                    run_text = ''.join(t.text or '' for t in child.findall(f'{W_NS}t'))
                    if run_text:
                        elements_info.append({
                            'element': child, 'text': run_text,
                            'start': current_pos, 'end': current_pos + len(run_text),
                        })
                        current_pos += len(run_text)
                elif child.tag == f'{W_NS}ins':
                    for r in child.findall(f'{W_NS}r'):
                        run_text = ''.join(t.text or '' for t in r.findall(f'{W_NS}t'))
                        if run_text:
                            elements_info.append({
                                'element': child, 'text': run_text,
                                'start': current_pos, 'end': current_pos + len(run_text),
                            })
                            current_pos += len(run_text)
                elif child.tag == f'{W_NS}hyperlink':
                    for r in child.findall(f'{W_NS}r'):
                        run_text = ''.join(t.text or '' for t in r.findall(f'{W_NS}t'))
                        if run_text:
                            elements_info.append({
                                'element': child, 'text': run_text,
                                'start': current_pos, 'end': current_pos + len(run_text),
                            })
                            current_pos += len(run_text)

            if not elements_info:
                continue

            full_text = ''.join(ei['text'] for ei in elements_info)

            # Match exato
            idx = full_text.find(texto_busca)
            if idx >= 0:
                for ei in elements_info:
                    if ei['start'] <= idx < ei['end']:
                        return paragraph, ei['element']
                continue

            # Match normalizado
            full_norm = normalizar_texto(full_text)
            if texto_norm in full_norm:
                return paragraph, elements_info[0]['element']

        return None, None

    # =========================================================================
    # OPERACOES DE TRACK CHANGES
    # =========================================================================

    def _processar_revisao(self, idx: int, rev: dict):
        """Processa uma unica revisao."""
        acao = rev.get("acao", "").lower()
        texto_original = rev.get("texto_original", "")
        texto_novo = rev.get("texto_novo", "")
        justificativa = rev.get("justificativa", "")
        tipo = rev.get("tipo", "TEXTO")

        if not texto_original and acao != "inserir":
            self.resultados.append({
                "idx": idx, "ok": False,
                "erro": "texto_original e obrigatorio para esta acao"
            })
            return

        if acao == "substituir":
            sucesso = self._aplicar_substituicao(texto_original, texto_novo)
            if sucesso:
                self._registrar_comentario(texto_novo or texto_original, tipo, justificativa)
                self.resultados.append({"idx": idx, "ok": True, "acao": "substituir"})
            else:
                self.resultados.append({
                    "idx": idx, "ok": False,
                    "erro": f"Texto nao encontrado: '{texto_original[:80]}...'"
                })

        elif acao == "deletar":
            sucesso = self._aplicar_delecao(texto_original)
            if sucesso:
                self._registrar_comentario(texto_original, tipo, f"Removido: {justificativa}")
                self.resultados.append({"idx": idx, "ok": True, "acao": "deletar"})
            else:
                self.resultados.append({
                    "idx": idx, "ok": False,
                    "erro": f"Texto nao encontrado para delecao: '{texto_original[:80]}...'"
                })

        elif acao == "inserir":
            sucesso = self._aplicar_insercao(texto_original, texto_novo)
            if sucesso:
                self._registrar_comentario(texto_novo, tipo, f"Inserido: {justificativa}")
                self.resultados.append({"idx": idx, "ok": True, "acao": "inserir"})
            else:
                self.resultados.append({
                    "idx": idx, "ok": False,
                    "erro": f"Nao foi possivel inserir apos: '{texto_original[:80]}...'"
                })

        elif acao == "comentario":
            sucesso = self._adicionar_comentario_inline(texto_original, tipo, justificativa)
            if sucesso:
                self.resultados.append({"idx": idx, "ok": True, "acao": "comentario"})
            else:
                self.resultados.append({
                    "idx": idx, "ok": False,
                    "erro": f"Texto nao encontrado para comentario: '{texto_original[:80]}...'"
                })

        else:
            self.resultados.append({
                "idx": idx, "ok": False,
                "erro": f"Acao desconhecida: {acao}"
            })

    def _aplicar_substituicao(self, texto_antigo: str, texto_novo: str) -> bool:
        """Aplica uma substituicao com Track Changes (multi-run, hyperlink-aware)."""
        match = self._encontrar_texto(texto_antigo)
        if not match:
            return False

        paragraph = match['paragraph']
        affected = match['affected']

        first_elem = affected[0]['element']
        first_idx = list(paragraph).index(first_elem)

        new_elements = []

        # Texto antes do match no primeiro segmento afetado
        if affected[0]['before_text']:
            new_elements.append(
                self._criar_segmento(
                    affected[0]['before_text'],
                    affected[0]['rPr'],
                    affected[0]['type'],
                    affected[0]['element']
                )
            )

        # Delecao do texto original (preserva formatacao de cada run)
        del_elem = self._criar_delecao_multi(affected)
        new_elements.append(del_elem)

        # Insercao do texto novo, preservando hyperlinks quando possivel
        ins_elements = self._criar_insercao_com_hyperlinks(
            texto_novo, affected, affected[0].get('rPr')
        )
        new_elements.extend(ins_elements)

        # Texto apos o match no ultimo segmento afetado
        if affected[-1]['after_text']:
            new_elements.append(
                self._criar_segmento(
                    affected[-1]['after_text'],
                    affected[-1]['rPr'],
                    affected[-1]['type'],
                    affected[-1]['element']
                )
            )

        # Remove segmentos afetados (deduplicados, mantendo ordem)
        unique_elems = list(dict.fromkeys(ar['element'] for ar in affected))
        for elem in unique_elems:
            paragraph.remove(elem)

        # Insere novos elementos
        for i, new_elem in enumerate(new_elements):
            paragraph.insert(first_idx + i, new_elem)

        self.revision_id += 1
        return True

    def _aplicar_delecao(self, texto: str) -> bool:
        """Aplica uma delecao com Track Changes (multi-run, hyperlink-aware)."""
        match = self._encontrar_texto(texto)
        if not match:
            return False

        paragraph = match['paragraph']
        affected = match['affected']

        first_elem = affected[0]['element']
        first_idx = list(paragraph).index(first_elem)

        new_elements = []

        if affected[0]['before_text']:
            new_elements.append(
                self._criar_segmento(
                    affected[0]['before_text'],
                    affected[0]['rPr'],
                    affected[0]['type'],
                    affected[0]['element']
                )
            )

        del_elem = self._criar_delecao_multi(affected)
        new_elements.append(del_elem)

        if affected[-1]['after_text']:
            new_elements.append(
                self._criar_segmento(
                    affected[-1]['after_text'],
                    affected[-1]['rPr'],
                    affected[-1]['type'],
                    affected[-1]['element']
                )
            )

        unique_elems = list(dict.fromkeys(ar['element'] for ar in affected))
        for elem in unique_elems:
            paragraph.remove(elem)

        for i, new_elem in enumerate(new_elements):
            paragraph.insert(first_idx + i, new_elem)

        self.revision_id += 1
        return True

    def _aplicar_insercao(self, contexto: str, texto_novo: str) -> bool:
        """Insere texto apos o contexto especificado (multi-run, hyperlink-aware)."""
        match = self._encontrar_texto(contexto)
        if not match:
            return False

        paragraph = match['paragraph']
        affected = match['affected']

        last_ar = affected[-1]
        last_elem = last_ar['element']
        last_idx = list(paragraph).index(last_elem)

        # Usar rPr do contexto para manter formatacao consistente
        ins_elem = self._criar_insercao(texto_novo, last_ar.get('rPr'))

        if not last_ar['after_text']:
            # Contexto termina exatamente no final do segmento
            paragraph.insert(last_idx + 1, ins_elem)
        else:
            # Contexto termina no meio do segmento - dividir
            paragraph.remove(last_elem)

            run_antes = self._criar_segmento(
                last_ar['text'][:last_ar['clip_end']],
                last_ar['rPr'],
                last_ar['type'],
                last_ar['element']
            )
            run_depois = self._criar_segmento(
                last_ar['after_text'],
                last_ar['rPr'],
                last_ar['type'],
                last_ar['element']
            )

            paragraph.insert(last_idx, run_antes)
            paragraph.insert(last_idx + 1, ins_elem)
            paragraph.insert(last_idx + 2, run_depois)

        self.revision_id += 1
        return True

    def _adicionar_comentario_inline(self, texto: str, tipo: str, comentario: str) -> bool:
        """Adiciona um comentario vinculado a um trecho de texto."""
        match = self._encontrar_texto(texto)
        if not match:
            return False
        self._registrar_comentario(texto, tipo, comentario)
        return True

    # =========================================================================
    # CRIACAO DE ELEMENTOS XML
    # =========================================================================

    def _criar_segmento(self, texto: str, rPr=None, tipo: str = 'run',
                        original_element=None) -> etree._Element:
        """
        Cria o elemento apropriado baseado no tipo do segmento.
        Para 'run': cria w:r com texto e formatacao.
        Para 'hyperlink': cria w:hyperlink preservando atributos do original.
        """
        if tipo == 'hyperlink' and original_element is not None:
            return self._criar_hyperlink_com_texto(original_element, texto, rPr)
        else:
            return self._criar_run_com_props(texto, rPr)

    def _criar_run_com_props(self, texto: str, rPr=None) -> etree._Element:
        """Cria um w:r com texto, copiando formatacao do run original."""
        r = etree.Element(f'{W_NS}r')
        if rPr is not None:
            r.append(deepcopy(rPr))
        t = etree.SubElement(r, f'{W_NS}t')
        t.text = texto
        t.set(f'{XML_NS}space', 'preserve')
        return r

    def _criar_hyperlink_com_texto(self, original_hyperlink, texto: str,
                                    rPr=None) -> etree._Element:
        """
        Cria um w:hyperlink baseado no original mas com texto diferente.
        Preserva todos os atributos (r:id, w:history, etc) e namespaces.
        """
        # Deep copy preserva atributos e namespaces
        new_hl = deepcopy(original_hyperlink)
        # Remove todos os filhos existentes
        for child in list(new_hl):
            new_hl.remove(child)
        # Adiciona novo run com o texto especificado
        r = etree.SubElement(new_hl, f'{W_NS}r')
        if rPr is not None:
            r.append(deepcopy(rPr))
        t = etree.SubElement(r, f'{W_NS}t')
        t.text = texto
        t.set(f'{XML_NS}space', 'preserve')
        return new_hl

    def _criar_delecao_multi(self, affected_segments: list) -> etree._Element:
        """
        Cria elemento w:del com multiplos runs, preservando a formatacao
        original de cada segmento (run ou hyperlink).
        """
        del_elem = etree.Element(f'{W_NS}del')
        del_elem.set(f'{W_NS}id', str(self.revision_id))
        del_elem.set(f'{W_NS}author', self.autor)
        del_elem.set(f'{W_NS}date', datetime.now().isoformat())

        for seg in affected_segments:
            matched_text = seg['matched_text']
            if matched_text:
                del_r = etree.SubElement(del_elem, f'{W_NS}r')
                if seg.get('rPr') is not None:
                    del_r.append(deepcopy(seg['rPr']))
                del_text = etree.SubElement(del_r, f'{W_NS}delText')
                del_text.text = matched_text
                del_text.set(f'{XML_NS}space', 'preserve')

        return del_elem

    def _criar_insercao(self, texto: str, rPr=None) -> etree._Element:
        """
        Cria um elemento w:ins para insercao rastreada.
        Opcionalmente copia formatacao (rPr) para manter estilo do texto original
        (ex: titulos, negrito, etc).
        """
        ins_elem = etree.Element(f'{W_NS}ins')
        ins_elem.set(f'{W_NS}id', str(self.revision_id + 1000))
        ins_elem.set(f'{W_NS}author', self.autor)
        ins_elem.set(f'{W_NS}date', datetime.now().isoformat())

        ins_r = etree.SubElement(ins_elem, f'{W_NS}r')
        if rPr is not None:
            ins_r.append(deepcopy(rPr))
        ins_text = etree.SubElement(ins_r, f'{W_NS}t')
        ins_text.text = texto
        ins_text.set(f'{XML_NS}space', 'preserve')

        return ins_elem

    def _criar_insercao_com_hyperlinks(self, texto_novo: str, affected: list,
                                        rPr=None) -> list:
        """
        Cria elementos de insercao preservando hyperlinks dos segmentos afetados.
        Se o texto do hyperlink original ainda aparece no texto_novo, o hyperlink
        e mantido (usando w:hyperlink > w:ins > w:r, que e OOXML valido).
        Caso contrario, cria insercao simples.

        Retorna lista de elementos (w:ins e/ou w:hyperlink).
        """
        # Coleta hyperlinks dos segmentos afetados
        hyperlinks = []
        for seg in affected:
            if seg['type'] == 'hyperlink' and seg['matched_text']:
                hyperlinks.append({
                    'text': seg['matched_text'],
                    'element': seg['element'],
                    'rPr': seg.get('rPr'),
                })

        if not hyperlinks:
            # Sem hyperlinks para preservar - insercao simples
            return [self._criar_insercao(texto_novo, rPr)]

        # Tenta encontrar o texto de cada hyperlink no texto novo
        elements = []
        remaining = texto_novo

        for hl in hyperlinks:
            hl_text = hl['text']
            idx = remaining.find(hl_text)
            if idx < 0:
                # Hyperlink nao encontrado no texto novo - sera perdido
                continue

            # Texto antes do hyperlink
            before = remaining[:idx]
            if before:
                elements.append(self._criar_insercao(before, rPr))

            # Hyperlink preservado com w:ins dentro
            hl_elem = self._criar_hyperlink_com_insercao(
                hl['element'], hl_text, hl['rPr']
            )
            elements.append(hl_elem)

            remaining = remaining[idx + len(hl_text):]

        # Texto restante apos ultimo hyperlink
        if remaining:
            elements.append(self._criar_insercao(remaining, rPr))

        if not elements:
            # Nenhum hyperlink preservado - fallback para insercao simples
            return [self._criar_insercao(texto_novo, rPr)]

        return elements

    def _criar_hyperlink_com_insercao(self, original_hyperlink, texto: str,
                                       rPr=None) -> etree._Element:
        """
        Cria w:hyperlink contendo w:ins (track change dentro do hyperlink).
        Preserva atributos do hyperlink original (r:id, URL, etc).
        Estrutura: w:hyperlink > w:ins > w:r > w:t
        """
        new_hl = deepcopy(original_hyperlink)
        # Remove filhos existentes
        for child in list(new_hl):
            new_hl.remove(child)

        # Cria w:ins dentro do hyperlink
        ins_elem = etree.SubElement(new_hl, f'{W_NS}ins')
        ins_elem.set(f'{W_NS}id', str(self.revision_id + 1000))
        ins_elem.set(f'{W_NS}author', self.autor)
        ins_elem.set(f'{W_NS}date', datetime.now().isoformat())

        ins_r = etree.SubElement(ins_elem, f'{W_NS}r')
        if rPr is not None:
            ins_r.append(deepcopy(rPr))
        ins_text = etree.SubElement(ins_r, f'{W_NS}t')
        ins_text.text = texto
        ins_text.set(f'{XML_NS}space', 'preserve')

        return new_hl

    # =========================================================================
    # COMENTARIOS
    # =========================================================================

    def _registrar_comentario(self, texto: str, tipo: str, comentario: str):
        """Registra um comentario para ser adicionado depois."""
        self.comments.append({
            "id": len(self.comments),
            "texto": texto,
            "comentario": f"[{tipo}] {comentario}",
            "autor": self.autor
        })

    def _adicionar_comments(self):
        """Adiciona todos os comentarios registrados ao documento."""
        NSMAP = {'w': NAMESPACES['w']}
        comments_xml = etree.Element(f'{W_NS}comments', nsmap=NSMAP)

        for comment in self.comments:
            comm_elem = etree.SubElement(comments_xml, f'{W_NS}comment')
            comm_elem.set(f'{W_NS}id', str(comment['id']))
            comm_elem.set(f'{W_NS}author', comment['autor'])
            comm_elem.set(f'{W_NS}date', datetime.now().isoformat())

            p = etree.SubElement(comm_elem, f'{W_NS}p')
            r = etree.SubElement(p, f'{W_NS}r')
            t = etree.SubElement(r, f'{W_NS}t')
            t.text = comment['comentario']

            self._marcar_texto_comentario(comment)

        comments_path = os.path.join(self.temp_dir, 'word', 'comments.xml')
        comments_tree = etree.ElementTree(comments_xml)
        comments_tree.write(comments_path, xml_declaration=True, encoding='UTF-8', standalone=True)

        self._atualizar_content_types()
        self._atualizar_rels()

    def _marcar_texto_comentario(self, comment: dict):
        """
        Marca um trecho de texto com referencia ao comentario.
        Busca inclusive dentro de w:ins e w:hyperlink.
        """
        texto_busca = comment['texto']
        comment_id = comment['id']

        paragraph, target_elem = self._encontrar_texto_para_comentario(texto_busca)
        if paragraph is None or target_elem is None:
            return

        idx = list(paragraph).index(target_elem)

        start = etree.Element(f'{W_NS}commentRangeStart')
        start.set(f'{W_NS}id', str(comment_id))
        paragraph.insert(idx, start)

        end = etree.Element(f'{W_NS}commentRangeEnd')
        end.set(f'{W_NS}id', str(comment_id))
        paragraph.insert(idx + 2, end)

        ref_r = etree.Element(f'{W_NS}r')
        ref = etree.SubElement(ref_r, f'{W_NS}commentReference')
        ref.set(f'{W_NS}id', str(comment_id))
        paragraph.insert(idx + 3, ref_r)

    # =========================================================================
    # CONFIGURACAO E EMPACOTAMENTO
    # =========================================================================

    def _habilitar_track_changes(self):
        """Habilita o rastreamento de alteracoes no settings.xml."""
        settings_path = os.path.join(self.temp_dir, 'word', 'settings.xml')

        if os.path.exists(settings_path):
            settings_tree = etree.parse(settings_path)
            settings_root = settings_tree.getroot()

            existing = settings_root.find(f'{W_NS}trackRevisions')
            if existing is None:
                etree.SubElement(settings_root, f'{W_NS}trackRevisions')

            settings_tree.write(settings_path, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _atualizar_content_types(self):
        """Atualiza [Content_Types].xml para incluir comments.xml."""
        content_types_path = os.path.join(self.temp_dir, '[Content_Types].xml')
        ct_tree = etree.parse(content_types_path)
        ct_root = ct_tree.getroot()

        for override in ct_root.findall('.//{*}Override'):
            if override.get('PartName') == '/word/comments.xml':
                return

        override = etree.SubElement(ct_root, 'Override')
        override.set('PartName', '/word/comments.xml')
        override.set('ContentType',
                     'application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml')

        ct_tree.write(content_types_path, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _atualizar_rels(self):
        """Atualiza document.xml.rels para incluir relacionamento com comments.xml."""
        rels_path = os.path.join(self.temp_dir, 'word', '_rels', 'document.xml.rels')
        rels_tree = etree.parse(rels_path)
        rels_root = rels_tree.getroot()

        for rel in rels_root:
            if rel.get('Target') == 'comments.xml':
                return

        rel_count = len(rels_root)

        rel = etree.SubElement(rels_root, f'{{{REL_NS}}}Relationship')
        rel.set('Id', f'rId{rel_count + 1}')
        rel.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments')
        rel.set('Target', 'comments.xml')

        rels_tree.write(rels_path, xml_declaration=True, encoding='UTF-8', standalone=True)

    def _recompactar_docx(self):
        """Recompacta o diretorio temporario em um arquivo DOCX."""
        with zipfile.ZipFile(self.output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root_dir, dirs, files in os.walk(self.temp_dir):
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    arc_name = os.path.relpath(file_path, self.temp_dir)
                    zipf.write(file_path, arc_name)


# =============================================================================
# FUNCAO DE CONVENIENCIA
# =============================================================================

def aplicar_revisoes_docx(
    input_path: str,
    output_path: str,
    revisoes: list,
    autor: str = "Agente IA Revisor"
) -> dict:
    """
    Funcao de conveniencia para aplicar revisoes a um documento.
    """
    applicator = TrackChangesApplicator(input_path, output_path)
    return applicator.aplicar_revisoes(revisoes, autor)
