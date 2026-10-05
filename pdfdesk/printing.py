"""Printing through the system print dialog (works with any printer the OS knows about)."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPageLayout
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import QApplication, QProgressDialog

import pymupdf as fitz

from pdfdesk import fonts, ui
from pdfdesk.document import PdfDocument


def print_document(parent, pdf: PdfDocument, current: int) -> None:
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setDocName(pdf.title)
    printer.setFromTo(1, pdf.page_count)
    first = pdf.page_rect(0)
    printer.setPageOrientation(QPageLayout.Orientation.Landscape if first.width > first.height
                               else QPageLayout.Orientation.Portrait)
    dlg = QPrintDialog(printer, parent)
    dlg.setWindowTitle(f"Print {pdf.title}")
    dlg.setOption(QPrintDialog.PrintDialogOption.PrintCurrentPage, True)
    dlg.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, True)
    dlg.setMinMax(1, pdf.page_count)
    if dlg.exec() != QPrintDialog.DialogCode.Accepted:
        return
    mode = printer.printRange()
    if mode == QPrinter.PrintRange.PageRange:
        pages = list(range(max(1, printer.fromPage()) - 1, min(pdf.page_count, printer.toPage())))
    elif mode == QPrinter.PrintRange.CurrentPage:
        pages = [current]
    else:
        pages = list(range(pdf.page_count))
    if not pages:
        return
    dpi = min(300, printer.resolution())
    painter = QPainter()
    if not painter.begin(printer):
        ui.warning(parent, "Print", "The printer could not be started.")
        return
    progress = QProgressDialog("Printing...", "Cancel", 0, len(pages), parent)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(500)
    try:
        for n, pno in enumerate(pages):
            if progress.wasCanceled():
                break
            progress.setValue(n)
            QApplication.processEvents()
            if n > 0:
                printer.newPage()
            page = pdf.page(pno)
            z = fonts.safe_zoom(page.rect, dpi / 72)
            pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False, annots=True)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            area = QRectF(painter.viewport())
            scale = min(area.width() / img.width(), area.height() / img.height())
            w, h = img.width() * scale, img.height() * scale
            target = QRectF(area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h)
            painter.drawImage(target, img)
    finally:
        painter.end()
        progress.setValue(len(pages))
