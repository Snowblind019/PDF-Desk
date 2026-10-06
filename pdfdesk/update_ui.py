"""The Qt side of update checks: a background check at start-up (once a day, can be turned off in
Preferences), Help > Check for updates, the Yes/No prompt, and the download with a progress bar.
The network and checking work lives in updater.py.

Network work runs on plain daemon threads, never QThreads, so closing PDF Desk in the middle of a
check or download can't crash it: the thread is simply dropped. Results come back to the GUI thread
through a small signal bridge. Only one check, prompt or download happens at a time."""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from pdfdesk import APP_NAME, __version__, jobs, ui, updater


class _Bridge(QObject):
    done = Signal(object, str)
    progressed = Signal(int, int)


class _Job:
    """One function on a daemon thread. Its result arrives as bridge.done(result, error)."""

    def __init__(self, fn, parent: QObject):
        self.bridge = _Bridge(parent)
        self.cancel = False
        self._fn = fn
        self._thread = threading.Thread(target=self._run, name="pdfdesk-update", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def progress(self, done: int, total: int) -> None:
        try:
            self.bridge.progressed.emit(done, total)
        except RuntimeError:  # PDF Desk is closing
            pass

    def _run(self) -> None:
        try:
            result, error = self._fn(self), ""
        except updater.UpdateError as exc:
            result, error = None, str(exc)
        except Exception as exc:  # noqa: BLE001 - never let a background check crash the app
            result, error = None, f"Something went wrong: {type(exc).__name__}"
        try:
            self.bridge.done.emit(result, error)
        except RuntimeError:  # PDF Desk is closing
            pass


class UpdateController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.settings = window.settings
        self._job: _Job | None = None
        self._kind = ""              # "check" or "download"
        self._manual = False
        self._prompt_open = False
        self._offer_id = 0           # a newer offer cancels a delayed older one
        self._progress: QProgressDialog | None = None

    def busy(self) -> bool:
        return self._job is not None or self._prompt_open

    def shutdown(self) -> None:
        """PDF Desk is closing: stop whatever is running (the thread is dropped with the process)."""
        if self._job is not None:
            self._job.cancel = True

    # ------------------------------------------------------------------ checking
    def check_in_background(self) -> None:
        """The start-up check. Quiet: it only says something when there's a new version."""
        if updater.due(self.settings) and not self.busy():
            self._check(manual=False)

    def check_now(self) -> None:
        """Help > Check for updates."""
        if self._kind == "download" or self._prompt_open:
            return
        if self._job is not None:  # a quiet start-up check is running: let it report back
            self._manual = True
            return
        self._check(manual=True)

    def _check(self, manual: bool) -> None:
        self._manual = manual
        if manual:
            self.window.msg("Checking for updates...", 60000)
        self._start(lambda _job: updater.latest_release(), "check")

    def _start(self, fn, kind: str) -> None:
        job = _Job(fn, self)
        job.bridge.done.connect(self._job_done)
        job.bridge.progressed.connect(self._job_progress)
        self._job, self._kind = job, kind
        job.start()

    def _job_done(self, result, error: str) -> None:
        job, kind = self._job, self._kind
        self._job, self._kind = None, ""
        if job is not None:
            job.bridge.deleteLater()
        if kind == "check":
            if job is not None and job.cancel:
                return  # PDF Desk is closing: don't pop anything up
            self._checked(result, error)
        elif kind == "download":
            self._downloaded(job, result, error)

    def _checked(self, release, error: str) -> None:
        manual = self._manual
        if manual:
            self.window.msg("")
        if error:
            if manual:
                ui.warning(self.window, "Check for updates", f"Couldn't check for updates.\n\n{error}")
            return  # a failed start-up check stays quiet and tries again next time
        self.settings.set("last_update_check", time.time())
        if not updater.is_newer(release):
            if manual:
                ui.information(self.window, "Check for updates",
                               f"You have the latest version of {APP_NAME} ({__version__}).")
            return
        if not manual and self.settings.get("skipped_version") == release["version_text"]:
            return
        self._offer_id += 1
        self._offer(release, manual, self._offer_id)

    # ------------------------------------------------------------------ asking
    def _offer(self, release: dict, manual: bool, offer_id: int, tries: int = 0) -> None:
        if offer_id != self._offer_id or self.busy():
            return  # replaced by a newer offer, or something else is already happening
        # don't pop up over another dialog or a running task: ask a little later instead
        if (QApplication.activeModalWidget() is not None or jobs.busy()) and tries < 40:
            QTimer.singleShot(15000, lambda: self._offer(release, manual, offer_id, tries + 1))
            return
        new, cur = release["version_text"], __version__
        why = updater.why_not_automatic(release)
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Update available")
        box.setTextFormat(Qt.TextFormat.PlainText)
        if not why:
            box.setText(f"{APP_NAME} {new} is available. You have {cur}.\n\nInstall it now? {APP_NAME} will "
                        "close, update itself and open again. You'll be asked to save any changes first.")
        else:
            box.setText(f"{APP_NAME} {new} is available. You have {cur}.\n\n{why}\n\nOpen the download page?")
        if release.get("notes"):
            box.setDetailedText(release["notes"])  # always shown as plain text
        yes = box.addButton("Install now" if not why else "Open download page", QMessageBox.ButtonRole.YesRole)
        box.addButton("Not now", QMessageBox.ButtonRole.NoRole)
        skip = None if manual else box.addButton("Skip this version", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes)
        self._prompt_open = True
        try:
            box.exec()
        finally:
            self._prompt_open = False
        clicked = box.clickedButton()
        if skip is not None and clicked is skip:
            self.settings.set("skipped_version", new)
        elif clicked is yes:
            if why:
                QDesktopServices.openUrl(QUrl(release["page"]))
            else:
                self._download(release)

    # ------------------------------------------------------------------ installing
    def _download(self, release: dict) -> None:
        if self._job is not None:
            return
        dlg = QProgressDialog(f"Downloading {APP_NAME} {release['version_text']}...", "Cancel", 0, 0, self.window)
        dlg.setWindowTitle("Updating")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._progress = dlg
        self._start(lambda job: updater.prepare(release, progress=job.progress, cancelled=lambda: job.cancel),
                    "download")
        job = self._job

        def cancel() -> None:
            job.cancel = True
            dlg.setLabelText("Cancelling...")
        dlg.canceled.connect(cancel)
        dlg.show()

    def _job_progress(self, done: int, total: int) -> None:
        dlg = self._progress
        if dlg is not None and total > 0:
            dlg.setMaximum(100)
            dlg.setValue(min(99, int(done * 100 / total)))

    def _downloaded(self, job: _Job, prepared, error: str) -> None:
        cancelled = job.cancel  # read before closing the dialog (closing it counts as Cancel)
        dlg, self._progress = self._progress, None
        if dlg is not None:
            try:
                dlg.canceled.disconnect()
            except (RuntimeError, TypeError):
                pass
            dlg.close()
            dlg.deleteLater()
        if cancelled:  # cancelled, even if the download had already finished
            updater.discard(prepared)
            return
        if error:
            ui.warning(self.window, "Update", f"The update wasn't installed.\n\n{error}")
            return
        if updater.other_copies_running():
            updater.discard(prepared)
            ui.information(self.window, "Update", f"Another {APP_NAME} window is open. Close it, then choose "
                           "Help > Check for updates again. Nothing was changed.")
            return
        # Close like the user would, so unsaved changes get the usual "save?" question.
        if not self.window.close():
            updater.discard(prepared)
            ui.information(self.window, "Update", "The update was cancelled. Nothing was changed.")
            return
        try:
            updater.start_install(prepared)
        except OSError as exc:
            updater.discard(prepared)
            ui.warning(None, "Update", f"The installer couldn't be started, so nothing was changed.\n\n{exc}")
            return
        QApplication.quit()


def report_last_update(window) -> None:
    """After an update PDF Desk is started again with PDFDESK_UPDATE_RESULT set; say how it went."""
    result = os.environ.pop("PDFDESK_UPDATE_RESULT", "")
    if result.startswith("ok:"):
        updater.clean_up_after_update()
        window.msg(f"Updated to {APP_NAME} {__version__}.", 10000)
    elif result.startswith("skipped:"):
        ui.information(window, "Update", f"The update to {APP_NAME} {result[len('skipped:'):]} wasn't installed "
                       "because PDF Desk was still open. Choose Help > Check for updates to try again.")
    elif result.startswith("failed:"):
        log = result[len("failed:"):]
        ui.warning(window, "Update", "The update didn't finish. Your files and settings are fine, but some parts "
                   "of PDF Desk may not have been updated. Download the new version from the releases page and run "
                   f"its installer to fix it.\n\nDetails: {log}")
