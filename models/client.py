import os
import httpx
import json
from typing import Generator
from core.config import LLAMA_SERVER_URL, LLAMA_TIMEOUT, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS

class LlamaClient:
    def __init__(self, base_url: str = LLAMA_SERVER_URL, timeout: int = LLAMA_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout

    def chat(self, messages: list[dict], temperature: float = DEFAULT_TEMPERATURE,
             max_tokens: int = DEFAULT_MAX_TOKENS, sampling: dict | None = None) -> str:
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }
        # Extra sampling controls (repeat_penalty, top_p, top_k, stop, …). llama-server
        # honours these in the OpenAI-compatible payload. Used to steady the quantized
        # MoE coder, which loops/rambles at high temperature.
        if sampling:
            payload.update(sampling)
        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except httpx.ConnectError:
            raise RuntimeError("Kamil: llama-server is not running. Start it first.")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kamil: llama-server returned error {e.response.status_code}")

    def chat_stream(self, messages: list[dict], temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generator[str, None, None]:
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True
        }
        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            return
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
        except httpx.ConnectError:
            raise RuntimeError("Kamil: llama-server is not running. Start it first.")