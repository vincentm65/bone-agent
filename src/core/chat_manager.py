"""Chat state and server lifecycle management."""

import os
import json
import subprocess
import time
import requests
from typing import Optional, IO

from llm.client import LLMClient
from llm.config import get_providers, get_provider_config, reload_config
from llm.prompts import build_system_prompt
from pathlib import Path
from llm.token_tracker import TokenTracker
from utils.settings import server_settings, context_settings
from utils.logger import MarkdownConversationLogger
from utils.result_parsers import extract_exit_code, extract_metadata_from_result

# Token counting constants
MESSAGE_OVERHEAD_TOKENS = 4  # Approximate tokens for JSON structure: braces, quotes, colons, commas
CHAR_BASED_OVERHEAD = 20    # Character overhead for JSON structure in character-based estimation

# Action labels for context management notifications (used by ensure_context_fits)
_ACTION_LABELS = {
    "tool_compaction": "compacted tool results",
    "history_compaction": "compacted history",
    "emergency_truncation": "emergency truncation (oldest messages dropped)",
}

class ChatManager:
    """Manages chat state, messages, and provider switching."""

    def __init__(self, compact_trigger_tokens: Optional[int] = None):
        # Initialize client with provider from global config
        self.client = LLMClient()
        self.messages = []
        self.server_process: Optional[subprocess.Popen] = None
        self._log_file: Optional[IO] = None  # Track llama_server log file handle
        self.approve_mode = "safe"
        self.interaction_mode = "edit"  # Default to edit mode
        self.plan_type = "feature"  # Default plan type (for plan interaction mode)
        self.token_tracker = TokenTracker()
        self.context_token_estimate = 0
        # In-session, memory-only task list (used in EDIT workflows)
        self.task_list = []
        self.task_list_title = None

        # .gitignore filtering state
        self._gitignore_spec = None
        self._gitignore_mtime = None
        self._repo_root = None

        # Custom compaction threshold (overrides global context_settings if set)
        self._compact_trigger_tokens = compact_trigger_tokens

        # Conversation logging
        self.markdown_logger: Optional[MarkdownConversationLogger] = None
        if context_settings.log_conversations:
            self.markdown_logger = MarkdownConversationLogger(
                conversations_dir=context_settings.conversations_dir
            )

        # Compaction lock: prevents compaction during active tool execution
        # Set by agentic.py before executing tools, cleared after all results appended
        self._compaction_locked = False

        self._init_messages(reset_totals=True)

    def set_compaction_lock(self, locked):
        """Set or release the compaction lock.

        When locked, compaction is skipped entirely (no message removal,
        no summarization, no truncation). Used during tool execution to
        prevent orphaning tool_call_ids.
        """
        self._compaction_locked = locked

    def _init_messages(self, reset_totals: bool = True):
        """Initialize message history with system prompt and agents.md as initial exchange."""
        # Start new conversation logging session
        if self.markdown_logger:
            self.markdown_logger.start_session()

        # Start with system prompt only (uses current self.interaction_mode)
        self.messages = [{"role": "system", "content": self._build_system_prompt()}]

        # Add agents.md as initial user/assistant exchange (only if it exists in cwd)
        user_msg, assistant_msg = self._load_agents_md()
        if user_msg and assistant_msg:
            self.messages.append({"role": "user", "content": user_msg})
            self.messages.append({"role": "assistant", "content": assistant_msg})

        # Log initial messages
        if self.markdown_logger:
            for msg in self.messages:
                self.markdown_logger.log_message(msg)

        # Reset session totals if requested (keep totals across /clear)
        # For a fresh conversation, cumulative totals start at 0 (no API calls made yet)
        if reset_totals:
            self.token_tracker.reset(prompt_tokens=0, completion_tokens=0)

        # Always reset conversation tokens (resets on /new and fresh starts)
        self.token_tracker.reset_conversation()

        # Initialize context tokens with actual message count (including tools if enabled)
        self._update_context_tokens()
        self.context_token_estimate = self.token_tracker.current_context_tokens

        # NOTE: interaction_mode is NOT reset - it persists across /clear

    def _build_system_prompt(self) -> str:
        """Build system prompt with mode-specific rules."""
        if self.interaction_mode == "plan":
            return build_system_prompt(self.interaction_mode, plan_type=self.plan_type)
        else:
            return build_system_prompt(self.interaction_mode)

    def update_system_prompt(self):
        """Rebuild system prompt after mode change."""
        if not self.messages:
            raise RuntimeError("Cannot update system prompt: messages array is empty")

        if self.messages[0]["role"] != "system":
            raise RuntimeError(f"Cannot update system prompt: messages[0] has role '{self.messages[0]['role']}', expected 'system'")

        # Update the system message with current mode
        self.messages[0]["content"] = self._build_system_prompt()
        self._update_context_tokens()

    def _load_agents_md(self) -> tuple[str, str]:
        """Load agents.md content and prepare user/assistant exchange.

        Returns:
            tuple: (user_message, assistant_message)
        """
        # Check for agents.md in current working directory (user's project)
        agents_path = Path.cwd() / "agents.md"

        if agents_path.exists():
            map_content = agents_path.read_text(encoding="utf-8").strip()
            user_msg = (
                "Here is the codebase map for this project. "
                "This provides an overview of the repository structure and file purposes. "
                "Use this as a reference when exploring the codebase.\n\n"
                f"## Codebase Map (auto-generated from agents.md)\n\n{map_content}"
            )
            assistant_msg = (
                "I've received the codebase map. I'll use this as a reference when "
                "exploring the repository, but I'll always verify current state by "
                "reading files and searching the codebase before making changes."
            )
        else:
            # No codebase map available - skip entirely
            user_msg = ""
            assistant_msg = ""

        return user_msg, assistant_msg

    def _update_context_tokens(self, tools=None):
        """Recount and update current_context_tokens after message changes.

        Args:
            tools: Optional list of tool definitions to include in token count.
                   If None, uses current mode's tools (if enabled).
        """
        message_tokens = self._count_tokens(self.messages)

        # Count tool tokens if tools are provided or enabled
        if tools is None:
            from llm.config import TOOLS_ENABLED
            if not TOOLS_ENABLED:
                self.token_tracker.set_context_tokens(message_tokens)
                self.context_token_estimate = message_tokens
                return
            else:
                from tools import _tools_for_mode
                tools = _tools_for_mode(self.interaction_mode)

        if tools:
            # Use character-based approximation for Anthropic (tiktoken doesn't support Claude)
            if self.client.provider == "anthropic":
                tools_json = json.dumps(tools)
                tool_tokens = len(tools_json) // 4
            else:
                try:
                    import tiktoken
                    model = getattr(self.client, "model", "") or ""
                    try:
                        enc = tiktoken.encoding_for_model(model)
                    except Exception:
                        enc = tiktoken.get_encoding("cl100k_base")

                    # Encode tools list as JSON (which is how it's sent to the API)
                    tools_json = json.dumps(tools)
                    tool_tokens = len(enc.encode(tools_json))
                except Exception:
                    # Fallback: character-based approximation
                    tools_json = json.dumps(tools)
                    tool_tokens = len(tools_json) // 4

            total_tokens = message_tokens + tool_tokens
        else:
            total_tokens = message_tokens

        self.token_tracker.set_context_tokens(total_tokens)
        self.context_token_estimate = total_tokens

    def _collect_message_text(self, msg) -> str:
        """Extract all text fields from a message as a single string.

        Collects role, content, tool_calls (id, type, function name/args),
        and tool_call_id fields. Used by token counting methods.

        Args:
            msg: Message dict

        Returns:
            Concatenated string of all message text fields
        """
        parts = []

        # Role field
        role = msg.get('role', '')
        if role:
            parts.append(role)

        # Content
        content = msg.get('content', '')
        if content:
            parts.append(str(content))

        # Tool calls (assistant messages)
        if msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                # id field (e.g., "call_abc123")
                tc_id = tc.get('id', '')
                if tc_id:
                    parts.append(tc_id)

                # type field (usually "function")
                tc_type = tc.get('type', 'function')
                parts.append(tc_type)

                # function object
                fn = tc.get('function', {})
                if fn:
                    fn_name = fn.get('name', '')
                    if fn_name:
                        parts.append(fn_name)

                    fn_args = fn.get('arguments', '{}')
                    parts.append(fn_args)

        # Tool call ID (tool messages)
        if msg.get('role') == 'tool' and msg.get('tool_call_id'):
            parts.append(msg['tool_call_id'])

        return ''.join(p or '' for p in parts)

    def _count_tokens(self, messages) -> int:
        """Count tokens accurately using tiktoken for OpenAI, character-based for Anthropic.

        Counts everything the AI receives:
        - All message types: user, assistant, system, tool
        - All fields: role, content, tool_calls (id, type, function, name, arguments)
        - Tool messages: tool_call_id + content

        Args:
            messages: List of messages to count tokens for

        Returns:
            int: Estimated token count
        """
        # Use character-based approximation for Anthropic (tiktoken doesn't support Claude)
        if self.client.provider == "anthropic":
            return self._count_tokens_char_based(messages)

        try:
            import tiktoken
            model = getattr(self.client, "model", "") or ""
            try:
                enc = tiktoken.encoding_for_model(model)
            except Exception:
                enc = tiktoken.get_encoding("cl100k_base")

            # Collect text from all messages and encode
            total = 0
            for msg in messages:
                text = self._collect_message_text(msg)
                total += len(enc.encode(text))
                total += MESSAGE_OVERHEAD_TOKENS

            return total

        except Exception:
            # Fallback to character-based estimation
            return self._count_tokens_char_based(messages)

    def _count_tokens_char_based(self, messages) -> int:
        """Count tokens using character-based approximation (for Anthropic).

        Uses ~4 characters per token as a rough estimate.

        Args:
            messages: List of messages to count tokens for

        Returns:
            int: Estimated token count
        """
        total = 0
        for msg in messages:
            text = self._collect_message_text(msg)
            total += (len(text) + CHAR_BASED_OVERHEAD) // 4

        return total


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
                content = m.get('content', '')
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
                content = m.get('content', '')
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

    # ===== Tool Result Compaction =====

    def _find_tool_blocks(self):
        """Find all tool-result blocks in message history.

        Handles both single-turn and multi-turn tool chains:
          Single: user → assistant(tc) → tool_results → assistant(answer)
          Multi:  user → assistant(tc1) → tools → assistant(tc2) → tools → assistant(answer)

        In multi-turn chains, all tool_calls and tool_results are merged into
        a single block spanning from the first assistant(tool_calls) to the
        final assistant(answer).

        Returns:
            list: List of block dicts with keys: user_idx, start, end, tool_calls, tool_results
        """
        blocks = []
        i = 0

        while i < len(self.messages):
            msg = self.messages[i]

            # Look for assistant message with tool_calls
            if msg.get('role') == 'assistant' and msg.get('tool_calls'):

                # Find user question before this
                user_idx = i - 1
                while user_idx >= 0 and self.messages[user_idx].get('role') != 'user':
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

                while j < len(self.messages):
                    if self.messages[j].get('role') == 'assistant' and self.messages[j].get('tool_calls'):
                        # Accumulate tool calls from this assistant message
                        all_tool_calls.extend(self.messages[j].get('tool_calls', []))
                        # Collect immediately following tool results
                        k = j + 1
                        while k < len(self.messages) and self.messages[k].get('role') == 'tool':
                            all_tool_results.append(self.messages[k].get('content', ''))
                            k += 1
                        j = k
                    elif self.messages[j].get('role') == 'assistant' and not self.messages[j].get('tool_calls'):
                        # Final answer — this completes the block
                        found_end = True
                        break
                    else:
                        # Non-tool, non-assistant message breaks the chain
                        break

                if found_end and all_tool_calls:
                    blocks.append({
                        'user_idx': user_idx,
                        'start': block_start,
                        'end': j,
                        'tool_calls': all_tool_calls,
                        'tool_results': all_tool_results
                    })

                # Continue scanning from after the final answer (or after the chain)
                # Guard: always advance at least one position to prevent infinite loops
                i = max(i + 1, j + 1 if found_end else j)
            else:
                i += 1

        return blocks

    def _get_tool_result_messages(self, start_idx, end_idx):
        """Extract only tool result messages between two indices.

        Args:
            start_idx: Starting index (exclusive)
            end_idx: Ending index (exclusive)

        Returns:
            list: Tool result messages (role='tool') between start_idx and end_idx
        """
        tool_results = []
        for i in range(start_idx + 1, end_idx):
            if self.messages[i].get('role') == 'tool':
                tool_results.append(self.messages[i])
        return tool_results

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

    def compact_tool_results(self):
        """Replace completed tool-result blocks with summaries.

        Runs after each completed tool sequence to keep context lean
        without using AI for summarization.

        This is called after the LLM produces a final answer with no more tool calls.
        """
        # Skip compaction if locked (tools are actively being executed)
        if self._compaction_locked:
            return

        if not context_settings.tool_compaction.enable_per_message_compaction:
            return

        # Safety: Don't compact if very few messages
        if len(self.messages) < 6:  # Minimum: user+assistant+tool+assistant+user+assistant
            return

        # Find tool-result blocks
        blocks = self._find_tool_blocks()

        if not blocks:
            return

        # Keep recent N blocks intact
        keep_verbatim = blocks[-context_settings.tool_compaction.keep_recent_tool_blocks:]
        blocks_to_compact = blocks[:-context_settings.tool_compaction.keep_recent_tool_blocks]

        if not blocks_to_compact:
            return

        # Track token counts before
        tokens_before = self._count_tokens(self.messages)

        # Replace old blocks with summaries
        new_messages = []
        processed_indices = set()

        for i, msg in enumerate(self.messages):
            if i in processed_indices:
                continue  # Skip messages that were compacted

            # Check if this is start of a block to compact
            block_start = next((b for b in blocks_to_compact if b['start'] == i), None)

            if block_start:
                # Check if any tool in this block failed
                skip_compaction = False
                if not context_settings.tool_compaction.compact_failed_tools:
                    for tool_result in block_start['tool_results']:
                        exit_code = extract_exit_code(tool_result)
                        if exit_code is not None and exit_code != 0:
                            skip_compaction = True
                            break

                if skip_compaction:
                    # Keep this block as-is
                    for idx in range(block_start['user_idx'], block_start['end'] + 1):
                        new_messages.append(self.messages[idx])
                        processed_indices.add(idx)
                    continue

                # Generate summary
                summary = self._generate_tool_block_summary(
                    block_start['tool_calls'],
                    block_start['tool_results']
                )

                # Add user question with summary appended
                user_msg = self.messages[block_start['user_idx']].copy()
                user_msg['content'] = user_msg['content'] + f"\n\n[Context: {summary}]"
                new_messages.append(user_msg)

                # Add final assistant answer
                new_messages.append(self.messages[block_start['end']])

                # Mark all indices as processed (including user question)
                processed_indices.add(block_start['user_idx'])
                for idx in range(block_start['start'], block_start['end'] + 1):
                    processed_indices.add(idx)
            else:
                # Keep this message as-is
                new_messages.append(msg)

        self.messages = new_messages
        self._update_context_tokens()

    # ===== AI-Based History Compaction =====

    def compact_history(self, console=None, trigger="manual", aggressive=False):
        """Compact chat history while preserving recent context.

        Strategy:
        1. Keep last user message verbatim
        2. Keep assistant tool_calls message (if present) for context
        3. Keep last assistant response (without tool calls) verbatim
        4. Summarize everything prior AND all tool result messages

        Aggressive mode:
        - Pre-compacts recent tool results first (for older blocks)
        - Also summarizes tool interactions between last user and final answer

        Args:
            console: Console for notifications (None for silent auto-compact)
            trigger: "manual" or "auto"
            aggressive: If True, also compact recent tool results aggressively

        Returns:
            dict with compaction stats or None
        """
        if len(self.messages) < 10:  # Need enough history
            return None

        # In aggressive mode, pre-compact tool results first (for older blocks)
        if aggressive:
            # Temporarily reduce keep_recent_tool_blocks to 1 for aggressive compaction
            original_keep = context_settings.tool_compaction.keep_recent_tool_blocks
            try:
                context_settings.tool_compaction.keep_recent_tool_blocks = 1
                self.compact_tool_results()
            finally:
                context_settings.tool_compaction.keep_recent_tool_blocks = original_keep

        # Find the last user message (start from end, skip system/tool messages)
        last_user_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            role = self.messages[i].get('role')
            # Look for user message that's not the codebase map
            if role == 'user' and not self.messages[i].get('tool_calls'):
                content = self.messages[i].get('content', '')
                if content and not content.startswith("The codebase map"):
                    last_user_idx = i
                    break

        if last_user_idx is None or last_user_idx < 3:
            return None  # Not enough history to compact

        # Find the last assistant message WITHOUT tool calls (final answer)
        last_assistant_without_tools_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
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
            messages_to_keep = self.messages[last_user_idx:]
            messages_to_summarize = self.messages[1:last_user_idx]
        else:
            # Case 2: There are tool interactions between last user and last assistant
            #         Keep: last user message + entire tool exchange + final answer
            #         Summarize: everything before last user message
            #
            # The tail from last_user_idx through last_assistant_without_tools_idx
            # is a valid message sequence (user → assistant(tool_calls) → tool results → assistant(answer))
            # and must be kept intact to avoid consecutive assistant messages or orphaned tool_call_ids.
            messages_to_keep = self.messages[last_user_idx:]
            messages_to_summarize = self.messages[1:last_user_idx]

        if not messages_to_summarize:
            return None

        # Generate comprehensive summary using extracted context
        summary_prompt_content = self._build_summary_prompt(messages_to_summarize)

        # Track token counts before (total tokens including system prompt + messages + tools)
        self._update_context_tokens()
        tokens_before = self.token_tracker.current_context_tokens

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
            response = self.client.chat_completion(summary_prompt, stream=False, tools=None)
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

        self.messages = [self.messages[0]] + [summary_message] + messages_to_keep

        # Update token tracking accurately (include system prompt + messages + tools)
        self._update_context_tokens()
        tokens_after = self.token_tracker.current_context_tokens
        summary_usage = response.get("usage", {})

        # Add summary generation tokens to cumulative usage
        self.token_tracker.add_usage(summary_usage)

        # Update context estimate (keeps cumulative API usage intact)
        self.context_token_estimate = tokens_after

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
        self._update_context_tokens()
        total_tokens = self.token_tracker.current_context_tokens

        # Skip auto-compaction if locked (tools are actively being executed)
        if self._compaction_locked:
            return

        # Use custom threshold if set, otherwise use global setting
        trigger_threshold = (
            self._compact_trigger_tokens
            if self._compact_trigger_tokens is not None
            else context_settings.compact_trigger_tokens
        )

        if total_tokens >= trigger_threshold:
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
        self._update_context_tokens()
        current_tokens = self.token_tracker.current_context_tokens
        hard_limit = context_settings.hard_limit_tokens

        # Layer 0: Under limit — no action needed
        if current_tokens < hard_limit:
            return {"action": "none", "tokens": current_tokens}

        tokens_before = current_tokens

        # If compaction is NOT locked, try layers 1 and 2
        if not self._compaction_locked:
            # Layer 1: Aggressive tool result compaction (non-LLM, fast)
            # Temporarily reduce keep_recent_tool_blocks to 1
            original_keep = context_settings.tool_compaction.keep_recent_tool_blocks
            try:
                context_settings.tool_compaction.keep_recent_tool_blocks = 1
                self.compact_tool_results()
            finally:
                context_settings.tool_compaction.keep_recent_tool_blocks = original_keep

            self._update_context_tokens()
            current_tokens = self.token_tracker.current_context_tokens
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
                result = self.compact_history(console=None, trigger="auto", aggressive=True)
            except Exception:
                result = None  # Compaction failed, fall through to truncation

            if result is not None:
                self._update_context_tokens()
                current_tokens = self.token_tracker.current_context_tokens
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
        if self._compaction_locked:
            self._update_context_tokens()
            current_tokens = self.token_tracker.current_context_tokens
            return {
                "action": "locked",
                "tokens": current_tokens,
                "reduction": tokens_before - current_tokens,
            }

        self._emergency_truncate(hard_limit)
        self._update_context_tokens()
        current_tokens = self.token_tracker.current_context_tokens

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
            self._update_context_tokens()
            if self.token_tracker.current_context_tokens < target_tokens:
                break

            tail_start = _compute_protected_tail(self.messages)
            if tail_start <= 1:
                break  # Nothing droppable remains

            # Find the oldest droppable message (skip index 0 and protected tail)
            dropped = False
            for i in range(1, tail_start):
                if not _is_protected(self.messages[i]):
                    self.messages.pop(i)
                    dropped = True
                    break

            if not dropped:
                break  # Only protected messages remain in droppable zone

        self.sync_log()

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

    def get_gitignore_spec(self, repo_root: Path):
        """Get cached or load PathSpec object for .gitignore filtering.

        Caches the spec and reloads if .gitignore is modified.

        Args:
            repo_root: Repository root directory

        Returns:
            pathspec.PathSpec or None if .gitignore doesn't exist
        """
        gitignore_path = repo_root / ".gitignore"

        # Check if we need to reload
        current_mtime = None
        if gitignore_path.exists():
            current_mtime = gitignore_path.stat().st_mtime

        # Reload if: (1) not initialized, (2) repo changed, (3) file modified
        if (
            self._gitignore_spec is None
            or self._repo_root != repo_root
            or current_mtime != self._gitignore_mtime
        ):
            from utils.gitignore_filter import load_gitignore_spec

            self._repo_root = repo_root
            self._gitignore_mtime = current_mtime
            self._gitignore_spec = load_gitignore_spec(repo_root)

        return self._gitignore_spec

    def switch_provider(self, provider_name):
        """Switch LLM provider.

        Args:
            provider_name: Provider name ('local' or 'openrouter')

        Returns:
            str: Result message
        """
        providers = get_providers()
        if provider_name not in providers:
            return f"Invalid provider. Use /provider to list. Available: {', '.join(providers)}"

        previous_provider = self.client.provider

        # Terminate server if switching away from local
        if previous_provider == "local" and provider_name != "local":
            self.cleanup()

        if self.client.switch_provider(provider_name):
            self._init_messages()
            if provider_name == "local":
                server = self.start_server_if_needed()
                if not server:
                    # Failed to start server - revert
                    self.client.switch_provider(previous_provider)
                    self._init_messages()
                    return f"Failed to start local server. Reverted to {previous_provider} provider."
                self.server_process = server
                return f"Switched to {provider_name} provider (server ready)."
            return f"Switched to {provider_name} provider."
        return "Provider switch failed."

    def reload_config(self):
        """Reload configuration from disk and update client.

        This should be called after any config change (provider, model, api key).
        """
        reload_config()
        self.client.sync_provider_from_config()

    # ===== Config Methods (for agent use) =====

    def set_provider(self, provider_name: str) -> str:
        """Set provider for current session (agent-accessible).

        Args:
            provider_name: Provider name to switch to.

        Returns:
            str: Result message.
        """
        return self.switch_provider(provider_name)

    def start_server_if_needed(self):
        """Start local server if using local provider and not already running.

        Returns:
            subprocess.Popen: Server process or None
        """
        if self.client.provider == "local" and not self.server_process:
            return self._start_local_server()
        return None

    def _start_local_server(self):
        """Start llama-server process and wait for health check.

        Returns:
            subprocess.Popen: Server process or None if failed
        """
        from llm.config import get_provider_config, _CONFIG

        local_config = get_provider_config("local")
        server_path = _CONFIG.get("LOCAL_SERVER_PATH", local_config["config_keys"]["LOCAL_SERVER_PATH"])
        model_path = local_config.get("model", "")
        host = local_config["extra"]["host"]
        port = local_config["extra"]["port"]

        args = [
            server_path,
            "-m", model_path,
            "-ngl", str(server_settings.ngl_layers),
            "--split-mode", "none",
            "--ctx-size", str(server_settings.ctx_size),
            "--n-predict", str(server_settings.n_predict),
            "--rope-scale", str(server_settings.rope_scale),
            "--host", host,
            "--port", str(port),
            "--jinja",
        ]

        # Restrict to RTX 5070 Ti only (GPU 0)
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = "0"

        # Log stderr to file for debugging
        log_path = Path(__file__).resolve().parents[2] / "llama_server.log"
        self._log_file = open(log_path, "w")

        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
            env=env,
        )

        health_url = f"http://{host}:{port}/health"
        for i in range(server_settings.health_check_timeout_sec):
            try:
                r = requests.get(health_url, timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") == "ok":
                        return process
            except Exception:
                pass
            time.sleep(server_settings.health_check_interval_sec)

        # Server failed health check - clean up resources
        if process:
            process.terminate()
            process.wait()
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        return None

    def cycle_approve_mode(self) -> str:
        """Cycle to next approval mode (for Edit mode) or plan type (for Plan mode).

        Returns:
            str: The new approval mode or plan type.
        """
        # In Plan mode, cycle plan types instead of approval modes
        if self.interaction_mode == "plan":
            return self.cycle_plan_type()

        # In Edit/Learn mode, cycle approval modes
        from llm.config import CYCLEABLE_APPROVE_MODES
        modes = CYCLEABLE_APPROVE_MODES
        try:
            next_index = (modes.index(self.approve_mode) + 1) % len(modes)
        except ValueError:
            next_index = 0
        self.approve_mode = modes[next_index]
        return self.approve_mode

    def cycle_plan_type(self) -> str:
        """Cycle to next plan type (for Plan interaction mode).

        Returns:
            str: The new plan type.
        """
        from llm.config import PLAN_TYPES
        modes = PLAN_TYPES
        try:
            next_index = (modes.index(self.plan_type) + 1) % len(modes)
        except ValueError:
            next_index = 0
        self.plan_type = modes[next_index]
        # Update system prompt to reflect new plan type
        if self.interaction_mode == "plan":
            self.update_system_prompt()
            # Sync conversation log to reflect plan type changes
            self.sync_log()
        return self.plan_type

    def toggle_interaction_mode(self) -> str:
        """Toggle between plan/edit modes.

        Returns:
            str: The new interaction mode.
        """
        modes = ("edit", "plan")
        current_index = modes.index(self.interaction_mode)
        self.interaction_mode = modes[(current_index + 1) % len(modes)]
        self.update_system_prompt()
        # Sync conversation log to reflect mode changes
        self.sync_log()
        return self.interaction_mode

    def set_interaction_mode(self, mode: str) -> str:
        """Set interaction mode to a specific value.

        Args:
            mode: The desired mode ('edit' or 'plan').

        Returns:
            str: The new interaction mode.
        """
        modes = ("edit", "plan")
        if mode not in modes:
            raise ValueError(f"Invalid mode '{mode}'. Must be one of {modes}")
        if mode == self.interaction_mode:
            return self.interaction_mode
        self.interaction_mode = mode
        self.update_system_prompt()
        self.sync_log()
        return self.interaction_mode

    def reset_session(self):
        """Reset chat session (clear messages and task list).

        This is a public wrapper for _init_messages that also clears
        the in-session task list.
        """
        # End current conversation logging session before reset
        if self.markdown_logger:
            self.markdown_logger.end_session()

        self._init_messages(reset_totals=False)
        self.task_list.clear()
        self.task_list_title = None

    def log_message(self, message: dict):
        """Log a message to the conversation logger.

        Args:
            message: Message dict to log
        """
        if self.markdown_logger:
            self.markdown_logger.log_message(message)

    def sync_log(self):
        """Rewrite the entire conversation log to match current message state.

        This should be called after any operation that modifies the messages array:
        - After adding new messages
        - After compaction
        - After mode changes (which modify system prompts)
        """
        if self.markdown_logger:
            self.markdown_logger.rewrite_log(self.messages)

    def end_conversation(self):
        """End the current conversation logging session."""
        if self.markdown_logger:
            self.markdown_logger.end_session()

    def toggle_logging(self):
        """Toggle conversation logging on/off.

        Returns:
            bool: New logging state (True if enabled, False if disabled)
        """
        from utils.logger import MarkdownConversationLogger

        if self.markdown_logger:
            # Disable logging
            self.markdown_logger.end_session()
            self.markdown_logger = None
            return False
        else:
            # Enable logging
            self.markdown_logger = MarkdownConversationLogger(
                conversations_dir=context_settings.conversations_dir
            )
            # Start a new session and log current messages
            self.markdown_logger.start_session()
            for msg in self.messages:
                self.markdown_logger.log_message(msg)
            return True

    def set_logging(self, enabled: bool) -> bool:
        """Set conversation logging to a specific state.

        Args:
            enabled: True to enable logging, False to disable.

        Returns:
            bool: The new logging state.
        """
        current_state = self.markdown_logger is not None
        if enabled == current_state:
            return current_state
        return self.toggle_logging()

    def cleanup(self):
        """Terminate server process if running."""
        # End conversation session on cleanup
        if self.markdown_logger:
            self.markdown_logger.end_session()

        if self.server_process:
            self.server_process.terminate()
            self.server_process.wait()

        # Close log file handle if open
        if self._log_file:
            self._log_file.close()
            self._log_file = None
