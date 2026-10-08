"""
Priority Queue (Max-Heap)
==========================
Python's heapq is a min-heap, so we negate the urgency score to get
max-heap behaviour. Ties are broken by earliest created_at (FIFO among
equally urgent requests), which itself is broken by a monotonically
increasing sequence number so heap items are always orderable even if
two timestamps are identical.
"""
import heapq
import itertools


class UrgencyMaxHeap:
    def __init__(self):
        self._heap: list[tuple[float, float, int, int]] = []
        self._counter = itertools.count()
        self._removed: set[int] = set()

    def push(self, request_id: int, urgency_score: float, created_ts: float):
        # negate score -> smallest negative (= largest score) pops first
        heapq.heappush(self._heap, (-urgency_score, created_ts, next(self._counter), request_id))

    def remove(self, request_id: int):
        """Lazy removal: mark as removed, actually skipped on pop/peek."""
        self._removed.add(request_id)

    def pop(self) -> int | None:
        while self._heap:
            _, _, _, request_id = heapq.heappop(self._heap)
            if request_id in self._removed:
                self._removed.discard(request_id)
                continue
            return request_id
        return None

    def peek(self) -> int | None:
        while self._heap:
            neg_score, ts, seq, request_id = self._heap[0]
            if request_id in self._removed:
                heapq.heappop(self._heap)
                self._removed.discard(request_id)
                continue
            return request_id
        return None

    def ordered_ids(self) -> list[int]:
        """Non-destructive full ordering, for the dashboard list view."""
        items = [x for x in self._heap if x[3] not in self._removed]
        return [x[3] for x in sorted(items)]

    def __len__(self):
        return len(self._heap) - len(self._removed)


queue = UrgencyMaxHeap()
