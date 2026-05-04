"""Shared swarm approval policy for worker command approvals.

Provides deterministic approval decisions for worker commands based on
danger-mode rules, safe command lists, and git-specific restrictions.

This module is the single source of truth for approval policy — used by
both the admin controller (for automatic decisions) and the UI (for
human-required approvals).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from utils.safe_commands import is_git_command, is_safe_command
from utils.validation import check_for_silent_blocked_command


class ApprovalDecision(Enum):
    """Deterministic approval decision."""
    APPROVED = "approved"
    DENIED = "denied"
    REQUIRES_HUMAN = "requires_human"


@dataclass(frozen=True)
class ApprovalResult:
    """Structured result from the approval policy.

    Attributes:
        decision: The approval decision.
        reason: Human-readable explanation for the decision.
        command: The original command string.
        is_git: Whether the command is a git invocation.
        is_safe: Whether the command passes the safe-command check.
        silent_blocked: Whether the command was silently blocked.
    """
    decision: ApprovalDecision
    reason: str
    command: str
    is_git: bool = False
    is_safe: bool = False
    silent_blocked: bool = False


def evaluate_swarm_approval(
    command: str,
    danger_mode: bool = False,
) -> ApprovalResult:
    """Evaluate a worker command against the swarm approval policy.

    Policy rules (in evaluation order):
    1. Silent-blocked commands (should use native tools) are denied.
    2. Globally safe/read-only commands are approved automatically.
    3. Git commands not in the safe set are denied automatically.
    4. In danger mode, non-git commands are approved automatically.
    5. Outside danger mode, non-safe commands require human approval.

    Args:
        command: The shell command string to evaluate.
        danger_mode: Whether the admin is in danger mode.

    Returns:
        ApprovalResult with the decision and metadata.
    """
    is_git = is_git_command(command)
    is_safe = is_safe_command(command)

    # Rule 1: Silent-blocked commands (native tool equivalents)
    silent_blocked, _ = check_for_silent_blocked_command(command)
    if silent_blocked:
        return ApprovalResult(
            decision=ApprovalDecision.DENIED,
            reason="Command blocked: use the native tool equivalent instead",
            command=command,
            is_git=is_git,
            is_safe=False,
            silent_blocked=True,
        )

    # Rule 2: Globally safe commands are always approved
    if is_safe:
        return ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            reason="Safe read-only command",
            command=command,
            is_git=is_git,
            is_safe=True,
        )

    # Rule 3: Unsafe git commands are always denied for workers
    if is_git:
        return ApprovalResult(
            decision=ApprovalDecision.DENIED,
            reason=(
                "Git command denied by swarm policy: only read-only git "
                "subcommands (status, diff, log, show, etc.) are allowed "
                "for workers"
            ),
            command=command,
            is_git=True,
            is_safe=False,
        )

    # Rule 4: In danger mode, non-git commands are approved
    if danger_mode:
        return ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            reason="Approved (danger mode active)",
            command=command,
            is_git=False,
            is_safe=False,
        )

    # Rule 5: Outside danger mode, requires human approval
    return ApprovalResult(
        decision=ApprovalDecision.REQUIRES_HUMAN,
        reason="Command requires human approval (not a safe command)",
        command=command,
        is_git=False,
        is_safe=False,
    )
