# classes/idempotency_cache.py

import threading
from typing import Iterable, List, Set

from sqlalchemy.orm import Session, sessionmaker

from classes.entities import Base, PendingCharge
from classes.GCConnection_hlpr import get_session_factory


class IdempotencyCache:
    """
    Process-local cache of idempotency keys.

    - No TTL.
    - Keys are removed when the corresponding PendingCharge row is CHARGED.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: Set[str] = set()

    def add(self, key: str) -> None:
        if not key:
            return
        with self._lock:
            self._keys.add(str(key))

    def remove(self, key: str) -> None:
        if not key:
            return
        with self._lock:
            self._keys.discard(str(key))

    def snapshot(self) -> List[str]:
        """
        Return a copy of all keys currently tracked.
        """
        with self._lock:
            return list(self._keys)

    def sweep_charged(self) -> int:
        """
        Look up cached keys in the DB and remove those whose
        PendingCharge.status == 'CHARGED'.

        Returns how many keys were removed.
        """
        with self._lock:
            keys = list(self._keys)

        if not keys:
            return 0

        sf = get_session_factory()
        session = sf()
        try:
            rows: Iterable[tuple[str, str]] = (
                session.query(
                    PendingCharge.idempotency_key,
                    PendingCharge.status,
                )
                .filter(PendingCharge.idempotency_key.in_(keys))
                .all()
            )
        finally:
            session.close()

        to_remove = [k for (k, status) in rows if status == "CHARGED"]
        if not to_remove:
            return 0

        removed = 0
        with self._lock:
            for k in to_remove:
                if k in self._keys:
                    self._keys.remove(k)
                    removed += 1

        return removed


# Global, process-local singleton
IDEMPOTENCY_CACHE = IdempotencyCache()
