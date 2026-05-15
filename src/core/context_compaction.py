"""Context compaction: token estimation, tool-result summarization, history
compaction, and emergency truncation — extracted from ChatManager."""

import json
import logging

from llm.config import get_provider_config
from utils.settings import context_settings
from utils.result_parsers import extract_exit_code, extract_metadata_from_result
from utils.multimodal import content_text_for_logs
from utils.terminal_sanitize import SanitizedMessageList

logger = logging.getLogger(__name__)

# Token counting constants
MESSAGE_OVERHEAD_TOKENS = 4  # Approximate tokens for JSON structure: braces, quotes, colons, commas

# Action labels for context management notifications (used by ensure_context_fits)
_ACTION_LABELS = {
    "tool_compaction": "compacted tool results",
    "history_compaction": "compacted history",
    "emergency_truncation": "emergency truncation (oldest messages dropped)",
}

# Module-level cache for the tiktoken encoder (cl100k_base only).
# If cl100k_base is unavailable (e.g. first-use download interrupted),
# p50k_base is used temporarily without caching so cl100k_base is retried
# on the next call.
_tiktoken_encoder_cache = None


class ContextCompaction:
    """Holds all compaction-related state and methods for ChatManager.

    Extracted from ChatManager to reduce its size by ~900 LOC.  Methods
    access ChatManager attributes through the ``_cm`` back-reference.
    """

    def __init__(self, chat_manager):
        self._cm = chat_manager  # back-reference to ChatManager

    # -- Payload-based token estimation helpers --------------------------------

    @staticmethod
    def _serialize_message_payload(msg) -> str:
        """Serialize a message dict to compact JSON (no ASCII escaping)."""
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _estimate_tokens_for_text(text: str) -> int:
        """Estimate tokens via tiktoken (cl100k_base) with byte-aware fallback."""
        global _tiktoken_encoder_cache
        try:
            import tiktoken
            if _tiktoken_encoder_cache is None:
                try:
                    _tiktoken_encoder_cache = tiktoken.get_encoding("cl100k_base")
                except Exception as exc:
                    logger.warning("cl100k_base encoder unavailable, trying p50k_base: %s", exc)
                    try:
                        enc = tiktoken.get_encoding("p50k_base")
                        return len(enc.encode(text))  # use but don't cache
                    except Exception:
                        pass  # fall through to outer except → byte-aware fallback
            return len(_tiktoken_encoder_cache.encode(text))
        except Exception:
            # Conservative fallback: never undercount high-token-density text.
            return max(len(text) // 4, len(text.encode("utf-8")) // 4)

    def _count_tokens(self, messages) -> int:
        """Count tokens for a message list via payload serialization."""
        total = 0
        for msg in messages:
            payload = self._serialize_message_payload(msg)
            total += self._estimate_tokens_for_text(payload)
            total += MESSAGE_OVERHEAD_TOKENS
        return total

    def _estimate_message_tokens(self, msg) -> int:
        """Per-message token estimate for boundary calculation."""
        payload = self._serialize_message_payload(msg)
        return self._estimate_tokens_for_text(payload)

    # -- Summary / building helpers ---------------------------------------------

    def _build_summary_prompt(self, messages) -> str:
        """Generate a comprehensive summary of messages.

        Captures:
        - User questions asked
        - Tool calls performed (files read, edits, searches)
        - Key decisions and changes

        Args:
            messages: List of messages to summarize

        Returns:
            str: Structured summary preserving context
        """
        # Extract user questions
        user_queries = []
        for m in messages:
            if m.get('role') == 'user':
                content = content_text_for_logs(m.get('content', ''))
                if content and not content.startswith("The codebase map"):
                    user_queries.append(content)

        # Extract tool calls
        tool_calls = []
        for m in messages:
            if m.get('tool_calls'):
                for tc in m['tool_calls']:
                    fn = tc['function']
                    name = fn.get('name', '')
                    args = fn.get('arguments', '')
                    tool_calls.append(f"- {name}: {args[:100]}")
            elif m.get('role') == 'tool':
                # Extract tool result metadata
                content = content_text_for_logs(m.get('content', ''))
                if 'exit_code=' in content:
                    lines = content.split('\n')[:5]  # First 5 lines for context
                    tool_calls.append(f"Result: {'; '.join(lines[:2])}")

        # Build summary prompt
        summary_prompt = f"""Summarize the following conversation context.

User questions:
{chr(10).join(f'- {q}' for q in user_queries) if user_queries else 'None'}

Tool operations performed:
{chr(10).join(tool_calls) if tool_calls else 'None'}

Focus on:
1. What problem was being solved
2. What files were read or modified
3. What searches were performed
4. Key code changes or decisions made
5. Current state/progress

Provide a concise summary (2-4 paragraphs) that captures all essential context for continuing the work."""

        return summary_prompt

    def _summarize_tool_call(self, tool_call, tool_result):
        """Extract key info from a single tool call.

        Args:
            tool_call: Tool call dict from message
            tool_result: Tool result content string

        Returns:
            str: Summary string for this tool
        """
        try:
            import json
            fn_name = tool_call['function']['name']
            args = json.loads(tool_call['function']['arguments'])
        except (json.JSONDecodeError, KeyError):
            return "Used a tool"

        if fn_name == "execute_command":
            cmd = args.get('command', '')
            exit_code = extract_exit_code(tool_result)
            matches = extract_metadata_from_result(tool_result, 'matches_found')

            if exit_code == 0:
                if matches is not None:
                    return f"Searched for '{cmd[:50]}...' (found {matches} matches)"
                else:
                    return f"Searched: '{cmd[:50]}...'"
            else:
                return f"Search failed: '{cmd[:30]}...'"

        elif fn_name == "read_file":
            path = args.get('path_str', '')
            lines = extract_metadata_from_result(tool_result, 'lines_read')
            start_line = extract_metadata_from_result(tool_result, 'start_line')

            if lines is not None:
                if start_line is not None and start_line > 1:
                    end_line = start_line + lines - 1
                    return f"Read {path} (lines {start_line}-{end_line})"
                else:
                    return f"Read {path} ({lines} lines)"
            else:
                return f"Read {path}"

        elif fn_name == "list_directory":
            path = args.get('path_str', '.')
            items = extract_metadata_from_result(tool_result, 'items_count')
            recursive = args.get('recursive', False)

            action = "Listed recursively" if recursive else "Listed"
            if items is not None:
                return f"{action} {path} ({items} items)"
            return f"{action} {path}"

        elif fn_name == "edit_file":
            path = args.get('path', '')
            search = args.get('search', '')
            search_preview = search[:30] + "..." if len(search) > 30 else search
            return f"Edited {path} (replaced '{search_preview}')"

        elif fn_name == "web_search":
            query = args.get('query', '')
            results = extract_metadata_from_result(tool_result, 'results_found')
            if results is not None:
                return f"Searched web for '{query[:40]}...' ({results} results)"
            return f"Searched web: '{query[:40]}...'"

        elif fn_name == "sub_agent":
            query = args.get('query', '')
            if "Sub-agent stopped before completion" in tool_result:
                return f"Sub-agent stopped at token limit: '{query[:50]}...'"
            return f"Ran sub-agent: '{query[:50]}...'"

        return f"Used {fn_name}"

    def _generate_tool_block_summary(self, tool_calls, tool_results):
        """Generate a single summary line for all tools in a block.

        Args:
            tool_calls: List of tool call dicts
            tool_results: List of tool result strings

        Returns:
            str: Human-readable summary
        """
        # Group tools by type for better readability
        searches = []
        reads = []
        lists = []
        edits = []
        web = []
        failed = []

        for i, tool_call in enumerate(tool_calls):
            result = tool_results[i] if i < len(tool_results) else ""
            summary = self._summarize_tool_call(tool_call, result)

            if "failed" in summary.lower():
                failed.append(summary)
            elif "searched" in summary.lower() and "web" not in summary.lower():
                searches.append(summary)
            elif "read" in summary.lower():
                reads.append(summary)
            elif "listed" in summary.lower():
                lists.append(summary)
            elif "edited" in summary.lower():
                edits.append(summary)
            elif "web" in summary.lower():
                web.append(summary)

        # Build human-readable summary
        parts = []

        if searches:
            count = len(searches)
            if count == 1:
                parts.append(searches[0])
            else:
                parts.append(f"performed {count} searches")

        if reads:
            if len(reads) == 1:
                parts.append(reads[0])
            else:
                parts.append(f"read {len(reads)} files")

        if lists:
            parts.append(lists[0] if len(lists) == 1 else "listed directories")

        if edits:
            parts.append(edits[0] if len(edits) == 1 else f"made {len(edits)} edits")

        if web:
            parts.append(web[0] if len(web) == 1 else "performed web searches")

        if failed:
            parts.append(f"{len(failed)} tool(s) failed")

        if not parts:
            return "Used tools for exploration"

        # Join with natural language
        if len(parts) <= 2:
            return " and ".join(parts) + "."
        else:
            first = ", ".join(parts[:-1])
            return f"{first}, and {parts[-1]}."

    # -- Block finding and boundary computation --------------------------------

    def _find_tool_blocks(self, include_in_flight=False):
        """Find all tool-result blocks in message history.

        Handles both single-turn and multi-turn tool chains:
          Single: user → assistant(tc) → tool_results → assistant(answer)
          Multi:  user → assistant(tc1) → tools → assistant(tc2) → tools → assistant(answer)

        In multi-turn chains, all tool_calls and tool_results are merged into
        a single block spanning from the first assistant(tool_calls) to the
        final assistant(answer).

        Args:
            include_in_flight: If True, also return blocks that lack a final
                assistant answer (in-flight tool chains). The 'end' field points
                to the index after the last message in the chain (or the breaking
                message index if the chain was interrupted).

        Returns:
            list: List of block dicts with keys: user_idx, start, end, tool_calls, tool_results
        """
        blocks = []
        i = 0

        while i < len(self._cm.messages):
            msg = self._cm.messages[i]

            # Look for assistant message with tool_calls
            if msg.get('role') == 'assistant' and msg.get('tool_calls'):

                # Find user question before this
                user_idx = i - 1
                while user_idx >= 0 and self._cm.messages[user_idx].get('role') != 'user':
                    user_idx -= 1

                if user_idx < 0:
                    i += 1
                    continue

                # Follow consecutive assistant(tool_calls) → tool_results pairs
                # until we reach a final answer (assistant without tool_calls)
                block_start = i
                all_tool_calls = []
                all_tool_results = []
                j = i
                found_end = False

                while j < len(self._cm.messages):
                    if self._cm.messages[j].get('role') == 'assistant' and self._cm.messages[j].get('tool_calls'):
                        # Accumulate tool calls from this assistant message
                        all_tool_calls.extend(self._cm.messages[j].get('tool_calls', []))
                        # Collect immediately following tool results
                        k = j + 1
                        while k < len(self._cm.messages) and self._cm.messages[k].get('role') == 'tool':
                            all_tool_results.append(self._cm.messages[k].get('content', ''))
                            k += 1
                        j = k
                    elif self._cm.messages[j].get('role') == 'assistant' and not self._cm.messages[j].get('tool_calls'):
                        # Final answer — this completes the block
                        found_end = True
                        break
                    else:
                        # Non-tool, non-assistant message breaks the chain
                        break

                if include_in_flight:
                    if all_tool_calls:
                        blocks.append({
                            'user_idx': user_idx,
                            'start': block_start,
                            'end': j,
                            'tool_calls': all_tool_calls,
                            'tool_results': all_tool_results,
                            'in_flight': not found_end,
                        })
                else:
                    if found_end and all_tool_calls:
                        blocks.append({
                            'user_idx': user_idx,
                            'start': block_start,
                            'end': j,
                            'tool_calls': all_tool_calls,
                            'tool_results': all_tool_results,
                        })

                # Continue scanning from after the final answer (or after the chain)
                # Guard: always advance at least one position to prevent infinite loops
                i = max(i + 1, j + 1 if found_end else j)
            else:
                i += 1

        return blocks

    def _find_in_flight_boundary(self):
        """Return index of first in-flight message, or len(messages) if none."""
        all_blocks = self._find_tool_blocks(include_in_flight=True)
        in_flight = [b for b in all_blocks if b.get('in_flight')]
        if in_flight:
            return min(b['user_idx'] for b in in_flight)
        return len(self._cm.messages)

    def _compute_split_boundary(self, blocks, in_flight_start,
                                uncompacted_tail_tokens=None, min_tool_blocks=None):
        """Compute the message index where the uncompacted tail begins.

        Three constraints determine the boundary (take the most conservative /
        earliest index):
        1. Token budget: accumulate from the end until uncompacted_tail_tokens
        2. Minimum tool blocks: preserve at least min_tool_blocks completed blocks
        3. Tool-call integrity: never split inside a tool block
        4. In-flight boundary: never include in-flight tool messages

        Args:
            blocks: List of tool block dicts from _find_tool_blocks()
            in_flight_start: Index of first in-flight message (from _find_in_flight_boundary)
            uncompacted_tail_tokens: Override for the token budget (None = use settings)
            min_tool_blocks: Override for minimum tool blocks to preserve (None = use settings)

        Returns:
            int: Message index where the uncompacted tail starts
        """
        tc = context_settings.tool_compaction
        token_budget = uncompacted_tail_tokens if uncompacted_tail_tokens is not None else tc.limit_tokens
        min_blocks = min_tool_blocks if min_tool_blocks is not None else tc.min_tool_blocks
        if token_budget is None:
            return 1
        n = len(self._cm.messages)

        # The verbatim region ends at the first in-flight message (exclusive)
        verbatim_end = min(in_flight_start, n)

        # Constraint 1: Token budget — walk from verbatim_end backward.
        # Note: range stops at 1 (not 0) so the system prompt is never counted
        # toward the budget — it is always preserved uncompacted.
        tokens_accumulated = 0
        token_boundary = 0
        for i in range(verbatim_end - 1, 0, -1):
            tokens_accumulated += self._estimate_message_tokens(self._cm.messages[i])
            if tokens_accumulated >= token_budget:
                token_boundary = i
                break
        else:
            # All messages fit within budget
            token_boundary = 1

        # Constraint 2: Minimum tool blocks — ensure at least min_blocks completed
        # blocks are within the uncompacted tail. Take the min_blocks most recent
        # completed blocks and set the boundary so they all fall at or after it.
        min_block_boundary = 1
        if min_blocks > 0 and len(blocks) >= min_blocks:
            # Sort by end index descending (most recent first), take top min_blocks
            sorted_blocks = sorted(blocks, key=lambda b: b['end'], reverse=True)
            recent_blocks = sorted_blocks[:min_blocks]
            # The boundary must be at or before the earliest user_idx of these blocks
            # so that all of them satisfy user_idx >= boundary (i.e. block is fully in the tail)
            min_block_boundary = min(b['user_idx'] for b in recent_blocks)

        # Constraint 3: Tool-call integrity — if token_boundary lands inside a
        # tool block, extend backward to include the complete block
        integrity_boundary = token_boundary
        for block in blocks:
            if block['user_idx'] < token_boundary <= block['end']:
                # Split would cut through this block — extend to include it
                integrity_boundary = min(integrity_boundary, block['user_idx'])

        # Take the most conservative (earliest) boundary
        # integrity_boundary <= token_boundary always (starts equal, only decreases)
        boundary = integrity_boundary
        if min_block_boundary < boundary:
            boundary = min_block_boundary

        return boundary

    # -- Compaction orchestration ----------------------------------------------

    def compact_tool_results(self, skip_token_update=False,
                              uncompacted_tail_tokens=None, min_tool_blocks=None):
        """Replace completed tool-result blocks with summaries using token-budget tail.

        Walks messages from the end, accumulating tokens until ~40k tokens are
        reached. Everything before that boundary gets compacted (completed tool
        blocks replaced with summary lines). Always preserves at least
        min_tool_blocks completed blocks regardless of token budget.

        Safe to call mid-loop (during tool execution) because it only compacts
        completed tool blocks — in-flight blocks are never touched.

        Args:
            skip_token_update: If True, skip the internal _update_context_tokens()
                call. Use when the caller will update tokens with mode-specific
                tools immediately after.
            uncompacted_tail_tokens: Override for the token budget (None = use settings).
                Use for aggressive compaction with a smaller tail.
            min_tool_blocks: Override for minimum tool blocks to preserve (None = use settings).
                Use for aggressive compaction with fewer preserved blocks.
        """
        # Skip if disabled (e.g. sub-agents preserving findings)
        if self._cm._compaction_disabled:
            return

        if context_settings.tool_compaction.limit_tokens is None and uncompacted_tail_tokens is None:
            return

        # Safety: Don't compact if very few messages
        if len(self._cm.messages) < 6:  # Minimum: user+assistant+tool+assistant+user+assistant
            return

        # Routine tool compaction runs only when the current context reaches
        # the configured token limit. Aggressive callers pass explicit overrides.
        is_aggressive = uncompacted_tail_tokens is not None or min_tool_blocks is not None
        if not is_aggressive:
            tc = context_settings.tool_compaction
            self._cm._update_context_tokens(force=True)
            current = self._cm.token_tracker.current_context_tokens
            if not isinstance(tc.limit_tokens, int) or current < tc.limit_tokens:
                return

        # Find completed tool-result blocks
        blocks = self._find_tool_blocks()

        if not blocks:
            return

        # Find where in-flight tool blocks begin (if any)
        in_flight_start = self._find_in_flight_boundary()

        # Compute the split boundary using token budget + constraints
        split_boundary = self._compute_split_boundary(
            blocks, in_flight_start,
            uncompacted_tail_tokens=uncompacted_tail_tokens,
            min_tool_blocks=min_tool_blocks,
        )

        # Determine which blocks fall entirely before the split boundary
        # (those are the ones to compact)
        blocks_to_compact = [
            b for b in blocks
            if b['end'] < split_boundary
        ]

        if not blocks_to_compact:
            return

        # Build the new message list
        new_messages = []
        processed_indices = set()

        for i, msg in enumerate(self._cm.messages):
            if i in processed_indices:
                continue

            # Check if this is the start of a block to compact
            block = next((b for b in blocks_to_compact if b['start'] == i), None)

            if block:
                # Check if any tool in this block failed
                skip_compaction = False
                if not context_settings.tool_compaction.compact_failed_tools:
                    for tool_result in block['tool_results']:
                        exit_code = extract_exit_code(tool_result)
                        if exit_code is not None and exit_code != 0:
                            skip_compaction = True
                            break

                if skip_compaction:
                    # Keep this block as-is
                    for idx in range(block['user_idx'], block['end'] + 1):
                        new_messages.append(self._cm.messages[idx])
                        processed_indices.add(idx)
                    continue

                # Generate summary and replace block
                summary = self._generate_tool_block_summary(
                    block['tool_calls'],
                    block['tool_results']
                )

                # Add user question with summary appended
                user_msg = self._cm.messages[block['user_idx']].copy()
                content = user_msg.get('content', '')
                context_text = f"\n\n[Context: {summary}]"
                if isinstance(content, str):
                    user_msg['content'] = content + context_text
                elif isinstance(content, list):
                    user_msg['content'] = content + [{"type": "text", "text": context_text}]
                else:
                    user_msg['content'] = f"{content}\n\n[Context: {summary}]"
                new_messages.append(user_msg)

                # Add final assistant answer
                new_messages.append(self._cm.messages[block['end']])

                # Mark all indices as processed
                processed_indices.add(block['user_idx'])
                for idx in range(block['start'], block['end'] + 1):
                    processed_indices.add(idx)
            else:
                # Keep this message as-is
                new_messages.append(msg)

        self._cm.messages = SanitizedMessageList(new_messages)
        if not skip_token_update:
            self._cm._update_context_tokens(force=True)
        else:
            self._cm.mark_context_dirty()

    # ===== AI-Based History Compaction =====

    def compact_history(self, console=None, trigger="manual"):
        """Compact chat history while preserving recent context.

        Strategy:
        1. Keep last user message verbatim
        2. Keep assistant tool_calls message (if present) for context
        3. Keep last assistant response (without tool calls) verbatim
        4. Summarize everything prior AND all tool result messages

        Args:
            console: Console for notifications (None for silent auto-compact)
            trigger: "manual" or "auto"

        Returns:
            dict with compaction stats or None
        """
        if len(self._cm.messages) < 10:  # Need enough history
            return None

        # Find the last user message (start from end, skip system/tool messages)
        last_user_idx = None
        for i in range(len(self._cm.messages) - 1, -1, -1):
            role = self._cm.messages[i].get('role')
            # Look for user message that's not the codebase map
            if role == 'user' and not self._cm.messages[i].get('tool_calls'):
                content = content_text_for_logs(self._cm.messages[i].get('content', ''))
                if content and not content.startswith("The codebase map"):
                    last_user_idx = i
                    break

        if last_user_idx is None or last_user_idx < 3:
            return None  # Not enough history to compact

        # Find the last assistant message WITHOUT tool calls (final answer)
        last_assistant_without_tools_idx = None
        for i in range(len(self._cm.messages) - 1, -1, -1):
            msg = self._cm.messages[i]
            if msg.get('role') == 'assistant' and not msg.get('tool_calls'):
                # This is a final answer
                last_assistant_without_tools_idx = i
                break

        if last_assistant_without_tools_idx is None:
            return None  # No final answer found

        # Determine what to keep vs summarize
        # We always keep: system prompt, last user message, assistant tool_calls (if present), last assistant answer
        # We summarize: everything between system prompt and last user message,
        #              AND all tool result messages (but not the tool_calls message)

        # Case 1: Last assistant answer is directly after last user message
        #         (no tools were called)
        if last_assistant_without_tools_idx == last_user_idx + 1:
            # Original behavior: keep from last_user_idx, summarize before
            messages_to_keep = self._cm.messages[last_user_idx:]
            messages_to_summarize = self._cm.messages[1:last_user_idx]
        else:
            # Case 2: There are tool interactions between last user and last assistant
            #         Keep: last user message + entire tool exchange + final answer
            #         Summarize: everything before last user message
            #
            # The tail from last_user_idx through last_assistant_without_tools_idx
            # is a valid message sequence (user → assistant(tool_calls) → tool results → assistant(answer))
            # and must be kept intact to avoid consecutive assistant messages or orphaned tool_call_ids.
            messages_to_keep = self._cm.messages[last_user_idx:]
            messages_to_summarize = self._cm.messages[1:last_user_idx]

        if not messages_to_summarize:
            return None

        # Generate comprehensive summary using extracted context
        summary_prompt_content = self._build_summary_prompt(messages_to_summarize)

        # Track token counts before (total tokens including system prompt + messages + tools)
        self._cm._update_context_tokens(force=True)
        tokens_before = self._cm.token_tracker.current_context_tokens

        # Call LLM to generate summary
        summary_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant that summarizes conversation context. "
                    "Provide clear, concise summaries that capture essential information for continuing work."
                ),
            },
            {
                "role": "user",
                "content": summary_prompt_content,
            },
        ]

        try:
            response = self._cm.client.chat_completion(summary_prompt, stream=False, tools=None)
        except Exception as e:
            if console and trigger == "manual":
                console.print(f"Compaction failed: {e}", style="red")
            return None

        if response is None:
            return None

        if isinstance(response, str):
            if console and trigger == "manual":
                console.print(f"Compaction failed: {response}", style="red")
            return None

        try:
            summary_text = response["choices"][0]["message"].get("content", "").strip()
        except (KeyError, IndexError, TypeError):
            summary_text = ""

        if not summary_text:
            if console and trigger == "manual":
                console.print("Compaction failed: empty summary.", style="red")
            return None

        # Build new history: system prompt + summary + recent messages
        summary_message = {
            "role": "system",
            "content": f"Previous conversation context (summarized):\n\n{summary_text}"
        }

        self._cm.messages = SanitizedMessageList([self._cm.messages[0]] + [summary_message] + messages_to_keep)

        # Update token tracking accurately (include system prompt + messages + tools)
        self._cm._update_context_tokens(force=True)
        tokens_after = self._cm.token_tracker.current_context_tokens
        provider_cfg = get_provider_config(self._cm.client.provider)
        self._cm.token_tracker.add_usage(
            response,
            model_name=provider_cfg.get("model", ""),
        )

        # Update context estimate (keeps cumulative API usage intact)
        self._cm.context_token_estimate = tokens_after

        # Notify only for manual trigger
        if console and trigger == "manual":
            reduction = tokens_before - tokens_after
            console.print(
                f"[dim]Compacted history: {tokens_before:,} → {tokens_after:,} tokens "
                f"(-{reduction:,} / {-100 * reduction // (tokens_before or 1)}%)[/dim]"
            )

        return {
            "trigger": trigger,
            "before_tokens": tokens_before,
            "after_tokens": tokens_after,
            "summary": summary_text,
        }

    def maybe_auto_compact(self, console=None):
        """Check token count and auto-compact if over threshold.

        Args:
            console: None for silent operation (no user notification)
        """
        # Check against total context tokens (system prompt + messages + tools)
        self._cm._update_context_tokens(force=True)
        total_tokens = self._cm.token_tracker.current_context_tokens

        # Skip auto-compaction if locked (tools are actively being executed)
        if self._cm._compaction_locked:
            return

        # Skip all compaction if disabled (e.g. sub-agents preserving findings)
        if self._cm._compaction_disabled:
            return

        # Use custom threshold if set, otherwise use global setting.
        # None means automatic history compaction is off.
        trigger_threshold = (
            self._cm._compact_trigger_tokens
            if self._cm._compact_trigger_tokens is not None
            else context_settings.compact_trigger_tokens
        )

        if trigger_threshold is not None and total_tokens >= trigger_threshold:
            # Auto-compact with optional notification
            result = self.compact_history(console=None, trigger="auto")
            if result and context_settings.notify_auto_compaction and console:
                self._notify_compaction(
                    console,
                    result["before_tokens"],
                    result["after_tokens"],
                    "compacted history",
                )

    def ensure_context_fits(self, console=None):
        """Ensure context fits within hard_limit_tokens before sending to LLM.

        Three-layer escalation strategy:
        1. Check — if under hard_limit, return immediately (no action)
        2. Layer 1 — aggressive tool result compaction (non-LLM, fast)
        3. Layer 2 — AI-based history compaction (slower, more effective)
        4. Layer 3 — emergency truncation (drop oldest messages)

        If _compaction_locked, skip all layers (including truncation) and return
        "locked" — the message list is in intermediate state during tool execution.

        Args:
            console: Optional Rich console for debug notifications.

        Returns:
            dict with action taken and details, e.g.:
            {"action": "none", "tokens": 120000}
            {"action": "tool_compaction", "tokens": 90000, "reduction": 30000}
            {"action": "history_compaction", "tokens": 70000, "reduction": 50000}
            {"action": "emergency_truncation", "tokens": 150000, "dropped": 5}
        """
        self._cm._update_context_tokens(force=True)
        current_tokens = self._cm.token_tracker.current_context_tokens
        hard_limit = context_settings.hard_limit_tokens
        if hard_limit is None:
            return {"action": "none", "tokens": current_tokens}

        # Layer 0: Under limit — no action needed
        if current_tokens < hard_limit:
            return {"action": "none", "tokens": current_tokens}

        # Skip all compaction layers if disabled (e.g. sub-agents preserving findings)
        if self._cm._compaction_disabled:
            logger.warning(
                "Context (%d tokens) exceeds hard limit (%d) but compaction is disabled — "
                "API call may fail with context-length error",
                current_tokens, hard_limit,
            )
            return {"action": "none", "tokens": current_tokens}

        tokens_before = current_tokens

        # If compaction is NOT locked, try layers 1 and 2
        if not self._cm._compaction_locked:
            # Layer 1: Aggressive tool result compaction (non-LLM, fast)
            # Use very small token budget and min blocks for aggressive compaction
            self.compact_tool_results(
                skip_token_update=True,
                uncompacted_tail_tokens=10_000,
                min_tool_blocks=1,
            )

            self._cm._update_context_tokens(force=True)
            current_tokens = self._cm.token_tracker.current_context_tokens
            if current_tokens < hard_limit:
                result = {
                    "action": "tool_compaction",
                    "tokens": current_tokens,
                    "reduction": tokens_before - current_tokens,
                }
                self._notify_compaction(console, tokens_before, current_tokens, _ACTION_LABELS["tool_compaction"])
                return result

            # Layer 2: AI-based history compaction
            try:
                result = self.compact_history(console=None, trigger="auto")
            except Exception:
                result = None  # Compaction failed, fall through to truncation

            if result is not None:
                self._cm._update_context_tokens(force=True)
                current_tokens = self._cm.token_tracker.current_context_tokens
                if current_tokens < hard_limit:
                    result = {
                        "action": "history_compaction",
                        "tokens": current_tokens,
                        "reduction": tokens_before - current_tokens,
                    }
                    self._notify_compaction(console, tokens_before, current_tokens, _ACTION_LABELS["history_compaction"])
                    return result

        # Layer 3: Emergency truncation — drop oldest messages
        # Skip if compaction is locked (tool execution in progress) to avoid
        # corrupting tool_call_id pairing on incomplete message state
        if self._cm._compaction_locked:
            self._cm._update_context_tokens(force=True)
            current_tokens = self._cm.token_tracker.current_context_tokens
            return {
                "action": "locked",
                "tokens": current_tokens,
                "reduction": tokens_before - current_tokens,
            }

        self._emergency_truncate(hard_limit)
        self._cm._update_context_tokens(force=True)
        current_tokens = self._cm.token_tracker.current_context_tokens

        result = {
            "action": "emergency_truncation",
            "tokens": current_tokens,
            "reduction": tokens_before - current_tokens,
        }
        self._notify_compaction(console, tokens_before, current_tokens, _ACTION_LABELS["emergency_truncation"])
        return result

    def _emergency_truncate(self, target_tokens):
        """Drop oldest non-system messages until context is under target.

        Preservation rules:
        - Index 0: system prompt (always kept)
        - Any "Previous conversation context" system messages (compaction summaries)
        - Last 6 messages minimum (recent context)
        - Tool-call integrity: if an assistant message with tool_calls is in the
          protected tail, all its corresponding tool result messages must also be
          in the tail (and vice versa). The protected region is expanded to
          include complete tool blocks.

        Args:
            target_tokens: Target token count to get under.
        """
        MIN_TAIL = 6  # Minimum recent messages to preserve

        def _is_protected(msg):
            """Check if a message should never be dropped."""
            return msg.get("role", "") == "system"

        def _compute_protected_tail(messages):
            """Compute the minimum protected tail index that preserves tool_call pairs.

            Start from MIN_TAIL from the end and expand backward if a tool block
            straddles the boundary.
            """
            n = len(messages)
            if n <= MIN_TAIL + 1:
                return 1  # Nothing to drop anyway

            tail_start = n - MIN_TAIL

            # Scan backward from tail_start to find tool blocks that straddle
            # the boundary and expand to include them.
            changed = True
            while changed:
                changed = False
                # Build set of tool_call_ids that appear in tool messages within
                # the protected tail region
                tool_ids_in_tail = set()
                for i in range(tail_start, n):
                    msg = messages[i]
                    if msg.get("role") == "tool":
                        tcid = msg.get("tool_call_id")
                        if tcid:
                            tool_ids_in_tail.add(tcid)

                # Check if any message just before tail_start has tool_calls
                # that reference those tool_call_ids
                scan = tail_start - 1
                while scan > 0:
                    msg = messages[scan]
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        msg_tool_ids = {
                            tc.get("id") for tc in msg["tool_calls"] if tc.get("id")
                        }
                        if msg_tool_ids & tool_ids_in_tail:
                            # This assistant message must be in the protected tail
                            tail_start = scan
                            changed = True
                            # Also add any of its tool_call_ids to the set
                            tool_ids_in_tail |= msg_tool_ids
                        else:
                            break  # No overlap, stop scanning backward
                    elif msg.get("role") == "tool":
                        # A tool message before the assistant — check if its
                        # tool_call_id belongs to an assistant in the tail
                        tcid = msg.get("tool_call_id")
                        if tcid and tcid in tool_ids_in_tail:
                            tail_start = scan
                            changed = True
                        else:
                            break
                    else:
                        break
                    scan -= 1

            return tail_start

        # Drop oldest non-protected messages until under target
        while True:
            self._cm._update_context_tokens(force=True)
            if self._cm.token_tracker.current_context_tokens < target_tokens:
                break

            tail_start = _compute_protected_tail(self._cm.messages)
            if tail_start <= 1:
                break  # Nothing droppable remains

            # Find the oldest droppable message (skip index 0 and protected tail)
            dropped = False
            for i in range(1, tail_start):
                if not _is_protected(self._cm.messages[i]):
                    self._cm.messages.pop(i)
                    dropped = True
                    break

            if not dropped:
                break  # Only protected messages remain in droppable zone

        self._cm.sync_log()

    def _notify_compaction(self, console, tokens_before, tokens_after, action_label):
        """Show dim notification when auto-compaction takes action.

        Args:
            console: Rich console (or None to suppress)
            tokens_before: Token count before compaction
            tokens_after: Token count after compaction
            action_label: Human-readable description of the action taken
        """
        if not context_settings.notify_auto_compaction or not console:
            return
        reduction = tokens_before - tokens_after
        console.print(
            f"[dim]Auto-compacted: {tokens_before:,} → {tokens_after:,} tokens "
            f"({action_label})[/dim]"
        )
