from fastapi import FastAPI
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def get_health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app
