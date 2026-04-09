"""Ralph Loop orchestrator for iterative task execution with progress persistence."""

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.settings import MonokaiDarkBGStyle
from rich.markdown import Markdown


@dataclass
class TaskState:
    """State stored in the progress file."""
    task_id: str
    goal: str
    status: str  # "in_progress" | "done" | "failed"
    iteration: int
    completed_steps: list
    pending_steps: list
    last_response: Optional[str]
    created_at: str
    updated_at: str


class RalphLoop:
    """Iterative agent loop with persistent progress tracking.
    
    Minimal Ralph pattern:
    1. Read progress file for current state
    2. Generate fresh context (relevant code + history summary)
    3. Run agent once with goal + progress + context
    4. Update progress file
    5. Check completion → exit or loop
    """

    def __init__(self, chat_manager, repo_root, rg_exe_path, console, debug_mode=False, max_iterations=20):
        self.chat_manager = chat_manager
        self.repo_root = repo_root
        self.rg_exe_path = rg_exe_path
        self.console = console
        self.debug_mode = debug_mode
        self.max_iterations = max_iterations
        
        # Task state
        self.task_state: Optional[TaskState] = None
        self.tasks_dir = Path(repo_root) / "tasks"
        self.tasks_dir.mkdir(exist_ok=True)

    def start(self, goal: str) -> bool:
        """Start a Ralph loop with the given goal.
        
        Args:
            goal: The task description from user
            
        Returns:
            True if task completed successfully, False otherwise
        """
        # Initialize task state
        task_id = str(uuid.uuid4())[:8]
        self.task_state = TaskState(
            task_id=task_id,
            goal=goal,
            status="in_progress",
            iteration=0,
            completed_steps=[],
            pending_steps=[goal],  # Start with the main goal as pending
            last_response=None,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        self._save_progress()

        self.console.print(f"[cyan]Ralph Loop started[/cyan] (id: {task_id})")
        
        # Main loop
        while self.task_state.iteration < self.max_iterations:
            if self.task_state.status != "in_progress":
                break
                
            self.task_state.iteration += 1
            self.task_state.updated_at = datetime.now().isoformat()
            
            # Build iteration prompt with fresh context
            prompt = self._build_iteration_prompt()
            
            if self.debug_mode:
                self.console.print(f"[dim]Iteration {self.task_state.iteration}/{self.max_iterations}[/dim]")
            
            # Run agent once (uses existing AgenticOrchestrator)
            from core.agentic import AgenticOrchestrator
            orchestrator = AgenticOrchestrator(
                chat_manager=self.chat_manager,
                repo_root=self.repo_root,
                rg_exe_path=self.rg_exe_path,
                console=self.console,
                debug_mode=self.debug_mode,
            )
            
            # Run without suppressing display - agent shows its work
            orchestrator.run(prompt)
            
            # Get last assistant message as "response"
            messages = self.chat_manager.messages
            last_response = None
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    last_response = msg.get("content") or ""
                    if msg.get("tool_calls"):
                        # Tool call response - get the last tool result
                        for rmsg in reversed(messages):
                            if rmsg.get("role") == "tool":
                                last_response = rmsg.get("content", "")
                                break
                    break
            
            self.task_state.last_response = last_response
            
            # Check for completion signals in response
            if self._check_completion(last_response):
                self.task_state.status = "done"
                self._save_progress()
                self.console.print("[green]Ralph Loop completed[/green]")
                return True
            
            # Check for failure signals
            if self._check_failure(last_response):
                self.task_state.status = "failed"
                self._save_progress()
                self.console.print("[red]Ralph Loop failed[/red]")
                return False
            
            # Mark iteration complete, loop for more
            self._save_progress()
        
        # Max iterations reached
        self.console.print(f"[yellow]Max iterations ({self.max_iterations}) reached[/yellow]")
        self.task_state.status = "failed"
        self._save_progress()
        return False

    def resume(self, task_id: str) -> bool:
        """Resume an existing Ralph loop from progress file.
        
        Args:
            task_id: The task ID to resume
            
        Returns:
            True if task completed successfully, False otherwise
        """
        progress_file = self.tasks_dir / f"{task_id}.json"
        if not progress_file.exists():
            self.console.print(f"[red]Task {task_id} not found[/red]")
            return False
        
        with open(progress_file) as f:
            data = json.load(f)
        
        self.task_state = TaskState(**data)
        
        if self.task_state.status == "done":
            self.console.print("[green]Task already completed[/green]")
            return True
        
        self.console.print(f"[cyan]Resuming Ralph Loop[/cyan] (id: {task_id}, iteration {self.task_state.iteration})")
        
        # Continue from where we left off
        while self.task_state.iteration < self.max_iterations:
            # Same loop logic as start()...
            pass
        
        return False

    def _build_iteration_prompt(self) -> str:
        """Build prompt for current iteration with context.
        
        Returns:
            String prompt with goal, progress context, and relevant code
        """
        parts = []
        
        # Header
        parts.append(f"## Ralph Loop Iteration {self.task_state.iteration}\n")
        
        # Goal
        parts.append(f"### Goal\n{self.task_state.goal}\n")
        
        # Progress
        if self.task_state.completed_steps:
            parts.append("### Completed Steps\n")
            for step in self.task_state.completed_steps:
                parts.append(f"- {step}")
            parts.append("")
        
        if self.task_state.pending_steps:
            parts.append("### Pending\n")
            for step in self.task_state.pending_steps:
                parts.append(f"- {step}")
            parts.append("")
        
        # Last response summary (truncated)
        if self.task_state.last_response:
            last = self.task_state.last_response[:500]
            if len(self.task_state.last_response) > 500:
                last += "..."
            parts.append(f"### Last Response\n{last}\n")
        
        # Fresh context: summarize recent message history
        context_summary = self._summarize_context()
        if context_summary:
            parts.append(f"### Code Context\n{context_summary}\n")
        
        # Instructions
        parts.append("""### Instructions
Work on the pending steps. When you complete a step, state "COMPLETE: <step description>" in your response.
When all steps are done, state "DONE" to end the loop.
Use tools as needed to accomplish each step.""")
        
        return "\n".join(parts)

    def _summarize_context(self) -> str:
        """Summarize recent messages for context.
        
        Returns:
            Truncated string of recent code/responses
        """
        # Get recent messages (last 20 to avoid too much context)
        messages = self.chat_manager.messages[-20:]
        
        summaries = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant":
                content = msg.get("content", "")
                if content and len(content) > 50:
                    # Truncate long responses
                    if len(content) > 200:
                        content = content[:200] + "..."
                    summaries.append(f"**Assistant**: {content}")
            elif role == "tool":
                content = msg.get("content", "")
                if content and len(content) > 20:
                    # Truncate tool results
                    if len(content) > 100:
                        content = content[:100] + "..."
                    summaries.append(f"**Tool result**: {content}")
        
        return "\n".join(summaries)

    def _check_completion(self, response: Optional[str]) -> bool:
        """Check if response indicates task completion."""
        if not response:
            return False
        # Look for DONE signal
        return "DONE" in response.upper() or "COMPLETE" in response.upper()

    def _check_failure(self, response: Optional[str]) -> bool:
        """Check if response indicates unrecoverable failure."""
        if not response:
            return False
        # Could add more failure signals
        return "CANNOT" in response.upper() and "IMPOSSIBLE" in response.upper()

    def _save_progress(self):
        """Save current state to progress file."""
        if not self.task_state:
            return
        
        progress_file = self.tasks_dir / f"{self.task_state.task_id}.json"
        with open(progress_file, "w") as f:
            json.dump(asdict(self.task_state), f, indent=2)


def ralph_answer(chat_manager, user_input, console, repo_root, rg_exe_path, debug_mode=False):
    """Ralph Loop entry point - iterative agent with progress persistence.
    
    Args:
        chat_manager: ChatManager instance
        user_input: User's input message (goal)
        console: Rich console for output
        repo_root: Path to repository root
        rg_exe_path: Path to rg.exe
        debug_mode: Whether to show debug output
    """
    loop = RalphLoop(
        chat_manager=chat_manager,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        debug_mode=debug_mode,
    )
    loop.start(user_input)