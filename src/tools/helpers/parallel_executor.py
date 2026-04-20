"""Concurrent tool execution engine.

This module provides parallel execution of multiple tool calls using
ThreadPoolExecutor for I/O-bound operations like file reads and web searches.
"""

import concurrent.futures
from typing import List, Tuple
from dataclasses import dataclass

from .base import ToolRegistry, build_context


@dataclass
class ToolCall:
    """Represents a single tool call.

    Attributes:
        tool_id: Unique identifier for this tool call
        function_name: Name of the tool function to execute
        arguments: Dictionary of arguments to pass to the tool handler
        call_index: Index in original tool_calls array (for order preservation)
    """
    tool_id: str
    function_name: str
    arguments: dict
    call_index: int


@dataclass
class ToolResult:
    """Result of a tool execution.

    Attributes:
        tool_id: Unique identifier for the tool call
        call_index: Index in original tool_calls array (for order preservation)
        success: Whether the tool executed successfully
        result: String result from tool execution (if successful)
        error: Error message (if failed)
        should_exit: Whether the tool requested the orchestration loop to exit
        requires_approval: Whether this tool requires user approval (for orchestrator)
    """
    tool_id: str
    call_index: int
    success: bool
    result: str
    error: str = None
    should_exit: bool = False
    requires_approval: bool = False


class ParallelToolExecutor:
    """Executes multiple tool calls concurrently with proper error handling.

    This class provides thread-safe concurrent execution of tool calls using
    ThreadPoolExecutor. Key features:
    - Executes independent tools concurrently for performance
    - Preserves result order using call_index tracking
    - Isolates errors (one failure doesn't stop others)
    - Fast-path optimization for single tool calls (no threading overhead)
    """

    def __init__(self, max_workers: int = 5):
        """Initialize executor.

        Args:
            max_workers: Maximum number of concurrent tool executions
        """
        self.max_workers = max_workers

    def execute_tools(
        self,
        tool_calls: List[ToolCall],
        context: dict
    ) -> Tuple[List[ToolResult], bool]:
        """Execute multiple tools concurrently.

        Args:
            tool_calls: List of ToolCall objects
            context: Dictionary containing repo_root, console, chat_manager, etc.

        Returns:
            Tuple of (results in call_index order, had_any_errors)
        """
        if len(tool_calls) == 1:
            # Fast path for single tool (no threading overhead)
            return self._execute_single(tool_calls[0], context)

        # Parallel execution for multiple tools
        results = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(tool_calls))
        ) as executor:
            # Submit all tool executions
            future_to_call = {
                executor.submit(
                    self._execute_single_tool,
                    tool_call,
                    context
                ): tool_call
                for tool_call in tool_calls
            }

            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_call):
                tool_call = future_to_call[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    results.append(ToolResult(
                        tool_id=tool_call.tool_id,
                        call_index=tool_call.call_index,
                        success=False,
                        result="",
                        error=str(e)
                    ))

        # Sort by call_index to maintain order
        results.sort(key=lambda r: r.call_index)

        # Check for errors
        had_errors = any(not r.success for r in results)

        return results, had_errors

    def _execute_single(
        self,
        tool_call: ToolCall,
        context: dict
    ) -> Tuple[List[ToolResult], bool]:
        """Execute single tool (fast path, no threading overhead).

        Args:
            tool_call: Single ToolCall to execute
            context: Execution context dict

        Returns:
            Tuple of (single-element result list, had_errors)
        """
        result = self._execute_single_tool(tool_call, context)
        return [result], not result.success

    def _execute_single_tool(
        self,
        tool_call: ToolCall,
        context: dict
    ) -> ToolResult:
        """Execute a single tool call with error handling.

        Args:
            tool_call: ToolCall to execute
            context: Execution context dict

        Returns:
            ToolResult with execution outcome
        """
        tool = ToolRegistry.get(tool_call.function_name)
        if tool:
            try:
                # For tools requiring approval, return preview without executing
                # The orchestrator will handle the approval workflow
                if tool.requires_approval:
                    # Build context from context dict
                    cm = context.get('chat_manager')

                    tool_context = build_context(
                        repo_root=context.get('repo_root'),
                        console=context.get('console'),
                        gitignore_spec=context.get('gitignore_spec'),
                        debug_mode=context.get('debug_mode', False),
                        chat_manager=cm,
                        rg_exe_path=context.get('rg_exe_path'),
                        panel_updater=context.get('panel_updater'),
                        vault_root=context.get('vault_root')
                    )

                    # For edit_file: return preview (orchestrator handles approval)
                    if tool_call.function_name == "edit_file":
                        tool_result = tool.execute(tool_call.arguments, tool_context)
                        return ToolResult(
                            tool_id=tool_call.tool_id,
                            call_index=tool_call.call_index,
                            success=True,
                            result=tool_result,
                            should_exit=False,
                            requires_approval=True  # Flag for orchestrator to handle
                        )
                    # Other approval-required tools would go here in the future

                # Normal execution for tools without approval
                # Build context from context dict
                cm = context.get('chat_manager')

                tool_context = build_context(
                    repo_root=context.get('repo_root'),
                    console=context.get('console'),
                    gitignore_spec=context.get('gitignore_spec'),
                    debug_mode=context.get('debug_mode', False),
                    chat_manager=cm,
                    rg_exe_path=context.get('rg_exe_path'),
                    panel_updater=context.get('panel_updater'),
                    vault_root=context.get('vault_root')
                )

                tool_result = tool.execute(tool_call.arguments, tool_context)
                return ToolResult(
                    tool_id=tool_call.tool_id,
                    call_index=tool_call.call_index,
                    success=True,
                    result=tool_result,
                    should_exit=False
                )
            except Exception as e:
                return ToolResult(
                    tool_id=tool_call.tool_id,
                    call_index=tool_call.call_index,
                    success=False,
                    result="",
                    error=f"Error executing tool '{tool_call.function_name}': {str(e)}"
                )

        # Tool not found in registry
        return ToolResult(
            tool_id=tool_call.tool_id,
            call_index=tool_call.call_index,
            success=False,
            result="",
            error=f"Unknown tool '{tool_call.function_name}'"
        )
