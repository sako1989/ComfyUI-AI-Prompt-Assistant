"""Aggregate CLI-reported usage across all stages of one node execution."""

import math


class UsageCounter:
    def __init__(self, backend):
        self.backend = backend
        self.calls = 0
        self.values = {}
        self.reported = {}

    def add(self, usage):
        self.calls += 1
        usage = usage or {}
        values = dict(usage)
        # Anthropic reports cache tokens separately from uncached input. Codex's
        # input_tokens already includes cached input; do not count those twice.
        if self.backend == "claude_code" and isinstance(values.get("input_tokens"), (int, float)):
            values["input_tokens"] += (usage.get("cache_read") or 0) + (usage.get("cache_creation") or 0)
        for key in ("input_tokens", "output_tokens", "cache_read", "cache_creation", "cost_usd"):
            value = values.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                self.values[key] = self.values.get(key, 0) + value
                self.reported[key] = self.reported.get(key, 0) + 1

    def snapshot(self):
        result = {key: self.values.get(key) for key in
                  ("input_tokens", "output_tokens", "cache_read", "cache_creation", "cost_usd")}
        result["calls"] = self.calls
        result["tokens_partial"] = any(self.reported.get(k, 0) < self.calls for k in ("input_tokens", "output_tokens"))
        result["cost_partial"] = self.reported.get("cost_usd", 0) < self.calls
        result["total_tokens"] = (result["input_tokens"] + result["output_tokens"]
                                  if result["input_tokens"] is not None and result["output_tokens"] is not None else None)
        return result
