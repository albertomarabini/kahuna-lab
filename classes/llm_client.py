import asyncio
import threading
import random
import time
import traceback
from typing import Callable, TypeVar, Any, Dict, List, Optional, Tuple

from openai import OpenAI
from langchain_google_vertexai import VertexAI, ChatVertexAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from classes.model_props import parse_model_name, is_openai_model, estimate_cost_usd

T = TypeVar("T")


class MaxRetryErrorsException(Exception):
    pass


# Global backoff state (shared across all clients)
_global_backoff_lock = threading.Lock()
_global_wait_until = 0.0
_global_backoff_seconds = 30.0
_GLOBAL_BACKOFF_MAX = 600.0


def call_with_retries_sync(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    timeout_threshold: float = 50.0,
    log: Callable[[str], None] | None = None,
) -> T:
    """
    Run a sync LLM call with global 429/timeout backoff + retries.
    """
    last_exception: Exception | None = None

    def _is_timeout_error(e: Exception) -> bool:
        if isinstance(e, asyncio.TimeoutError):
            return True
        msg = repr(e)
        return "TimeoutError" in msg or "timed out" in msg.lower()

    def _is_resource_exhausted_error(e: Exception) -> bool:
        msg = str(e)
        return (
            "429" in msg
            and (
                "RESOURCE_EXHAUSTED" in msg
                or "Resource has been exhausted" in msg
                or "Too Many Requests" in msg
            )
        )

    def _respect_global_backoff() -> None:
        while True:
            with _global_backoff_lock:
                now = time.monotonic()
                wait = _global_wait_until - now
            if wait <= 0:
                return
            time.sleep(min(wait, 1.0))

    def _register_429_and_get_delay() -> float:
        global _global_wait_until, _global_backoff_seconds

        with _global_backoff_lock:
            now = time.monotonic()
            base = _global_backoff_seconds
            delay = random.uniform(base * 0.95, base * 1.35)
            _global_backoff_seconds = min(_global_backoff_seconds * 2, _GLOBAL_BACKOFF_MAX)
            _global_wait_until = max(_global_wait_until, now + delay)
            return delay

    def _reset_backoff_on_success() -> None:
        global _global_backoff_seconds
        _global_backoff_seconds = max(1.0, _global_backoff_seconds * 0.5)

    for attempt in range(retries):
        _respect_global_backoff()
        start_time = time.time()
        try:
            result = fn()
            _reset_backoff_on_success()
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            last_exception = e

            if _is_resource_exhausted_error(e) or _is_timeout_error(e):
                delay = _register_429_and_get_delay()
                msg = f"Attempt {attempt+1} got 429/timeout, backing off ~{delay:.1f}s."
            else:
                msg = f"Attempt {attempt+1} failed."

            if log:
                log(f"{msg} (elapsed={elapsed:.2f}s): {e}\n{traceback.format_exc()}")

            # optional: if elapsed > timeout_threshold and you want special logging, it's already covered above

    raise MaxRetryErrorsException(f"All {retries} retry attempts failed.") from last_exception


class BaseLlmClient:
    """
    Common usage accounting for both completion and chat clients.
    """

    last_usage: Optional[Dict[str, int]]

    def _merge_usage(self, resp: Any) -> None:
        if resp is None:
            return
        usage = getattr(resp, "usage", None)
        inc = None
        if usage is not None:
            details = getattr(usage, "input_tokens_details", None)
            inc = {
                "prompt_token_count": getattr(usage, "input_tokens", 0) or 0,
                "candidates_token_count": getattr(usage, "output_tokens", 0) or 0,
                "total_token_count": getattr(usage, "total_tokens", 0) or 0,
                "cached_content_token_count": getattr(details, "cached_tokens", 0) if details else 0,
            }
            # cost for this call
            model_name = getattr(self, "model_name", None)
            if model_name is not None:
                inc["accrued_cost"] = estimate_cost_usd(
                    llm_model_name=model_name,
                    prompt_tokens=int(inc["prompt_token_count"]),
                    completion_tokens=int(inc["candidates_token_count"]),
                    # model_configuration=getattr(self, "model_configuration", None),
                    service_tier=None,
                )[1]
            if self.last_usage is None:
                self.last_usage = inc
                return
            for k, v in inc.items():
                self.last_usage[k] = (self.last_usage.get(k, 0) or 0) + (v or 0)

    def _merge_vertex_usage(self, usage_metadata: Any) -> None:
        if not usage_metadata:
            return

        def get(k: str) -> int:
            if isinstance(usage_metadata, dict):
                return int(usage_metadata.get(k, 0) or 0)
            return int(getattr(usage_metadata, k, 0) or 0)

        inc = {
            "prompt_token_count": get("prompt_token_count"),
            "candidates_token_count": get("candidates_token_count"),
            "total_token_count": get("total_token_count"),
            "cached_content_token_count": get("cached_content_token_count"),
        }
        # cost for this call
        model_name = getattr(self, "model_name", None)
        if model_name is not None:
            inc["accrued_cost"] = estimate_cost_usd(
                llm_model_name=model_name,
                prompt_tokens=int(inc["prompt_token_count"]),
                completion_tokens=int(inc["candidates_token_count"]),
                # model_configuration=getattr(self, "model_configuration", None),
                service_tier=None,
            )[1]
        if self.last_usage is None:
            self.last_usage = inc
            return
        for k, v in inc.items():
            self.last_usage[k] = (self.last_usage.get(k, 0) or 0) + (v or 0)

    def get_accrued_cost(self) -> float:
        if not self.last_usage:
            return 0.0
        return float(self.last_usage.get("accrued_cost", 0.0))

    def get_accrued_usage(self):
        if not self.last_usage:
            return 0.0
        return float(self.last_usage)

class LlmClient(BaseLlmClient):
    """
    Minimal wrapper for "completion-style" use:

        text = llm.invoke("some prompt")

    Under the hood:
    - Vertex: VertexAI.invoke(prompt)
    - OpenAI: Responses API (client.responses.create)
    """

    def __init__(
        self,
        model_name: str,
        *,
        vertex_project: str,
        vertex_region: str,
        timeout: float | None = None,
    ):
        self.provider = "openai" if is_openai_model(model_name) else "vertex"
        self._timeout = timeout
        self.model_name = model_name
        self.last_usage: Optional[Dict[str, int]] = None
        self._openai_params = None

        if self.provider == "vertex":
            self._vertex = VertexAI(
                project=vertex_project,
                location=vertex_region,
                model_name=model_name,
                timeout=timeout,
            )
            self._client = None
        elif self.provider == "openai":
            self._vertex = None
            self.model_name, self._openai_params = parse_model_name(self.model_name)
            client_kwargs: Dict[str, Any] = {"max_retries": 0}
            if timeout is not None:
                client_kwargs["timeout"] = timeout

            self._client = OpenAI(**client_kwargs)
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def _invoke_once(self, prompt: str) -> str:
        """
        Single HTTP call without retries/backoff.
        """
        if self.provider == "vertex":
            resp = self._vertex.invoke(prompt)

            # Try to pull usage_metadata from the response if available
            usage_md = getattr(resp, "usage_metadata", None)
            if usage_md is None:
                rm = getattr(resp, "response_metadata", None)
                if isinstance(rm, dict):
                    usage_md = rm.get("usage_metadata")
                elif rm is not None:
                    usage_md = getattr(rm, "usage_metadata", None)
            self._merge_vertex_usage(usage_md)

            if isinstance(resp, str):
                return resp
            # LangChain's Vertex types often have .content
            return getattr(resp, "content", str(resp))

        # OpenAI: use Responses API; prompt is a plain string
        resp = self._client.responses.create(
            model=self.model_name,
            input=prompt,
            **self._openai_params,
        )
        self._merge_usage(resp)

        text = getattr(resp, "output_text", "") or ""
        return text.strip()

    def invoke(self, prompt: str, *, retries: int = 3) -> str:
        """
        Synchronous call with global 429/timeout backoff + retries.
        """
        return call_with_retries_sync(
            lambda: self._invoke_once(prompt),
            retries=retries,
            log=lambda msg: print(f"[LLM-RETRY] {msg}"),
        )


class ChatLlmClient(BaseLlmClient):
    """
    Minimal wrapper for chat-style use:

        text = chat_llm.invoke([HumanMessage(...), AIMessage(...), ...])

    Under the hood:
    - Vertex: ChatVertexAI.invoke(messages)
    - OpenAI: Responses API with input=[{role, content}, ...]
    """

    def __init__(
        self,
        model_name: str,
        *,
        vertex_project: str,
        vertex_region: str,
        timeout: float | None = None,
    ):
        self.provider = "openai" if is_openai_model(model_name) else "vertex"
        self.model_name = model_name
        self._timeout = timeout
        self.last_usage: Optional[Dict[str, int]] = None
        self._openai_params = None

        if self.provider == "vertex":
            self._vertex = ChatVertexAI(
                project=vertex_project,
                location=vertex_region,
                model_name=model_name,
                timeout=timeout,
            )
            self._client = None
        elif self.provider == "openai":
            self._vertex = None
            self.model_name, self._openai_params = parse_model_name(self.model_name)
            client_kwargs: Dict[str, Any] = {"max_retries": 0}
            if timeout is not None:
                client_kwargs["timeout"] = timeout

            self._client = OpenAI(**client_kwargs)
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def _to_openai_messages(self, messages: List[HumanMessage | AIMessage]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for m in messages:
            if isinstance(m, SystemMessage):
                role = "developer"  # or "system" if you prefer
            elif isinstance(m, HumanMessage):
                role = "user"
            elif isinstance(m, AIMessage):
                role = "assistant"
            else:
                role = "user"
            out.append({"role": role, "content": str(m.content)})
        return out

    def _invoke_once(self, messages: List[HumanMessage | AIMessage]) -> str:
        """
        Single HTTP call without retries/backoff.
        """
        if self.provider == "vertex":
            resp = self._vertex.invoke(messages)

            # Try to pull usage_metadata from the response if available
            usage_md = getattr(resp, "usage_metadata", None)
            if usage_md is None:
                rm = getattr(resp, "response_metadata", None)
                if isinstance(rm, dict):
                    usage_md = rm.get("usage_metadata")
                elif rm is not None:
                    usage_md = getattr(rm, "usage_metadata", None)
            self._merge_vertex_usage(usage_md)

            if isinstance(resp, str):
                return resp
            return getattr(resp, "content", str(resp))

        # OpenAI: Responses API with role/content messages
        oai_messages = self._to_openai_messages(messages)
        resp = self._client.responses.create(
            model=self.model_name,
            input=oai_messages,
            **self._openai_params,
        )
        self._merge_usage(resp)

        text = getattr(resp, "output_text", "") or ""
        return text.strip()

    def invoke(
        self,
        messages: List[HumanMessage | AIMessage],
        *,
        retries: int = 3,
    ) -> str:
        """
        Synchronous chat call with global 429/timeout backoff + retries.
        """
        return call_with_retries_sync(
            lambda: self._invoke_once(messages),
            retries=retries,
            log=lambda msg: print(f"[CHAT-LLM-RETRY] {msg}"),
        )
