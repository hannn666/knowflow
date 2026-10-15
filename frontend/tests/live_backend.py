"""Owned M5-3 FastAPI process: an explicit disposable PG database only."""
import json
import os
from pathlib import Path
import re
import sys
from threading import Thread
import time


def main():
    if os.environ.get("KNOWFLOW_M53_LIVE") != "1":
        raise RuntimeError("Explicit live test opt-in required")
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    database = os.environ.get("KNOWFLOW_M53_DB","")
    if not re.fullmatch(r"knowflow_test_m53_[0-9a-f]{32}",database):
        raise RuntimeError("Expected this run's disposable database")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from project.db.business_database import get_database_url, get_database_session
    from project.api.app import create_app
    from project.api.auth_service import register_user
    from project.api.grounded_service import GroundedService, get_grounded_service
    from project.core.grounded_llm import get_grounded_llm
    from project.core.index_embeddings import get_index_embeddings
    from project.core.index_models import IndexSettings
    from project.core.version_index import VersionIndex
    import uvicorn

    source=get_database_url()
    if (source.host,source.port,source.database)!=("127.0.0.1",5432,"knowflow"):
        raise RuntimeError("Unexpected maintenance instance")
    engine=create_engine(source.set(database=database),hide_parameters=True)
    with Session(engine) as session:
        for email in ("m53-alice@example.com","m53-bob@example.com"):
            register_user(session,email,"Fixture-only-password-123")
    encoder,spec=get_index_embeddings()  # Production offline cache-only implementation.
    index=VersionIndex(IndexSettings())
    metrics_path=Path(os.environ["KNOWFLOW_M53_RUN_ROOT"])/"backend-metrics.json"
    metrics={"llm_calls":0,"llm_completed":0,"model_statuses":[],
             "embedding_model":spec.model,"embedding_revision":spec.revision,
             "embedding_dimension":spec.dimension,"qdrant_url":"http://127.0.0.1:6333"}
    def record():
        temporary=metrics_path.with_suffix(".part")
        temporary.write_text(json.dumps(metrics,indent=2),encoding="utf-8")
        temporary.replace(metrics_path)
    class ObservedLLM:
        def generate(self,question,evidence):
            metrics["llm_calls"]+=1
            record()
            value=get_grounded_llm().generate(question,evidence)  # No simulated output.
            metrics["llm_completed"]+=1
            try:
                status=json.loads(value).get("status")
                metrics["model_statuses"].append(status if status in {"answered","insufficient_evidence"} else "invalid")
            except (ValueError,AttributeError):
                metrics["model_statuses"].append("invalid")
            record()
            return value
    record()
    service=GroundedService(index=index,encoder=encoder,spec=spec,llm=ObservedLLM())
    app=create_app()
    def sessions():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_database_session]=sessions
    app.dependency_overrides[get_grounded_service]=lambda:service
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=8109,log_level="warning",access_log=False))
    def stop_when_requested():
        marker=metrics_path.parent/"shutdown"
        while not marker.exists():
            time.sleep(.1)
        server.should_exit=True
    Thread(target=stop_when_requested,daemon=True).start()
    try:
        server.run()
    finally:
        index.close()
        engine.dispose()


if __name__=="__main__":
    try:
        main()
    except Exception as error:
        print("Isolated backend startup failed: "+type(error).__name__,flush=True)
        raise SystemExit(1) from None
