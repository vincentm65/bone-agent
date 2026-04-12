"""Token usage tracking for chat sessions."""

from llm.config import get_model_cost


def usage_with_cost(response: dict) -> dict:
    """Extract usage dict from an LLM response, optionally including cost.

    Copies 'usage' (which may contain 'cost' for OpenRouter-style responses) and
    promotes a top-level 'cost' field (some providers) into the usage dict.
    This ensures any upstream-reported cost is captured regardless of location.

    Reduces repeated copy-and-merge boilerplate across call sites.

    Args:
        response: LLM response dict containing 'usage' (and optionally 'cost').

    Returns:
        dict with usage fields; empty dict if response has no 'usage'.
    """
    usage = dict(response.get("usage", {}))
    # Top-level cost takes precedence (some providers place it here)
    if "cost" in response:
        usage["cost"] = response["cost"]
    return usage


class TokenTracker:
    """Tracks token usage across a chat session."""

    def __init__(self):
        self.total_prompt_tokens = 0      # Cumulative input tokens (never reset by compaction)
        self.total_completion_tokens = 0  # Cumulative output tokens (never reset by compaction)
        self.total_tokens = 0              # Cumulative total tokens (never reset by compaction)

        # Conversation tokens: per-conversation billing (reset on /new)
        self.conv_prompt_tokens = 0       # Current conversation input tokens
        self.conv_completion_tokens = 0   # Current conversation output tokens
        self.conv_total_tokens = 0        # Current conversation total tokens

        # Context tokens: current conversation length (all messages in context)
        self.current_context_tokens = 0   # Updated via set_context_tokens()

        # Upstream-reported cost (e.g. OpenRouter's actual cost per request)
        self.total_actual_cost = 0.0       # Cumulative upstream-reported cost (never reset by compaction)
        self.conv_actual_cost = 0.0        # Per-conversation upstream-reported cost (reset on /new)

        # Config-estimated cost (fallback when upstream cost is absent)
        self.total_estimated_cost = 0.0    # Cumulative estimated cost (never reset by compaction)
        self.conv_estimated_cost = 0.0     # Per-conversation estimated cost (reset on /new)

        # Cache tokens: tracked when providers return cache breakdowns
        # Only input tokens can be cached (no output caching in any known API)
        self.total_cache_read_tokens = 0       # Cumulative input tokens read from cache
        self.total_cache_creation_tokens = 0   # Cumulative input tokens written to cache
        self.conv_cache_read_tokens = 0        # Per-conversation cache read tokens
        self.conv_cache_creation_tokens = 0    # Per-conversation cache creation tokens
    def add_usage(self, usage_data, model_name: str = ""):
        """Add token usage from an API response.

        Accepts either a full LLM response dict (non-streaming) or a pre-extracted
        usage dict (streaming). Full responses are normalized internally via
        usage_with_cost() to extract usage fields and promote top-level cost.

        Cost is resolved internally:
        1. Upstream-reported cost (e.g. OpenRouter's response['usage']['cost']) — most accurate
        2. Config-based fallback (tokens × rates from MODEL_PRICES) — used when upstream cost is absent

        Args:
            usage_data: Full LLM response dict (with 'usage' key) or pre-extracted
                        usage dict (with 'prompt_tokens', 'completion_tokens').
                        May also contain 'cost' (upstream-reported actual cost).
            model_name: Model name for config-based cost lookup (used as fallback).
        """
        if not usage_data or not isinstance(usage_data, dict):
            return

        # Normalize: full response dicts (non-streaming) have usage nested under
        # a 'usage' key with cost possibly at the top level. Extract and merge.
        # Pre-extracted usage dicts (streaming) pass through unchanged.
        if "usage" in usage_data:
            usage_data = usage_with_cost(usage_data)

        # Update cumulative token counts (accumulated for billing, never reset by compaction)
        prompt_tokens = usage_data.get('prompt_tokens', 0)
        completion_tokens = usage_data.get('completion_tokens', 0)
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens += prompt_tokens + completion_tokens

        # Update conversation token counts (reset on /new)
        self.conv_prompt_tokens += prompt_tokens
        self.conv_completion_tokens += completion_tokens
        self.conv_total_tokens += prompt_tokens + completion_tokens

        # Extract cache tokens from provider responses (if available)
        # Anthropic: cache_read_input_tokens, cache_creation_input_tokens
        # OpenAI: prompt_tokens_details.cached_tokens
        # Use explicit is-not-None checks to avoid treating 0 as falsy
        cache_read = usage_data.get('cache_read_input_tokens')
        if cache_read is None:
            cache_read = usage_data.get('cached_tokens')
        if cache_read is None:
            details = usage_data.get('prompt_tokens_details')
            cache_read = details.get('cached_tokens') if details else None
        cache_read = cache_read or 0

        cache_creation = usage_data.get('cache_creation_input_tokens', 0)
        self.total_cache_read_tokens += cache_read
        self.total_cache_creation_tokens += cache_creation
        self.conv_cache_read_tokens += cache_read
        self.conv_cache_creation_tokens += cache_creation

        # Record cost: upstream-reported takes priority; compute from config as fallback
        upstream_cost = usage_data.get('cost')
        if upstream_cost is not None:
            try:
                self.add_actual_cost(float(upstream_cost))
            except (ValueError, TypeError):
                pass
        else:
            # Fallback: look up cost rates from config
            cost_in, cost_out = get_model_cost(model_name)
            if cost_in > 0 or cost_out > 0:
                computed = self._calculate_cost(prompt_tokens, completion_tokens, cost_in, cost_out)
                self.add_estimated_cost(computed['total_cost'])

    def add_actual_cost(self, cost_usd: float):
        """Add upstream-reported actual cost for a request.

        Used when providers like OpenRouter return the exact cost in the response,
        which is more accurate than estimating from token counts × static rates.

        Args:
            cost_usd: Actual cost in USD for a single request
        """
        self.total_actual_cost += cost_usd
        self.conv_actual_cost += cost_usd

    def add_estimated_cost(self, cost_usd: float):
        """Add config-estimated cost for a request.

        Used as a fallback when providers do not return cost in the response.
        Estimated costs are tracked separately from upstream-reported actual costs
        so they remain distinguishable.

        Args:
            cost_usd: Estimated cost in USD for a single request
        """
        self.total_estimated_cost += cost_usd
        self.conv_estimated_cost += cost_usd

    def has_actual_cost(self) -> bool:
        """Whether any upstream-reported actual cost has been recorded."""
        return self.total_actual_cost > 0.0

    def has_estimated_cost(self) -> bool:
        """Whether any config-estimated cost has been recorded."""
        return self.total_estimated_cost > 0.0

    def has_cost(self) -> bool:
        """Whether any cost (actual or estimated) has been recorded."""
        return self.total_actual_cost > 0.0 or self.total_estimated_cost > 0.0

    def get_session_summary(self):
        """Return formatted session usage summary string."""
        parts = (
            f"Session Input: [#5F9EA0]{self.total_prompt_tokens:,}[/#5F9EA0] | "
            f"Session Output: [#5F9EA0]{self.total_completion_tokens:,}[/#5F9EA0] | "
            f"Session Total: [#5F9EA0]{self.total_tokens:,}[/#5F9EA0]"
        )
        total_cost = self.total_actual_cost + self.total_estimated_cost
        if total_cost > 0:
            parts += f" | Cost: [green]${total_cost:.4f}[/green]"
        return parts

    def get_all_token_counts(self):
        """Return all token counts as a dictionary for UI display.

        Returns:
            dict with keys: prompt_in, completion_out, total
        """
        return {
            'prompt_in': self.total_prompt_tokens,
            'completion_out': self.total_completion_tokens,
            'total': self.total_tokens
        }

    def reset(self, prompt_tokens=None, completion_tokens=None, total_tokens=None):
        """Reset token counters to zero or to specified values.

        Used by /clear to reset conversation context while preserving cumulative
        billing costs across the session.

        Args:
            prompt_tokens: If provided, set total_prompt_tokens to this value
            completion_tokens: If provided, set total_completion_tokens to this value
            total_tokens: If provided, set total_tokens to this value
        """
        self.total_prompt_tokens = prompt_tokens if prompt_tokens is not None else 0
        self.total_completion_tokens = completion_tokens if completion_tokens is not None else 0
        if total_tokens is None:
            self.total_tokens = self.total_prompt_tokens + self.total_completion_tokens
        else:
            self.total_tokens = total_tokens
        self.current_context_tokens = 0  # Reset context tokens
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0
        # Note: total_actual_cost and total_estimated_cost are preserved across resets (cumulative billing)

    def reset_all(self):
        """Full reset of all counters including cost accumulators.

        Used on provider switch to clear stale cost state from the previous
        provider. Unlike reset(), this zeros actual/estimated costs so the
        new provider starts with a clean billing slate.
        """
        self.reset()
        self.total_actual_cost = 0.0
        self.total_estimated_cost = 0.0
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0

    @staticmethod
    def estimate_tokens(text, model=""):
        """Estimate token count using tiktoken.

        Args:
            text: String to estimate tokens for
            model: Optional model name for encoding selection (uses cl100k_base if empty)

        Returns:
            Estimated token count (int)
        """
        if not text:
            return 0

        try:
            import tiktoken
            try:
                enc = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("cl100k_base")
            except Exception:
                enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            # Fallback to character-based approximation if tiktoken not available
            return len(text) // 4

    def set_context_tokens(self, token_count):
        """Set the current context token count.

        Args:
            token_count: Actual token count of the current message list
        """
        self.current_context_tokens = token_count

    @staticmethod
    def _calculate_cost(prompt_tokens: int, completion_tokens: int, cost_in: float, cost_out: float) -> dict:
        """Core cost formula: (tokens / 1M) * rate."""
        input_cost = (prompt_tokens / 1_000_000) * cost_in
        output_cost = (completion_tokens / 1_000_000) * cost_out
        return {
            'input_cost': input_cost,
            'output_cost': output_cost,
            'total_cost': input_cost + output_cost,
        }

    def reset_conversation(self):
        """Reset conversation token counters (called on /new).

        Session totals (total_prompt_tokens, total_completion_tokens) are preserved.
        """
        self.conv_prompt_tokens = 0
        self.conv_completion_tokens = 0
        self.conv_total_tokens = 0
        self.conv_actual_cost = 0.0
        self.conv_estimated_cost = 0.0
        self.conv_cache_read_tokens = 0
        self.conv_cache_creation_tokens = 0

    def get_usage_for_prompt(self, context_limit: int = 200_000) -> str:
        """Get formatted usage information for inclusion in agent prompts.

        This provides agents with awareness of their current context window
        usage to help them work within context limits. Urgency is based on
        actual context length (current_context_tokens), not cumulative billing.

        Args:
            context_limit: The context window limit to compare against (default: 200k)

        Returns:
            Formatted string with usage statistics and guidance
        """
        context_used = self.current_context_tokens
        total_burned = self.total_tokens
        remaining = context_limit - context_used
        percentage = (context_used / context_limit) * 100

        # Determine urgency level
        if percentage >= 90:
            urgency = "CRITICAL"
            guidance = "You have nearly exhausted your token budget. Be extremely concise and limit exploration."
        elif percentage >= 75:
            urgency = "HIGH"
            guidance = "You are approaching your token limit. Prioritize focused exploration over breadth."
        elif percentage >= 50:
            urgency = "MODERATE"
            guidance = "You have used half your token budget. Be mindful of exploration scope."
        else:
            urgency = "LOW"
            guidance = "Token usage is within normal bounds."

        return (
            f"## Token Usage Awareness\n\n"
            f"**Status:** {urgency} | **Context:** {context_used:,} / {context_limit:,} ({percentage:.1f}%)\n"
            f"**Remaining:** {remaining:,} tokens | **Session total burned:** {total_burned:,}\n\n"
            f"**Guidance:** {guidance}\n\n"
            f"**Note:** Context shows current conversation length; session total is cumulative across all LLM calls."
        )

    def get_context_summary(self) -> str:
        """Get a brief summary of current context usage.

        Returns:
            Concise string with context and session totals
        """
        return (
            f"Context: {self.current_context_tokens:,} tokens | "
            f"Session total burned: {self.total_tokens:,} tokens"
        )

    def get_display_cost(self, model_name: str = "") -> float:
        """Get the cost to display in UI (session-level).

        Priority:
        1. Upstream-reported actual cost (most accurate, e.g. OpenRouter)
        2. Config-based fallback (tokens x rates from MODEL_PRICES)

        Args:
            model_name: Model name for config-based cost lookup (fallback).

        Returns:
            Total cost in USD, or 0.0 if neither source is available
        """
        # If we have upstream-reported cost, use it (most accurate)
        if self.has_actual_cost():
            return self.total_actual_cost + self.total_estimated_cost
        # Fallback: full config-based recalculation for all tokens
        cost_in, cost_out = get_model_cost(model_name)
        if cost_in > 0 or cost_out > 0:
            return self._calculate_cost(
                self.total_prompt_tokens, self.total_completion_tokens,
                cost_in, cost_out
            )['total_cost']
        return 0.0

    def get_conversation_display_cost(self, cost_in: float, cost_out: float) -> float:
        """Get the cost to display for conversation-level (reset on /new).

        For callers that already have cost rates (e.g. config_manager), this
        computes directly.

        Returns:
            Conversation cost in USD
        """
        return self._calculate_cost(
            self.conv_prompt_tokens, self.conv_completion_tokens,
            cost_in, cost_out
        )['total_cost']
