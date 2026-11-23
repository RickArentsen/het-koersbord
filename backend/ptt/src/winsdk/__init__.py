"""
A stand-in for ``winsdk``, the only addition to the tracker's own code.

The tracker's OCR (``ocr.py``) reads scanned filings with Windows.Media.Ocr
through ``winsdk``, which ships no build for Python 3.13 (the terminal's).
Its maintained successor, pywinrt's ``winrt-*`` packages, carries the same
classes under ``winrt.windows.*``; these modules re-export the five that
``ocr.py`` uses, so ``ocr.py`` itself stays as written.
"""
