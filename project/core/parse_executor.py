"""Bounded synchronous execution in a disposable native-parser process."""
from contextlib import contextmanager
import multiprocessing
import os
from threading import BoundedSemaphore
import time

from project.core.document_parser import PdfParsingError, parse_pdf_version
from project.core.document_storage import DocumentStorage
from project.core.parse_artifacts import (
    MAX_PARSED_PAGES, MAX_TEXT_CHARACTERS, ParseArtifactError, ParseArtifactStore,
)


class ParseExecutionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ParseExecutionUncertain(Exception):
    """Shutdown cannot be established; retain claim/files instead of cleaning."""
    pass


class ParseCapacityUnavailable(Exception):
    pass


def _parse_worker(source, storage_root, reservation, sender):
    # Native diagnostics are silenced only in this isolated process. Parent
    # Gradio/RAG/PyMuPDF settings are untouched; never send full text over IPC.
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    with open(os.devnull, 'w') as quiet:
        import contextlib
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            try:
                storage = DocumentStorage(storage_root)
                result = parse_pdf_version(source, storage, max_pages=MAX_PARSED_PAGES,
                                           max_text_characters=MAX_TEXT_CHARACTERS)
                digest = ParseArtifactStore(storage).publish(reservation, result)
                sender.send(('ok', digest))
            except (PdfParsingError, ParseArtifactError) as error:
                sender.send(('error', error.code))
            except (OSError, UnicodeError):
                sender.send(('error', 'artifact_write_failed'))
            except BaseException:
                sender.send(('error', 'worker_failure'))
            finally:
                sender.close()


class BoundedParseExecutor:
    def __init__(self, timeout_seconds=30.0, max_processes=2, worker_target=_parse_worker):
        if timeout_seconds <= 0 or max_processes < 1:
            raise ValueError('Invalid parser execution budget')
        self.timeout_seconds = timeout_seconds
        self._slots = BoundedSemaphore(max_processes)
        self._worker_target = worker_target
        self._blocked = False

    @contextmanager
    def slot(self):
        if self._blocked or not self._slots.acquire(blocking=False):
            raise ParseCapacityUnavailable
        try:
            yield
        finally:
            self._slots.release()

    def run(self, source, artifacts, reservation):
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=self._worker_target,
                                  args=(source, artifacts.storage.root, reservation, sender))
        started = False
        deadline = time.monotonic() + self.timeout_seconds
        try:
            try:
                process.start()
            except Exception:
                raise ParseExecutionError('worker_failure') from None
            started = True
            sender.close()
            if not receiver.poll(max(0, deadline - time.monotonic())):
                raise ParseExecutionError('parse_timeout')
            try:
                message = receiver.recv()
            except (EOFError, OSError):
                raise ParseExecutionError('worker_failure') from None
            process.join(timeout=max(0, deadline - time.monotonic()))
            if process.is_alive():
                raise ParseExecutionError('parse_timeout')
            if process.exitcode != 0 or not isinstance(message, tuple) or len(message) != 2:
                raise ParseExecutionError('worker_failure')
            kind, value = message
            if kind == 'error':
                raise ParseExecutionError(value)
            if kind != 'ok' or not isinstance(value, str) or len(value) != 64:
                raise ParseExecutionError('worker_failure')
            return value
        finally:
            uncertain = False
            try:
                if started and process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
                    uncertain = process.is_alive()
            except Exception:
                uncertain = True
            finally:
                receiver.close()
                sender.close()
                if started and not process.is_alive():
                    process.close()
            if uncertain:
                self._blocked = True
                raise ParseExecutionUncertain

_EXECUTOR = BoundedParseExecutor()


def get_parse_executor():
    return _EXECUTOR
