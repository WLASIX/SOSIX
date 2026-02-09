"""NVIDIA API client module with optional streaming support.
Uses API key and endpoint directly from `config.json` via `config_loader`.
"""
import requests
import json
import time
import asyncio
import concurrent.futures
from typing import List, Dict, Any, Optional, Generator, Union
from logger import logger


class Message:
    """Message representation for API"""
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class NvidiaAPIClient:
    """Client for NVIDIA's API with streaming support"""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize NVIDIA API client with config

        Args:
            config: Configuration dict with endpoint, model, api_key, generation_params
        """
        self.endpoint = config.get("endpoint")  # Use endpoint as-is from config (should include /chat/completions)
        self.model = config.get("model")
        self.api_key = config.get("api_key")
        self.generation_params = config.get("generation_params", {})
        # New flags from config
        self.stream_default = config.get("stream", False)
        self.enable_reasoning = config.get("enable_reasoning", False)
        # Retry configuration
        self.max_retries = 3
        self.retry_delay_base = 1  # seconds
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        self.conversation_history: List[Message] = []

    def set_system_message(self, system_message: str):
        """Set initial system message for the conversation"""
        self.conversation_history = [Message("system", system_message)]

    def add_context_message(self, role: str, content: str):
        """Add a message to conversation history for context"""
        self.conversation_history.append(Message(role, content))

    def clear_history(self):
        """Clear conversation history"""
        self.conversation_history = []

    def _build_payload(self, message: str, use_history: bool, stream: bool = False) -> Dict[str, Any]:
        messages = []
        if use_history:
            messages = [msg.to_dict() for msg in self.conversation_history]
        messages.append({"role": "user", "content": message})

        payload = {
            "model": self.model,
            "messages": messages,
            **self.generation_params
        }
        # If streaming is requested, add stream flag
        if stream:
            payload["stream"] = True
        # If reasoning is enabled, include a reasoning hint in the payload (best-effort)
        if self.enable_reasoning:
            # NVIDIA Integrate may support custom reasoning hints; include conservatively
            payload["reasoning"] = {"enabled": True}
        return payload

    def _call_with_retry(self, message: str, use_history: bool) -> Optional[str]:
        """Make API call with retry logic on connection errors"""
        attempt = 0
        while attempt < self.max_retries:
            try:
                payload = self._build_payload(message, use_history, stream=False)
                logger.wait(f"Отправляю запрос на {self.endpoint}...")
                response = requests.post(self.endpoint, headers=self.headers, json=payload, timeout=30)
                
                # 🚨 SPECIAL HANDLING FOR 429 (Too Many Requests)
                if response.status_code == 429:
                    logger.warning(f"⏳ API: 429 Too Many Requests - жду 10 секунд...")
                    time.sleep(10)
                    logger.info(f"⏳ Повторяю запрос после паузы...")
                    # Retry immediately without incrementing attempt counter
                    continue
                
                if response.status_code != 200:
                    logger.error(f"Ошибка API: {response.status_code} - {response.text[:200]}")
                    raise Exception(f"API Ошибка {response.status_code}")

                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    choice = result["choices"][0]
                    response_text = None
                    if isinstance(choice, dict):
                        if "message" in choice and isinstance(choice["message"], dict):
                            response_text = choice["message"].get("content")
                        else:
                            response_text = choice.get("text") or choice.get("content")
                    response_text = response_text or ""
                    if use_history and response_text:
                        self.add_context_message("assistant", response_text)
                    logger.success(f"Ответ от API получен")
                    return response_text
                raise Exception("Неверный формат ответа API")

            except (requests.exceptions.RequestException, ConnectionError) as e:
                logger.warning(f"Ошибка соединения (попытка {attempt + 1}/{self.max_retries}): {type(e).__name__}")
                attempt += 1
                if attempt < self.max_retries:
                    wait_time = self.retry_delay_base * (2 ** (attempt - 1))
                    logger.info(f"Жду {wait_time}с перед повторной попыткой...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Все попытки исчерпаны после {self.max_retries} попыток")
                    raise
            except Exception as e:
                logger.error(f"Необратимая ошибка: {str(e)}")
                raise

    def call(self, message: str, use_history: bool = True, stream: Optional[bool] = None) -> Union[Optional[str], Generator[str, None, None]]:
        """
        Make API call to NVIDIA endpoint (SYNC VERSION).
        
        For async code, use call_async() instead to avoid blocking event loop.
        If `stream` is True, returns a generator (iterate over the result of `stream_call`).
        Otherwise returns the full response text.
        """
        # Log tool call
        logger.tool_call(
            "nvidia_api.call",
            "отправить запрос в LLM и получить ответ"
        )

        payload = self._build_payload(message, use_history)

        # Determine streaming behavior: explicit arg overrides config default
        if stream is None:
            stream = bool(self.stream_default)

        if stream:
            return self.stream_call(message, use_history)

        return self._call_with_retry(message, use_history)

    async def call_async(self, message: str, use_history: bool = True, stream: Optional[bool] = None) -> Union[Optional[str], Generator[str, None, None]]:
        """
        Async wrapper around call() to be used with await in async context.
        Runs the blocking call in a thread pool to avoid blocking event loop.
        """
        loop = asyncio.get_event_loop()
        
        # Run sync call in executor to not block event loop
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result = await loop.run_in_executor(
                executor,
                lambda: self.call(message, use_history, stream)
            )
        
        return result

    def stream_call(self, message: str, use_history: bool = True) -> Generator[str, None, None]:
        """
        Stream the API response line-by-line (SSE / chunked JSON lines support).

        Yields partial strings as they arrive (reasoning and content chunks).
        """
        attempt = 0
        while attempt < self.max_retries:
            try:
                payload = self._build_payload(message, use_history, stream=True)
                logger.wait(f"Отправляю streaming-запрос на {self.endpoint}... (попытка {attempt + 1}/{self.max_retries}, stream=True)")
                resp = requests.post(self.endpoint, headers=self.headers, json=payload, stream=True, timeout=30)
                
                # 🚨 SPECIAL HANDLING FOR 429 (Too Many Requests)
                if resp.status_code == 429:
                    logger.warning(f"⏳ API: 429 Too Many Requests - жду 5 секунд...")
                    time.sleep(5)
                    logger.info(f"⏳ Повторяю streaming-запрос после паузы...")
                    # Retry immediately without incrementing attempt counter
                    continue
                
                if resp.status_code != 200:
                    logger.error(f"Streaming API ошибка: {resp.status_code}")
                    raise Exception(f"API Ошибка {resp.status_code}")

                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    text = line.strip()
                    # Some servers prefix SSE with 'data: '
                    if text.startswith("data:"):
                        text = text[len("data:"):].strip()
                    if not text or text == "[DONE]":
                        continue
                    try:
                        obj = json.loads(text)
                    except Exception:
                        # Not JSON: yield raw chunk
                        yield text
                        continue

                    # Parse common streaming chunk structure
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # 'reasoning_content' may be present
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    content = delta.get("content") or delta.get("text")
                    if reasoning:
                        yield reasoning
                    if content:
                        yield content
                
                logger.success("Streaming завершён")
                return  # Success

            except (requests.exceptions.RequestException, ConnectionError) as e:
                logger.warning(f"Ошибка streaming соединения (попытка {attempt + 1}/{self.max_retries}): {type(e).__name__}")
                attempt += 1
                if attempt < self.max_retries:
                    wait_time = self.retry_delay_base * (2 ** (attempt - 1))
                    logger.info(f"Жду {wait_time}с перед повторной streaming-попыткой...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Streaming прерван: все {self.max_retries} попыток исчерпаны")
                    raise
            except Exception as e:
                logger.error(f"Необратимая ошибка streaming: {str(e)}")
                raise

    def analyze(self, prompt: str, context: str = "") -> str:
        message = prompt if not context else f"{context}\n\n{prompt}"
        return self.call(message, use_history=False, stream=False)

    async def analyze_async(self, prompt: str, context: str = "") -> str:
        """Async version of analyze()"""
        message = prompt if not context else f"{context}\n\n{prompt}"
        return await self.call_async(message, use_history=False, stream=False)

    def decide(self, prompt: str, context: str = "") -> str:
        message = prompt if not context else f"{context}\n\n{prompt}"
        return self.call(message, use_history=False, stream=False)

    async def decide_async(self, prompt: str, context: str = "") -> str:
        """Async version of decide()"""
        message = prompt if not context else f"{context}\n\n{prompt}"
        return await self.call_async(message, use_history=False, stream=False)

    def stream_decide(self, prompt: str, context: str = "") -> Generator[str, None, None]:
        message = prompt if not context else f"{context}\n\n{prompt}"
        return self.stream_call(message, use_history=False)
