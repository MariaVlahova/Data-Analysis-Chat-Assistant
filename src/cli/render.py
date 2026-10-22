"""Rich output helpers.

Progress and answers are kept apart (S9): only short progress text shows while a turn runs, and the
answer prints once, after the Output Guard. Every user-derived string goes through `Text`, never
through markup, so a message like "[/oops]" cannot crash the screen.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

ProgressFn = Callable[[str], None]


class Renderer:
    def __init__(self, console: Console | None = None, err_console: Console | None = None) -> None:
        self.console = console or Console()
        self.err_console = err_console or Console(stderr=True)

    # input ---------------------------------------------------------------
    def prompt(self, name: str) -> str:
        return self.console.input(f"[bold cyan]{escape(name)}[/]> ")

    # progress ------------------------------------------------------------
    @contextmanager
    def progress(self, message: str = "Working...") -> Iterator[ProgressFn]:
        with self.console.status(Text(message), spinner="dots") as status:
            yield lambda text: status.update(Text(text))

    # output --------------------------------------------------------------
    def banner(self, text: str) -> None:
        self.console.print(Text(text, style="dim"))

    def info(self, text: str) -> None:
        self.console.print(Text(text))

    def answer(self, markdown: str) -> None:
        self.console.print(Markdown(markdown))

    def refusal(self, text: str) -> None:
        self.console.print(Panel(Text(text), title="Not possible", border_style="yellow"))

    def error(self, text: str) -> None:
        self.console.print(Panel(Text(text), title="Something went wrong", border_style="red"))

    def traceback(self) -> None:
        self.console.print_exception(show_locals=False)

    def goodbye(self) -> None:
        self.console.print(Text("Goodbye.", style="dim"))

    # startup problems go to stderr: one clear sentence and the fix ---------
    def startup_problem(self, message: str, fix: str | None = None) -> None:
        self.err_console.print(Text(f"Error: {message}", style="bold red"))
        if fix:
            self.err_console.print(Text(f"Fix: {fix}"))
