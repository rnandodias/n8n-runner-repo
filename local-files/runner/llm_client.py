"""
Cliente unificado para LLMs (Anthropic Claude e OpenAI GPT).
Permite alternar entre provedores via configuracao.
Suporta texto, busca web e visao multimodal.
"""
import base64
import json
import os
import re
from abc import ABC, abstractmethod

import httpx


# Limite de 5MB para imagens (API Anthropic)
# Base64 encoding aumenta o tamanho em ~33%, entao o limite original deve ser ~3.75MB
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB (limite da API para base64)
MAX_IMAGE_SIZE_ORIGINAL = int(MAX_IMAGE_SIZE_BYTES * 0.75)  # ~3.75MB (limite pre-base64)


def _verificar_tamanho_imagem_url(url: str) -> bool:
    """
    Verifica se imagem em URL esta dentro do limite (~3.75MB original = 5MB base64).
    Usa HEAD request primeiro, fallback para GET parcial.
    Retorna True se OK, False se excede ou falha.
    """
    try:
        # Tenta HEAD primeiro (mais rapido)
        response = httpx.head(url, timeout=10, follow_redirects=True)
        content_length = response.headers.get('content-length')

        if content_length:
            size = int(content_length)
            if size > MAX_IMAGE_SIZE_ORIGINAL:
                size_mb = size / (1024 * 1024)
                estimated_base64_mb = (size * 4 / 3) / (1024 * 1024)
                print(f"🚫 Imagem ignorada via HEAD ({size_mb:.1f}MB -> ~{estimated_base64_mb:.1f}MB base64): {url}")
                return False
            return True

        # Se HEAD nao retornou content-length, faz GET com stream
        with httpx.stream("GET", url, timeout=30, follow_redirects=True) as response:
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes(chunk_size=1024 * 64):
                size += len(chunk)
                if size > MAX_IMAGE_SIZE_ORIGINAL:
                    size_mb = size / (1024 * 1024)
                    print(f"🚫 Imagem ignorada via GET (>{size_mb:.1f}MB): {url}")
                    return False
            return True

    except Exception as e:
        print(f"AVISO: Erro ao verificar tamanho de imagem {url}: {e}")
        return False


def _carregar_imagem_como_base64(url: str) -> tuple:
    """
    Carrega imagem de URL e retorna (base64_data, media_type).
    Retorna (None, None) se falhar ou se imagem exceder 5MB.
    """
    print(f"🔄 _carregar_imagem_como_base64 v2: {url}")
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()

        size_bytes = len(response.content)
        size_mb = size_bytes / (1024 * 1024)
        # Base64 aumenta ~33%, entao estimamos o tamanho final
        estimated_base64_size = int(size_bytes * 4 / 3)
        estimated_base64_mb = estimated_base64_size / (1024 * 1024)
        print(f"📦 Tamanho: {size_mb:.2f}MB original -> ~{estimated_base64_mb:.2f}MB base64")

        # Verifica se o tamanho original vai exceder 5MB apos base64
        if size_bytes > MAX_IMAGE_SIZE_ORIGINAL:
            print(f"🚫 IGNORANDO (base64 excederia 5MB: {estimated_base64_mb:.2f}MB): {url}")
            return None, None

        print(f"✅ Imagem OK: {size_mb:.2f}MB -> ~{estimated_base64_mb:.2f}MB base64")

        content_type = response.headers.get('content-type', 'image/jpeg')
        if ';' in content_type:
            content_type = content_type.split(';')[0].strip()

        # Mapeia content-type para media_type valido
        media_type_map = {
            'image/jpeg': 'image/jpeg',
            'image/jpg': 'image/jpeg',
            'image/png': 'image/png',
            'image/gif': 'image/gif',
            'image/webp': 'image/webp',
        }
        media_type = media_type_map.get(content_type, 'image/jpeg')

        base64_data = base64.b64encode(response.content).decode('utf-8')
        return base64_data, media_type
    except Exception as e:
        print(f"Erro ao carregar imagem {url}: {e}")
        return None, None


class LLMClient(ABC):
    """Interface base para clientes de LLM."""

    @abstractmethod
    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000, artigo_context: str = None) -> str:
        """Gera uma resposta do modelo."""
        pass

    def gerar_resposta_com_busca(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000, artigo_context: str = None) -> str:
        """Gera resposta com capacidade de busca web. Fallback para gerar_resposta."""
        return self.gerar_resposta(system_prompt, user_prompt, max_tokens, artigo_context=artigo_context)

    def gerar_resposta_com_imagens(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000,
        artigo_context: str = None
    ) -> str:
        """
        Gera resposta analisando imagens (visao multimodal).

        Args:
            system_prompt: Prompt do sistema
            user_prompt: Prompt do usuario
            imagens: Lista de dicts com {url, alt?}
            max_tokens: Limite de tokens
            artigo_context: Conteudo do artigo para cache

        Returns:
            Resposta do modelo
        """
        # Fallback padrao: ignora imagens e usa texto
        print("AVISO: gerar_resposta_com_imagens nao implementado, usando texto apenas")
        return self.gerar_resposta(system_prompt, user_prompt, max_tokens, artigo_context=artigo_context)

    def gerar_resposta_com_imagens_e_busca(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000,
        artigo_context: str = None
    ) -> str:
        """
        Gera resposta com visao multimodal E busca web.
        Fallback para gerar_resposta_com_imagens.
        """
        return self.gerar_resposta_com_imagens(system_prompt, user_prompt, imagens, max_tokens, artigo_context=artigo_context)

    def extrair_json(self, resposta: str) -> list:
        """
        Extrai array JSON da resposta do modelo.
        Resiliente a: markdown fences, JSON truncado, erros parciais.
        Recupera o maximo de objetos possiveis mesmo com erros.
        Retorna apenas dicts validos.
        """
        if not resposta:
            print("AVISO: Resposta vazia do LLM")
            return []

        resposta = resposta.strip()
        print(f"🔎 extrair_json: resposta tem {len(resposta)} chars")

        # Remove markdown code fences (```json ... ``` ou ``` ... ```)
        if resposta.startswith('```'):
            resposta = re.sub(r'^```\w*\s*\n?', '', resposta)
            resposta = re.sub(r'\n?```\s*$', '', resposta)
            resposta = resposta.strip()
            print(f"🔎 Apos remover fences: {len(resposta)} chars")

        # Extrai o conteudo do array JSON
        json_text = resposta
        json_match = re.search(r'\[[\s\S]*\]', resposta)
        if json_match:
            json_text = json_match.group()
            print(f"🔎 Array JSON encontrado: {len(json_text)} chars")
        else:
            print(f"🔎 Nenhum array JSON encontrado, usando resposta completa")

        def _filtrar_dicts(items: list) -> list:
            """Filtra apenas dicts validos da lista."""
            filtered = [item for item in items if isinstance(item, dict)]
            print(f"🔎 _filtrar_dicts: {len(items)} items -> {len(filtered)} dicts")
            return filtered

        # Tentativa 1: parse direto
        try:
            result = json.loads(json_text)
            if isinstance(result, list):
                print(f"🔎 Parse direto OK: {len(result)} items")
                return _filtrar_dicts(result)
            else:
                print(f"🔎 Parse direto: resultado nao e lista, e {type(result).__name__}")
        except json.JSONDecodeError as e:
            print(f"🔎 Parse direto falhou: {e}")

        # Tentativa 2: truncar no ultimo objeto completo e fechar array
        # (recupera tudo antes do ponto com erro)
        try:
            last_brace = json_text.rfind('}')
            if last_brace > 0:
                truncated = json_text[:last_brace + 1]
                if not truncated.lstrip().startswith('['):
                    truncated = '[' + truncated
                truncated = truncated.rstrip().rstrip(',') + ']'
                result = json.loads(truncated)
                if isinstance(result, list) and result:
                    filtered = _filtrar_dicts(result)
                    if filtered:
                        print(f"JSON reparado (truncado): {len(filtered)} revisoes recuperadas")
                        return filtered
        except json.JSONDecodeError:
            pass

        # Tentativa 3: extrair objetos individuais com regex
        # (recupera cada objeto valido, ignora os malformados)
        objects = []
        # Busca blocos {...} que contenham campos de revisao
        for m in re.finditer(r'\{[^{}]*"acao"\s*:[^{}]*\}', json_text):
            try:
                obj = json.loads(m.group())
                if isinstance(obj, dict):
                    objects.append(obj)
            except json.JSONDecodeError:
                continue

        if objects:
            print(f"JSON reparado (individual): {len(objects)} revisoes recuperadas")
            return objects

        print(f"Erro ao parsear JSON: nenhuma revisao recuperada")
        print(f"Resposta (preview): {resposta[:500]}...")
        return []


class AnthropicClient(LLMClient):
    """Cliente para API da Anthropic (Claude)."""

    def __init__(self, model: str = None):
        import anthropic
        self.client = anthropic.Anthropic(max_retries=10)
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

    def _build_system(self, system_prompt: str, artigo_context: str = None):
        """Monta system prompt com cache_control quando artigo_context fornecido."""
        if artigo_context:
            return [
                {"type": "text", "text": artigo_context, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system_prompt}
            ]
        return system_prompt

    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000, artigo_context: str = None) -> str:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=self._build_system(system_prompt, artigo_context),
            messages=[{"role": "user", "content": user_prompt}]
        ) as stream:
            return stream.get_final_text()

    def gerar_resposta_com_busca(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000, artigo_context: str = None) -> str:
        """Gera resposta com web search habilitado (server-side tool da Anthropic)."""
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=self._build_system(system_prompt, artigo_context),
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_prompt}]
        ) as stream:
            return stream.get_final_text()

    def _preparar_imagens_para_mensagem(self, imagens: list) -> list:
        """
        Prepara lista de imagens para o formato de mensagem da Anthropic.
        Tenta usar URL direta para cdn-wcsm.alura.com.br, fallback para base64.
        """
        content_blocks = []

        for img in imagens:
            url = img.get('url', '')
            if not url:
                continue

            # Tenta usar URL direta para CDN da Alura (imagens publicas)
            if 'cdn-wcsm.alura.com.br' in url or 'cdn.alura.com.br' in url:
                # Verifica tamanho antes de incluir (limite 5MB da API)
                if not _verificar_tamanho_imagem_url(url):
                    continue
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": url
                    }
                })
            else:
                # Fallback: carrega como base64
                base64_data, media_type = _carregar_imagem_como_base64(url)
                if base64_data:
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64_data
                        }
                    })
                else:
                    print(f"AVISO: Imagem ignorada (falha ao carregar): {url}")

        return content_blocks

    def gerar_resposta_com_imagens(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000,
        artigo_context: str = None
    ) -> str:
        """Gera resposta com visao multimodal (Claude Vision)."""
        # Prepara blocos de imagem
        image_blocks = self._preparar_imagens_para_mensagem(imagens)

        # Monta conteudo: texto + imagens
        content = [{"type": "text", "text": user_prompt}]
        content.extend(image_blocks)

        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=self._build_system(system_prompt, artigo_context),
            messages=[{"role": "user", "content": content}]
        ) as stream:
            return stream.get_final_text()

    def gerar_resposta_com_imagens_e_busca(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000,
        artigo_context: str = None
    ) -> str:
        """Gera resposta com visao multimodal E busca web (Claude Vision + Web Search)."""
        # Prepara blocos de imagem
        image_blocks = self._preparar_imagens_para_mensagem(imagens)

        # Monta conteudo: texto + imagens
        content = [{"type": "text", "text": user_prompt}]
        content.extend(image_blocks)

        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=self._build_system(system_prompt, artigo_context),
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": content}]
        ) as stream:
            return stream.get_final_text()


class OpenAIClient(LLMClient):
    """Cliente para API da OpenAI (GPT)."""

    def __init__(self, model: str = None):
        from openai import OpenAI
        self.client = OpenAI()
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1")

    def _build_system(self, system_prompt: str, artigo_context: str = None) -> str:
        """Monta system prompt com artigo_context como prefixo (cache automatico da OpenAI)."""
        if artigo_context:
            return f"{artigo_context}\n\n---\n\n{system_prompt}"
        return system_prompt

    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000, artigo_context: str = None) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": self._build_system(system_prompt, artigo_context)},
                {"role": "user", "content": user_prompt}
            ]
        )
        return response.choices[0].message.content

    def _preparar_imagens_para_mensagem(self, imagens: list) -> list:
        """
        Prepara lista de imagens para o formato de mensagem da OpenAI.
        Usa URL direta ou base64.
        """
        image_contents = []

        for img in imagens:
            url = img.get('url', '')
            if not url:
                continue

            # OpenAI suporta URL direta para imagens publicas
            if url.startswith('http'):
                image_contents.append({
                    "type": "image_url",
                    "image_url": {"url": url}
                })
            else:
                # Fallback: carrega como base64
                base64_data, media_type = _carregar_imagem_como_base64(url)
                if base64_data:
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{base64_data}"
                        }
                    })
                else:
                    print(f"AVISO: Imagem ignorada (falha ao carregar): {url}")

        return image_contents

    def gerar_resposta_com_imagens(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000,
        artigo_context: str = None
    ) -> str:
        """Gera resposta com visao multimodal (GPT-4 Vision)."""
        # Prepara conteudo: texto + imagens
        image_contents = self._preparar_imagens_para_mensagem(imagens)

        user_content = [{"type": "text", "text": user_prompt}]
        user_content.extend(image_contents)

        response = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": self._build_system(system_prompt, artigo_context)},
                {"role": "user", "content": user_content}
            ]
        )
        return response.choices[0].message.content

    def gerar_resposta_com_imagens_e_busca(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000,
        artigo_context: str = None
    ) -> str:
        """
        Gera resposta com visao multimodal.
        AVISO: OpenAI nao tem busca web integrada como Anthropic.
        """
        print("AVISO: OpenAI nao suporta busca web integrada. Usando apenas visao.")
        return self.gerar_resposta_com_imagens(system_prompt, user_prompt, imagens, max_tokens, artigo_context=artigo_context)


def criar_cliente_llm(provider: str = None, model: str = None) -> LLMClient:
    """
    Cria um cliente LLM baseado no provedor especificado.

    Args:
        provider: "anthropic" ou "openai". Se None, usa LLM_PROVIDER do ambiente.
        model: Modelo especifico. Se None, usa padrao do provedor.

    Returns:
        Instancia de LLMClient
    """
    provider = provider or os.getenv("LLM_PROVIDER", "anthropic")

    if provider.lower() == "anthropic":
        return AnthropicClient(model)
    elif provider.lower() == "openai":
        return OpenAIClient(model)
    else:
        raise ValueError(f"Provedor desconhecido: {provider}. Use 'anthropic' ou 'openai'.")
