"""
Cliente unificado para LLMs (Anthropic Claude e OpenAI GPT).
Permite alternar entre provedores via configuracao.
"""
import json
import os
import re
from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Interface base para clientes de LLM."""

    @abstractmethod
    def gerar_resposta(self, system_prompt: str, user_prompt: str, max_tokens: int = 32000) -> str:
        """Gera uma resposta do modelo."""
        pass

    def extrair_json(self, resposta: str) -> list:
        """Extrai array JSON da resposta do modelo."""
        try:
            resposta = resposta.strip()

            # Remove markdown code fences (```json ... ``` ou ``` ... ```)
            if resposta.startswith('```'):
                resposta = re.sub(r'^```\w*\s*\n?', '', resposta)
                resposta = re.sub(r'\n?```\s*$', '', resposta)
                resposta = resposta.strip()

            # Tenta parse direto primeiro
            try:
                result = json.loads(resposta)
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

            # Tenta encontrar array JSON na resposta
            json_match = re.search(r'\[[\s\S]*\]', resposta)
            if json_match:
                return json.loads(json_match.group())

            return []
        except json.JSONDecodeError as e:
            print(f"Erro ao parsear JSON: {e}")
            print(f"Resposta: {resposta[:500]}...")
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
