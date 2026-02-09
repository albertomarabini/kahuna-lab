from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
from langchain_google_vertexai import VertexAI, ChatVertexAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from classes.model_props import parse_model_name, is_openai_model, estimate_cost_usd


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
            )
        if self.last_usage is None:
            self.last_usage = inc
            return
        for k, v in inc.items():
            self.last_usage[k] = (self.last_usage.get(k, 0) or 0) + (v or 0)

    def get_accrued_cost(self):
        if not self.last_usage:
            return  0.0
        else:
            return self.last_usage.get("accrued_cost", 0.0)



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
    ):
        self.provider = "openai" if is_openai_model(model_name) else "vertex"
        self.model_name = model_name
        self.last_usage: Optional[Dict[str, int]] = None
        self._openai_params = None

        if self.provider == "vertex":
            self._vertex = VertexAI(
                project=vertex_project,
                location=vertex_region,
                model_name=model_name,
            )
            self._client = None
        elif self.provider == "openai":
            self._vertex = None
            self.model_name, self._openai_params = parse_model_name(self.model_name)
            self._client = OpenAI()
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def invoke(self, prompt: str) -> str:
        """
        Synchronous call, returns a plain string.
        Backend already knows how to handle str vs .content, so str is fine.
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
    ):
        self.provider = "openai" if is_openai_model(model_name) else "vertex"
        self.model_name = model_name
        self.last_usage: Optional[Dict[str, int]] = None
        self._openai_params = None

        if self.provider == "vertex":
            self._vertex = ChatVertexAI (
                project=vertex_project,
                location=vertex_region,
                model_name=model_name,
            )
            self._client = None
        elif self.provider == "openai":
            self._vertex = None
            self.model_name, self._openai_params = parse_model_name(self.model_name)
            self._client = OpenAI()
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def _to_openai_messages(self, messages: List[HumanMessage | AIMessage]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for m in messages:
            if isinstance(m, SystemMessage):
                role = "developer"
            if isinstance(m, HumanMessage):
                role = "user"
            elif isinstance(m, AIMessage):
                role = "assistant"
            else:
                role = "user"
            out.append({"role": role, "content": str(m.content)})
        return out

    def invoke(self, messages: List[HumanMessage | AIMessage]) -> str:
        """
        Synchronous chat call, returns text.
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
