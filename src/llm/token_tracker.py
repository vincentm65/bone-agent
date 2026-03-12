"""Token usage tracking for chat sessions."""

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
    def add_usage(self, usage_data):
        """Add token usage from API response.

        Args:
            usage_data: dict with 'prompt_tokens', 'completion_tokens' (total derived)
        """
        if not usage_data or not isinstance(usage_data, dict):
            return

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
    def get_session_summary(self):
        """Return formatted session usage summary string."""
        return (
            f"Session Input: [cyan]{self.total_prompt_tokens:,}[/cyan] | "
            f"Session Output: [cyan]{self.total_completion_tokens:,}[/cyan] | "
            f"Session Total: [cyan]{self.total_tokens:,}[/cyan]"
        )
    
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
        """Reset counters to zero or to specified values.

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

    def calculate_session_cost(self, cost_in: float, cost_out: float) -> dict:
        """Calculate session cost based on token usage.

        Args:
            cost_in: Cost per 1M input tokens
            cost_out: Cost per 1M output tokens

        Returns:
            Dict with 'input_cost', 'output_cost', 'total_cost' values
        """
        input_cost = (self.total_prompt_tokens / 1_000_000) * cost_in
        output_cost = (self.total_completion_tokens / 1_000_000) * cost_out
        return {
            'input_cost': input_cost,
            'output_cost': output_cost,
            'total_cost': input_cost + output_cost
        }

    def reset_conversation(self):
        """Reset conversation token counters (called on /new).

        Session totals (total_prompt_tokens, total_completion_tokens) are preserved.
        """
        self.conv_prompt_tokens = 0
        self.conv_completion_tokens = 0
        self.conv_total_tokens = 0

    def calculate_conversation_cost(self, cost_in: float, cost_out: float) -> dict:
        """Calculate conversation cost based on token usage.

        Args:
            cost_in: Cost per 1M input tokens
            cost_out: Cost per 1M output tokens

        Returns:
            Dict with 'input_cost', 'output_cost', 'total_cost' values
        """
        input_cost = (self.conv_prompt_tokens / 1_000_000) * cost_in
        output_cost = (self.conv_completion_tokens / 1_000_000) * cost_out
        return {
            'input_cost': input_cost,
            'output_cost': output_cost,
            'total_cost': input_cost + output_cost
        }

    def get_usage_for_prompt(self, context_limit: int = 200_000) -> str:
        """Get formatted usage information for inclusion in agent prompts.

        This provides agents with awareness of their token consumption to help
        them work within context limits. Shows total tokens burned (cumulative
        across all LLM calls), not just conversation context length.

        Args:
            context_limit: The context window limit to compare against (default: 200k)

        Returns:
            Formatted string with usage statistics and guidance
        """
        total_burned = self.total_tokens
        remaining = context_limit - total_burned
        percentage = (total_burned / context_limit) * 100

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
            f"**Status:** {urgency} | **Total Burned:** {total_burned:,} / {context_limit:,} ({percentage:.1f}%)\n"
            f"**Remaining:** {remaining:,} tokens\n\n"
            f"**Guidance:** {guidance}\n\n"
            f"**Note:** This count includes ALL tokens burned across the session "
            f"(all LLM calls, tool results, etc.), not just current conversation context."
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
