import time
import threading

from langchain_community.chat_message_histories.in_memory import ChatMessageHistory
from langchain_core.messages import HumanMessage, AIMessage

from classes.entities import Base
from classes.google_helpers import get_db_engine

from sqlalchemy.orm import sessionmaker


class HistoryCache:
    """
    In-memory, per-project chat history with:
    - sliding TTL (expires ttl_seconds after last touch)
    - approximate token cap (chars/4 heuristic)
    - thread-safe operations (single worker, but concurrent tasks)
    """

    def __init__(self, ttl_seconds: int, max_tokens: int):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._lock = threading.Lock()
        # project_id -> {"history": ChatMessageHistory, "expires_at": float}
        self._items: dict[str, dict[str, object]] = {}
        self.engine = get_db_engine()
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

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
        Also prunes to cap (under lock), and touches TTL.
        """
        pid = str(project_id)
        with self._lock:
            history = self._get_or_create_unlocked(pid)
            self._prune_to_token_cap_unlocked(history)
            self._touch_unlocked(pid)
            return list(history.messages)

    def append_turn(self, project_id: str, user_text: str, assistant_text: str) -> None:
        """
        Append user+assistant messages as a single turn and prune to cap.
        """
        pid = str(project_id)
        with self._lock:
            history = self._get_or_create_unlocked(pid)
            history.add_message(HumanMessage(content=user_text))
            history.add_message(AIMessage(content=assistant_text))
            self._prune_to_token_cap_unlocked(history)
            self._touch_unlocked(pid)

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


GLOBAL_BSS_HISTORY_CACHE = HistoryCache(ttl_seconds=24 * 3600, max_tokens=8000)
GLOBAL_HISTORY_CACHE= HistoryCache(ttl_seconds=24 * 3600, max_tokens=8000)
