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


def _carregar_imagem_como_base64(url: str) -> tuple:
    """
    Carrega imagem de URL e retorna (base64_data, media_type).
    Retorna (None, None) se falhar.
    """
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()

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
    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000) -> str:
        """Gera uma resposta do modelo."""
        pass

    def gerar_resposta_com_busca(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000) -> str:
        """Gera resposta com capacidade de busca web. Fallback para gerar_resposta."""
        return self.gerar_resposta(system_prompt, user_prompt, max_tokens)

    def gerar_resposta_com_imagens(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000
    ) -> str:
        """
        Gera resposta analisando imagens (visao multimodal).

        Args:
            system_prompt: Prompt do sistema
            user_prompt: Prompt do usuario
            imagens: Lista de dicts com {url, alt?}
            max_tokens: Limite de tokens

        Returns:
            Resposta do modelo
        """
        # Fallback padrao: ignora imagens e usa texto
        print("AVISO: gerar_resposta_com_imagens nao implementado, usando texto apenas")
        return self.gerar_resposta(system_prompt, user_prompt, max_tokens)

    def gerar_resposta_com_imagens_e_busca(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000
    ) -> str:
        """
        Gera resposta com visao multimodal E busca web.
        Fallback para gerar_resposta_com_imagens.
        """
        return self.gerar_resposta_com_imagens(system_prompt, user_prompt, imagens, max_tokens)

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
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000) -> str:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        ) as stream:
            return stream.get_final_text()

    def gerar_resposta_com_busca(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000) -> str:
        """Gera resposta com web search habilitado (server-side tool da Anthropic)."""
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
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
        max_tokens: int = 32000
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
            system=system_prompt,
            messages=[{"role": "user", "content": content}]
        ) as stream:
            return stream.get_final_text()

    def gerar_resposta_com_imagens_e_busca(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000
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
            system=system_prompt,
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

    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
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
        max_tokens: int = 32000
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
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]
        )
        return response.choices[0].message.content

    def gerar_resposta_com_imagens_e_busca(
        self,
        system_prompt: str,
        user_prompt: str,
        imagens: list,
        max_tokens: int = 32000
    ) -> str:
        """
        Gera resposta com visao multimodal.
        AVISO: OpenAI nao tem busca web integrada como Anthropic.
        """
        print("AVISO: OpenAI nao suporta busca web integrada. Usando apenas visao.")
        return self.gerar_resposta_com_imagens(system_prompt, user_prompt, imagens, max_tokens)


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
