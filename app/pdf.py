"""PDF rendering in a short-lived child process.

WeasyPrint needs ~80 MB extra per render and the Python allocator doesn't hand that back to the OS.
Rendering in a separate process that exits afterwards keeps the long-running app small, which matters
on small VPSes next to Uptime Kuma. This module deliberately imports nothing from the app.
"""
from __future__ import annotations

import importlib.util
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

PAGE_CSS = ("@page { size: A4; margin: 10mm 8mm; } .kr-canvas, .kr-content { padding: 0 !important; } "
            "tr { page-break-inside: avoid; }")


def _render(html: str) -> bytes:
    from weasyprint import CSS, HTML
    # Mail layouts use a grey canvas and a ~760px column; drop the canvas and scale to A4.
    html = html.replace("background:#f3f4f6", "background:#ffffff")
    return HTML(string=html).write_pdf(stylesheets=[CSS(string=PAGE_CSS)], zoom=0.85, presentational_hints=True)


def html_to_pdf(html: str, timeout: float = 120) -> bytes:
    # Only check that it exists: importing it here would load it into the long-running process.
    if importlib.util.find_spec("weasyprint") is None:
        raise RuntimeError("PDF output needs WeasyPrint (included in the Docker image)")
    ctx = multiprocessing.get_context("spawn")  # never fork the threaded web server
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        return pool.submit(_render, html).result(timeout=timeout)
