import time
import threading

from langchain_community.chat_message_histories.in_memory import ChatMessageHistory
from langchain_core.messages import HumanMessage, AIMessage

from classes.entities import Base

class HistoryCache:
    """
    In-memory, per-project chat history with:
    - sliding TTL (expires ttl_seconds after last touch)
    - approximate token cap (chars/4 heuristic)
    - max message cap (keeps only the most recent N messages)
    - thread-safe operations (single worker, but concurrent tasks)
    """

    def __init__(self, ttl_seconds: int, max_tokens: int, max_messages: int | None = 40):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        # If you ever want "no message cap", pass max_messages=None
        self.max_messages = max_messages
        self._lock = threading.Lock()
        # project_id -> {"history": ChatMessageHistory, "expires_at": float}
        self._items: dict[str, dict[str, object]] = {}

    def _approx_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _touch_unlocked(self, project_id: str) -> None:
        now = time.time()
        item = self._items.get(project_id)
        if item is not None:
            item["expires_at"] = now + self.ttl_seconds

    def _get_or_create_unlocked(self, project_id: str) -> ChatMessageHistory:
        now = time.time()
        item = self._items.get(project_id)

        if item is not None:
            expires_at = float(item["expires_at"])
            if expires_at > now:
                item["expires_at"] = now + self.ttl_seconds
                return item["history"]  # type: ignore[return-value]
            # expired -> replace
            del self._items[project_id]

        history = ChatMessageHistory()
        self._items[project_id] = {"history": history, "expires_at": now + self.ttl_seconds}
        return history

    def get(self, project_id: str) -> ChatMessageHistory:
        with self._lock:
            return self._get_or_create_unlocked(str(project_id))

    def snapshot(self, project_id: str) -> list:
        """
        Returns a COPY of the current message list for LLM input.
        Also prunes to caps (tokens + max_messages) under lock, and touches TTL.
        """
        pid = str(project_id)
        with self._lock:
            history = self._get_or_create_unlocked(pid)
            self._prune_unlocked(history)
            self._touch_unlocked(pid)
            return list(history.messages)

    def append_turn(self, project_id: str, user_text: str, assistant_text: str) -> None:
        """
        Append user+assistant messages as a single turn and prune to caps.
        """
        pid = str(project_id)
        with self._lock:
            history = self._get_or_create_unlocked(pid)
            history.add_message(HumanMessage(content=user_text))
            history.add_message(AIMessage(content=assistant_text))
            self._prune_unlocked(history)
            self._touch_unlocked(pid)

    def _prune_unlocked(self, history: ChatMessageHistory) -> None:
        """
        Apply both token-based and message-count-based pruning.
        Oldest messages are dropped first.
        """
        self._prune_to_token_cap_unlocked(history)
        self._prune_to_message_cap_unlocked(history)

    def _prune_to_token_cap_unlocked(self, history: ChatMessageHistory) -> None:
        msgs = list(history.messages)

        tokens = []
        total = 0
        for m in msgs:
            content = getattr(m, "content", "") or ""
            t = self._approx_tokens(str(content))
            tokens.append(t)
            total += t

        if total <= self.max_tokens:
            return

        # drop from front until under cap
        i = 0
        while i < len(msgs) and total > self.max_tokens:
            total -= tokens[i]
            i += 1

        history.messages = msgs[i:]

    def _prune_to_message_cap_unlocked(self, history: ChatMessageHistory) -> None:
        """
        Keep at most self.max_messages most recent messages.
        """
        if self.max_messages is None:
            return

        msgs = list(history.messages)
        if len(msgs) <= self.max_messages:
            return

        # Keep only the last max_messages (drop oldest first)
        history.messages = msgs[-self.max_messages :]

    def sweep_expired(self) -> int:
        """
        Delete expired histories. Safe to call every AsyncGuard cycle.
        Returns how many entries were removed.
        """
        now = time.time()
        removed = 0
        with self._lock:
            expired = [k for k, v in self._items.items() if float(v["expires_at"]) <= now]
            for k in expired:
                del self._items[k]
                removed += 1
        return removed


GLOBAL_BSS_HISTORY_CACHE = HistoryCache(ttl_seconds=24 * 3600, max_tokens=8000, max_messages=40)
GLOBAL_HISTORY_CACHE = HistoryCache(ttl_seconds=24 * 3600, max_tokens=8000, max_messages=40)
