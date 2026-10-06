"""Read Out Loud with the computer's own speech engine (works offline).

Linux: espeak-ng, espeak or speech-dispatcher (spd-say). Windows: the built-in Windows voices,
reached through PowerShell's System.Speech. The text is always passed on standard input, never on a
command line or through a shell, so nothing in a PDF can change what program runs.
"""
from __future__ import annotations

import os
import re
import sys

from PySide6.QtCore import QObject, QProcess, Signal

from pdfdesk import externals

IS_WINDOWS = sys.platform.startswith("win")
MAX_CHUNK = 20000

_PS_SCRIPT = ("$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;$s.Rate=[int]$args[0];"
              "[Console]::InputEncoding=[Text.Encoding]::UTF8;$t=[Console]::In.ReadToEnd();"
              "if($t.Trim()){$s.Speak($t)}")


def engine() -> tuple[str, list[str]] | None:
    """(program, arguments) for the speech engine on this computer, or None. `{rate}` in an argument
    is replaced by the speed (-10 slow ... 10 fast)."""
    if IS_WINDOWS:
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        ps = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(ps):
            # everything after -Command is one command line, so the speed goes inside it as a number
            return ps, ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        "& {" + _PS_SCRIPT + "} {rate}"]
        return None
    for name, args in (("espeak-ng", ["--stdin", "-s", "{wpm}"]), ("espeak", ["--stdin", "-s", "{wpm}"]),
                       ("spd-say", ["-w", "-e", "-r", "{rate10}"])):
        path = externals.find_program(name)
        if path:
            return path, args
    return None


def available() -> bool:
    return engine() is not None


def missing_help() -> str:
    if IS_WINDOWS:
        return "Windows PowerShell wasn't found, so the Windows voices can't be used."
    return ("No speech engine was found. Install one with your package manager, for example:\n\n"
            "  sudo apt install espeak-ng      (Debian, Ubuntu, Mint)\n"
            "  sudo dnf install espeak-ng      (Fedora)\n"
            "  sudo pacman -S espeak-ng        (Arch)\n\n"
            "or run ./install.sh --extras. Then try again (no restart needed).")


def clean_for_speech(text: str) -> str:
    """Join words split across lines and tidy spacing so the voice reads naturally."""
    text = text.replace("\r", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]*\n[ \t]*\n+", ". \n", text)
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = "".join(ch for ch in text if ch.isprintable() or ch == "\n")
    return text.strip()[:MAX_CHUNK]


class Reader(QObject):
    """Speaks a list of text pieces one after another (for example one per page)."""
    started_piece = Signal(int)     # index of the piece now being read
    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self.pieces: list[str] = []
        self.index = -1
        self.rate = 0

    def is_reading(self) -> bool:
        return self.proc is not None

    def read(self, pieces: list[str], rate: int = 0) -> None:
        self.stop()
        self.pieces = [p for p in (clean_for_speech(x) for x in pieces)]
        self.rate = max(-10, min(10, int(rate)))
        self.index = -1
        if not any(self.pieces):
            self.finished.emit()
            return
        self._next()

    def _args(self, args: list[str]) -> list[str]:
        wpm = str(175 + self.rate * 12)
        rate10 = str(self.rate * 10)
        return [a.replace("{rate}", str(self.rate)).replace("{wpm}", wpm).replace("{rate10}", rate10) for a in args]

    def _next(self) -> None:
        self.index += 1
        while self.index < len(self.pieces) and not self.pieces[self.index]:
            self.index += 1
        if self.index >= len(self.pieces):
            self._cleanup()
            self.finished.emit()
            return
        found = engine()
        if found is None:
            self._cleanup()
            self.failed.emit("No speech engine was found on this computer.")
            return
        program, args = found
        proc = QProcess(self)
        proc.setProgram(program)
        proc.setArguments(self._args(args))
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(self._done)
        proc.errorOccurred.connect(self._error)
        self.proc = proc
        self.started_piece.emit(self.index)
        proc.start()
        if not proc.waitForStarted(5000):
            return
        proc.write(self.pieces[self.index].encode("utf-8"))
        proc.closeWriteChannel()

    def _done(self, *_):
        if self.proc is None:
            return
        self.proc.deleteLater()
        self.proc = None
        self._next()

    def _error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            self._cleanup()
            self.failed.emit("The speech engine couldn't be started.")

    def _cleanup(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.finished.disconnect(self._done)
            except (RuntimeError, TypeError):
                pass
            if proc.state() != QProcess.ProcessState.NotRunning:
                proc.kill()
                proc.waitForFinished(2000)
            proc.deleteLater()

    def stop(self) -> None:
        was = self.proc is not None
        self.pieces = []
        self._cleanup()
        if was:
            self.finished.emit()
