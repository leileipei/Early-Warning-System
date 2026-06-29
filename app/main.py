from fastapi import FastAPI

from app.settings import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title=get_settings().app_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
