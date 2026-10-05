"""Run slow work (conversions, OCR, compression) on a background thread with a progress dialog.

Background jobs only ever touch their own PDF copies (passed in as bytes or file paths).
While a job runs, the page canvas avoids rendering new pages, so MuPDF is never used from
two threads on the same document."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QProgressDialog, QWidget

from pdfdesk.pdfops import Cancelled

_running = 0
_active: set = set()  # keep controllers alive until their job ends


def busy() -> bool:
    return _running > 0


class _Worker(QObject):
    progressed = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, func, args, kwargs):
        super().__init__()
        self.func, self.args, self.kwargs = func, args, kwargs
        self.stop = False

    def _progress(self, done: int, total: int, msg: str) -> bool:
        self.progressed.emit(done, total, msg)
        return not self.stop

    @Slot()
    def run(self):
        try:
            result = self.func(*self.args, progress=self._progress, **self.kwargs)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # report every failure to the user instead of crashing
            self.failed.emit(str(exc) or exc.__class__.__name__, traceback.format_exc())
        else:
            self.finished.emit(result)


class _Controller(QObject):
    """Lives on the GUI thread and receives the worker's signals there."""

    def __init__(self, parent: QWidget, title: str, func, args, kwargs, on_done, on_error, cancellable: bool):
        super().__init__(parent)
        self.parent_widget = parent
        self.title = title
        self.on_done = on_done
        self.on_error = on_error
        self.dlg = QProgressDialog("", "Cancel" if cancellable else "", 0, 0, parent)
        self.dlg.setWindowTitle(title)
        self.dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.dlg.setMinimumDuration(400)
        self.dlg.setAutoClose(False)
        self.dlg.setAutoReset(False)
        self.dlg.setMinimumWidth(380)
        if not cancellable:
            self.dlg.setCancelButton(None)
        label = QLabel()  # plain text, since messages can include file names
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.dlg.setLabel(label)
        self.dlg.setLabelText(title)
        self.thread = QThread()
        self.worker = _Worker(func, args, kwargs)
        self.worker.moveToThread(self.thread)
        self.worker.progressed.connect(self.on_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.on_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self.on_failed, Qt.ConnectionType.QueuedConnection)
        self.worker.cancelled.connect(self.on_cancelled, Qt.ConnectionType.QueuedConnection)
        self.dlg.canceled.connect(self.request_stop)
        self.thread.started.connect(self.worker.run)

    def start(self) -> None:
        global _running
        _running += 1
        _active.add(self)
        self.thread.start()

    @Slot()
    def request_stop(self) -> None:
        self.worker.stop = True

    def _finish(self) -> None:
        global _running
        _running = max(0, _running - 1)
        self.dlg.reset()
        self.dlg.hide()
        self.dlg.deleteLater()
        self.thread.quit()
        self.thread.wait(10000)
        self.worker.deleteLater()
        self.thread.deleteLater()
        _active.discard(self)
        self.deleteLater()
        # let views that skipped rendering during the job catch up
        for w in QApplication.topLevelWidgets():
            w.update()

    @Slot(int, int, str)
    def on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self.dlg.setMaximum(total)
            self.dlg.setValue(min(done, total))
        if msg:
            self.dlg.setLabelText(msg)

    @Slot(object)
    def on_finished(self, result) -> None:
        self._finish()
        if self.on_done:
            self.on_done(result)

    @Slot(str, str)
    def on_failed(self, message: str, details: str) -> None:
        self._finish()
        if self.on_error:
            self.on_error(message)
            return
        box = QMessageBox(QMessageBox.Icon.Warning, self.title, message, parent=self.parent_widget)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setDetailedText(details)
        box.exec()

    @Slot()
    def on_cancelled(self) -> None:
        self._finish()


def run_job(parent: QWidget, title: str, func, *args, on_done=None, on_error=None,
            cancellable: bool = True, **kwargs) -> None:
    """Run func(*args, progress=cb, **kwargs) in a thread. on_done(result) runs on the GUI thread."""
    ctl = _Controller(parent, title, func, args, kwargs, on_done, on_error, cancellable)
    ctl.start()


def run_now(func, *args, **kwargs):
    """Run a job function synchronously (used by tests and tiny inputs)."""
    return func(*args, progress=None, **kwargs)


def later(ms: int, fn) -> None:
    QTimer.singleShot(ms, fn)
