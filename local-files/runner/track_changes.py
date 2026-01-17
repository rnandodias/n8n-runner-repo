"""
Modulo para aplicar Track Changes em documentos DOCX.
Usa manipulacao OOXML direta para criar revisoes rastreaveis.
"""
import os
import shutil
import tempfile
import zipfile
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
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'


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

    def _processar_revisao(self, idx: int, rev: dict):
        """Processa uma unica revisao."""
        acao = rev.get("acao", "").lower()
        texto_original = rev.get("texto_original", "")
        texto_novo = rev.get("texto_novo", "")
        justificativa = rev.get("justificativa", "")
        tipo = rev.get("tipo", "TEXTO")

        if not texto_original and acao != "inserir":
            self.resultados.append({
                "idx": idx,
                "ok": False,
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
                    "idx": idx,
                    "ok": False,
                    "erro": f"Texto nao encontrado: '{texto_original[:50]}...'"
                })

        elif acao == "deletar":
            sucesso = self._aplicar_delecao(texto_original)
            if sucesso:
                self._registrar_comentario(texto_original, tipo, f"Removido: {justificativa}")
                self.resultados.append({"idx": idx, "ok": True, "acao": "deletar"})
            else:
                self.resultados.append({
                    "idx": idx,
                    "ok": False,
                    "erro": f"Texto nao encontrado para delecao: '{texto_original[:50]}...'"
                })

        elif acao == "inserir":
            sucesso = self._aplicar_insercao(texto_original, texto_novo)
            if sucesso:
                self._registrar_comentario(texto_novo, tipo, f"Inserido: {justificativa}")
                self.resultados.append({"idx": idx, "ok": True, "acao": "inserir"})
            else:
                self.resultados.append({
                    "idx": idx,
                    "ok": False,
                    "erro": f"Nao foi possivel inserir apos: '{texto_original[:50]}...'"
                })

        elif acao == "comentario":
            sucesso = self._adicionar_comentario_inline(texto_original, tipo, justificativa)
            if sucesso:
                self.resultados.append({"idx": idx, "ok": True, "acao": "comentario"})
            else:
                self.resultados.append({
                    "idx": idx,
                    "ok": False,
                    "erro": f"Texto nao encontrado para comentario: '{texto_original[:50]}...'"
                })

        else:
            self.resultados.append({
                "idx": idx,
                "ok": False,
                "erro": f"Acao desconhecida: {acao}"
            })

    def _aplicar_substituicao(self, texto_antigo: str, texto_novo: str) -> bool:
        """Aplica uma substituicao com Track Changes."""
        for elem in self.doc_root.iter(f'{W_NS}t'):
            if elem.text and texto_antigo in elem.text:
                parent_run = elem.getparent()
                parent_para = parent_run.getparent()

                full_text = elem.text
                start_idx = full_text.find(texto_antigo)

                if start_idx == -1:
                    continue

                before = full_text[:start_idx]
                after = full_text[start_idx + len(texto_antigo):]

                run_idx = list(parent_para).index(parent_run)
                parent_para.remove(parent_run)

                new_elements = []

                if before:
                    r_before = self._criar_run_texto(before)
                    new_elements.append(r_before)

                del_elem = self._criar_delecao(texto_antigo)
                new_elements.append(del_elem)

                ins_elem = self._criar_insercao(texto_novo)
                new_elements.append(ins_elem)

                if after:
                    r_after = self._criar_run_texto(after)
                    new_elements.append(r_after)

                for i, new_elem in enumerate(new_elements):
                    parent_para.insert(run_idx + i, new_elem)

                self.revision_id += 1
                return True

        return False

    def _aplicar_delecao(self, texto: str) -> bool:
        """Aplica uma delecao com Track Changes."""
        for elem in self.doc_root.iter(f'{W_NS}t'):
            if elem.text and texto in elem.text:
                parent_run = elem.getparent()
                parent_para = parent_run.getparent()

                full_text = elem.text
                start_idx = full_text.find(texto)

                if start_idx == -1:
                    continue

                before = full_text[:start_idx]
                after = full_text[start_idx + len(texto):]

                run_idx = list(parent_para).index(parent_run)
                parent_para.remove(parent_run)

                new_elements = []

                if before:
                    new_elements.append(self._criar_run_texto(before))

                del_elem = self._criar_delecao(texto)
                new_elements.append(del_elem)

                if after:
                    new_elements.append(self._criar_run_texto(after))

                for i, new_elem in enumerate(new_elements):
                    parent_para.insert(run_idx + i, new_elem)

                self.revision_id += 1
                return True

        return False

    def _aplicar_insercao(self, contexto: str, texto_novo: str) -> bool:
        """Insere texto apos o contexto especificado."""
        for elem in self.doc_root.iter(f'{W_NS}t'):
            if elem.text and contexto in elem.text:
                parent_run = elem.getparent()
                parent_para = parent_run.getparent()

                full_text = elem.text
                start_idx = full_text.find(contexto)

                if start_idx == -1:
                    continue

                insert_point = start_idx + len(contexto)
                before = full_text[:insert_point]
                after = full_text[insert_point:]

                run_idx = list(parent_para).index(parent_run)
                parent_para.remove(parent_run)

                new_elements = []

                if before:
                    new_elements.append(self._criar_run_texto(before))

                ins_elem = self._criar_insercao(texto_novo)
                new_elements.append(ins_elem)

                if after:
                    new_elements.append(self._criar_run_texto(after))

                for i, new_elem in enumerate(new_elements):
                    parent_para.insert(run_idx + i, new_elem)

                self.revision_id += 1
                return True

        return False

    def _adicionar_comentario_inline(self, texto: str, tipo: str, comentario: str) -> bool:
        """Adiciona um comentario vinculado a um trecho de texto."""
        for elem in self.doc_root.iter(f'{W_NS}t'):
            if elem.text and texto in elem.text:
                self._registrar_comentario(texto, tipo, comentario)
                return True
        return False

    def _criar_run_texto(self, texto: str) -> etree._Element:
        """Cria um elemento w:r com texto."""
        r = etree.Element(f'{W_NS}r')
        t = etree.SubElement(r, f'{W_NS}t')
        t.text = texto
        t.set(f'{XML_NS}space', 'preserve')
        return r

    def _criar_delecao(self, texto: str) -> etree._Element:
        """Cria um elemento w:del para delecao rastreada."""
        del_elem = etree.Element(f'{W_NS}del')
        del_elem.set(f'{W_NS}id', str(self.revision_id))
        del_elem.set(f'{W_NS}author', self.autor)
        del_elem.set(f'{W_NS}date', datetime.now().isoformat())

        del_r = etree.SubElement(del_elem, f'{W_NS}r')
        del_text = etree.SubElement(del_r, f'{W_NS}delText')
        del_text.text = texto
        del_text.set(f'{XML_NS}space', 'preserve')

        return del_elem

    def _criar_insercao(self, texto: str) -> etree._Element:
        """Cria um elemento w:ins para insercao rastreada."""
        ins_elem = etree.Element(f'{W_NS}ins')
        ins_elem.set(f'{W_NS}id', str(self.revision_id + 1000))
        ins_elem.set(f'{W_NS}author', self.autor)
        ins_elem.set(f'{W_NS}date', datetime.now().isoformat())

        ins_r = etree.SubElement(ins_elem, f'{W_NS}r')
        ins_text = etree.SubElement(ins_r, f'{W_NS}t')
        ins_text.text = texto
        ins_text.set(f'{XML_NS}space', 'preserve')

        return ins_elem

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
        """Marca um trecho de texto com referencia ao comentario."""
        texto_busca = comment['texto']
        comment_id = comment['id']

        for elem in self.doc_root.iter(f'{W_NS}t'):
            if elem.text and texto_busca in elem.text:
                parent_run = elem.getparent()
                parent_para = parent_run.getparent()

                idx = list(parent_para).index(parent_run)

                start = etree.Element(f'{W_NS}commentRangeStart')
                start.set(f'{W_NS}id', str(comment_id))
                parent_para.insert(idx, start)

                end = etree.Element(f'{W_NS}commentRangeEnd')
                end.set(f'{W_NS}id', str(comment_id))
                parent_para.insert(idx + 2, end)

                ref_r = etree.Element(f'{W_NS}r')
                ref = etree.SubElement(ref_r, f'{W_NS}commentReference')
                ref.set(f'{W_NS}id', str(comment_id))
                parent_para.insert(idx + 3, ref_r)

                break

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
