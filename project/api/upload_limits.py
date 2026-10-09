"""Limits only for the document upload route, including chunked requests."""
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.types import Receive, Scope, Send


MAX_REQUEST_BYTES = 11 * 1024 * 1024
NO_STORE = {"Cache-Control": "no-store"}


class DocumentUploadRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] != "POST" or "POST" not in (self.methods or set()):
            await super().handle(scope, receive, send)
            return
        lengths = [value for key, value in scope["headers"] if key.lower() == b"content-length"]
        if lengths and (len(lengths) != 1 or not lengths[0].isdigit()):
            await JSONResponse({"detail": "Invalid Content-Length"}, 400, headers=NO_STORE)(scope, receive, send)
            return
        # Avoid converting an arbitrarily long numeric header with int().
        normalized = (lengths[0].lstrip(b"0") or b"0") if lengths else b"0"
        maximum = str(MAX_REQUEST_BYTES).encode()
        if len(normalized) > len(maximum) or (len(normalized) == len(maximum) and normalized > maximum):
            await JSONResponse({"detail": "Upload request too large"}, 413, headers=NO_STORE)(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Upload request too large", headers=NO_STORE)
            return message

        await super().handle(scope, limited_receive, send)
