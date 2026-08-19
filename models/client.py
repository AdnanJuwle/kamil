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

    def chat_tools(self, messages: list[dict], tools: list[dict],
                   temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                   sampling: dict | None = None) -> dict:
        """Native tool-calling: pass OpenAI `tools`, return the assistant message
        {"content": str|None, "tool_calls": [...]|None}. If the server rejects
        `tools` (older build / no tool template), degrade to a plain completion so
        the agent's text-parsing fallback still works."""
        payload = {"messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "stream": False, "tools": tools}
        if sampling:
            payload.update(sampling)
        try:
            r = httpx.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout)
            if r.status_code in (400, 404, 500):
                payload.pop("tools", None)   # server doesn't support tools — degrade
                r = httpx.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls")}
        except httpx.ConnectError:
            raise RuntimeError("Kamil: llama-server is not running. Start it first.")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kamil: llama-server returned error {e.response.status_code}")

    def describe_image(self, image_bytes: bytes, prompt: str = "Describe this image.",
                       mime: str = "image/jpeg", system: str | None = None,
                       temperature: float = 0.3, max_tokens: int = 512) -> str:
        """Ask a multimodal (mmproj-enabled) llama-server about an image. Sends the
        image as a base64 data-URI content part in the OpenAI vision format."""
        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]})
        payload = {"messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "stream": False}
        try:
            r = httpx.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except httpx.ConnectError:
            raise RuntimeError("Kamil: vision server is not running.")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kamil: vision server returned error {e.response.status_code}")

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