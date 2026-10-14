"""What one request cost: external calls, cache hits and time per stage."""
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class RequestStats:
    external_calls: int = 0
    geocode_cache_hits: int = 0
    route_cache_hit: bool = False
    timings_ms: dict = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)

    @contextmanager
    def timed(self, stage):
        """Add the time spent inside the block to `timings_ms[stage]`."""
        began = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - began) * 1000
            self.timings_ms[stage] = self.timings_ms.get(stage, 0.0) + elapsed

    def total_ms(self):
        return (time.perf_counter() - self.started) * 1000
