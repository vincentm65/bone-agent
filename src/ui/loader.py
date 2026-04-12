"""Animated terminal effects for vmCode — loading spinners, progress bars, and visual flair."""

import time
import random
import threading
from rich.console import Console
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.align import Align

console = Console()


# ── ASCII art logo ──────────────────────────────────────────────────────────

VMCODE_LOGO = r"""
   ╦ ╦┌─┐┌┐ ╔╦╗┬┬  ┌─┐
   ║║║├┤ ├┴┐ ║ ││  ├┤
   ╚╩╝└─┘└─┘ ╩ ┴┴─┘└─┘
"""

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
DOT_FRAMES = ["   ", ".  ", ".. ", "..."]
WAVE_FRAMES = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂", "▁"]
BAR_FRAMES = ["◐", "◓", "◑", "◒"]


def _wave_string(text: str, frame_idx: int, color: str = "#5F9EA0") -> Text:
    """Create a wave-animated string where each character oscillates vertically."""
    result = Text()
    n = len(WAVE_FRAMES)
    for i, ch in enumerate(text):
        offset = (frame_idx + i * 2) % n
        bar_char = WAVE_FRAMES[offset]
        # Fade intensity based on wave position (center = bright)
        intensity = abs(offset - n // 2) / (n // 2)
        if ch == " ":
            result.append(" ")
        else:
            result.append(bar_char, style=color)
    return result


def display_intro_animation(provider: str = "", model: str = ""):
    """Play a cinematic intro animation on startup.

    Shows the vmCode logo with a typing effect, a wave animation underneath,
    and provider/model info fading in.
    """
    logo_lines = VMCODE_LOGO.strip("\n").split("\n")

    try:
        with Live(console=console, transient=False, refresh_per_second=24) as live:
            # Phase 1: Logo reveal (typewriter effect)
            revealed_lines = []
            for line_idx, line in enumerate(logo_lines):
                revealed_lines.append("")
                for ch_idx, ch in enumerate(line):
                    revealed_lines[line_idx] = line[: ch_idx + 1]
                    layout = Layout()
                    logo_text = Text("\n".join(revealed_lines), style="bold #5F9EA0")
                    layout.update(Align.center(Panel(
                        logo_text,
                        border_style="grey30",
                        padding=(1, 4),
                        subtitle=Text("  ", style="dim"),
                    )))
                    live.update(layout)
                    time.sleep(0.008)
                time.sleep(0.04)

            # Phase 2: Wave animation beneath the logo (runs for ~2 seconds)
            wave_line = "━" * 42
            start = time.time()
            frame = 0
            while time.time() - start < 1.8:
                wave = _wave_string(wave_line, frame, color="#3a7ca5")
                layout = Layout()
                full = Text()
                full.append("\n".join(logo_lines) + "\n", style="bold #5F9EA0")
                full.append(wave)
                layout.update(Align.center(Panel(
                    full,
                    border_style="grey30",
                    padding=(1, 4),
                )))
                live.update(layout)
                frame += 1
                time.sleep(0.05)

            # Phase 3: Show tagline + provider info
            tagline = Text("  local-first  ·  agent-powered  ·  terminal-native", style="dim grey60")
            if provider and model:
                info = Text.assemble(
                    ("  ", ""),
                    (f"● {provider.upper()} ", "bold #5F9EA0"),
                    (f"{model}", "grey70"),
                    style="",
                )
            else:
                info = Text("")

            layout = Layout()
            full = Text()
            full.append("\n".join(logo_lines), style="bold #5F9EA0")
            full.append("\n")
            full.append(wave_line, style="#3a7ca5")
            layout.update(Align.center(Panel(
                Align.center(
                    Table.grid(padding=(0, 0)),
                ),
                border_style="grey30",
                padding=(1, 4),
                subtitle=tagline,
            )))
            live.update(layout)
            time.sleep(0.6)

            # Final frame — static logo with tagline
            final = Text()
            final.append("\n".join(logo_lines), style="bold #5F9EA0")
            final.append("\n")
            final.append(wave_line, style="#3a7ca5")
            layout.update(Align.center(Panel(
                final,
                border_style="grey30",
                padding=(1, 4),
                subtitle=tagline,
            )))
            live.update(layout)
            time.sleep(0.3)
    except Exception:
        # Fallback: if Live fails (e.g. non-TTY), just print static logo
        console.print(Panel(
            Text(VMCODE_LOGO.strip("\n"), style="bold #5F9EA0"),
            border_style="grey30",
            subtitle="  local-first  ·  agent-powered  ·  terminal-native",
        ))


# ── Spinner context manager ─────────────────────────────────────────────────

class Spinner:
    """A rich-based status spinner for long-running operations.

    Usage:
        with Spinner("Indexing files..."):
            do_expensive_work()
    """

    def __init__(self, message: str, style: str = "#5F9EA0", done_message: str = "done"):
        self.message = message
        self.style = style
        self.done_message = done_message
        self._stop = threading.Event()
        self._thread = None

    def _spin(self):
        frame = 0
        n = len(SPINNER_FRAMES)
        with Live(console=console, transient=True, refresh_per_second=12) as live:
            while not self._stop.is_set():
                spinner = SPINNER_FRAMES[frame % n]
                live.update(Text(f"  {spinner} {self.message}", style=self.style))
                frame += 1
                self._stop.wait(0.08)

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        console.print(f"  ✓ {self.message} {self.done_message}", style="dim green")

    def ok(self, msg: str = ""):
        """Mark as done with a custom message."""
        self.done_message = msg


# ── Progress bar ────────────────────────────────────────────────────────────

class ProgressBar:
    """A lightweight animated progress bar for terminal output.

    Usage:
        with ProgressBar("Loading", total=100) as bar:
            for i in range(100):
                bar.update(i + 1)
    """

    def __init__(self, label: str = "", total: int = 100, width: int = 30,
                 fill: str = "█", empty: str = "░", color: str = "#5F9EA0"):
        self.label = label
        self.total = total
        self.width = width
        self.fill = fill
        self.empty = empty
        self.color = color
        self.current = 0
        self._stop = threading.Event()

    def update(self, value: int):
        self.current = min(value, self.total)

    def _render(self) -> Text:
        pct = self.current / self.total if self.total else 0
        filled = int(self.width * pct)
        bar = self.fill * filled + self.empty * (self.width - filled)
        result = Text()
        if self.label:
            result.append(f"  {self.label} ", style="dim")
        result.append(bar, style=self.color)
        result.append(f" {pct:>5.1%}", style="dim grey70")
        return result

    def _animate(self):
        with Live(console=console, transient=True, refresh_per_second=8) as live:
            while not self._stop.is_set():
                live.update(self._render())
                self._stop.wait(0.1)

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        # Print final state
        console.print(self._render())


# ── Scan-line / matrix effect (decorative) ─────────────────────────────────

def matrix_rain(duration: float = 1.5, cols: int = 60, rows: int = 8, chars: str = "01アイウエオカキクケコ"):
    """Print a brief Matrix-style rain effect to the terminal.

    Purely decorative — great for transitions between sections.
    """
    random.seed()
    try:
        with Live(console=console, transient=True, refresh_per_second=16) as live:
            start = time.time()
            # Each column has a falling "drop" at a random row
            drops = [random.randint(0, rows - 1) for _ in range(cols)]
            speeds = [random.uniform(0.3, 1.0) for _ in range(cols)]
            phases = [random.random() * duration for _ in range(cols)]

            while time.time() - start < duration:
                grid = []
                t = time.time() - start
                for r in range(rows):
                    row = Text()
                    for c in range(cols):
                        # Determine if this cell is "active"
                        drop_pos = drops[c] + int((t - phases[c]) * speeds[c] * rows / duration)
                        drop_pos = drop_pos % (rows + 4)  # wrap around
                        dist = drop_pos - r
                        if 0 <= dist <= 3:
                            ch = random.choice(chars)
                            if dist == 0:
                                row.append(ch, style="bold white")
                            else:
                                fade = f"#{max(0, 0x10):02x}{max(0, 0x40 + (3 - dist) * 0x20):02x}{max(0, 0x10):02x}"
                                row.append(ch, style=fade)
                        else:
                            row.append(" ")
                    grid.append(row)

                live.update(Text("\n").join(grid))
                time.sleep(0.04)
    except Exception:
        pass  # silently skip if terminal doesn't support it
