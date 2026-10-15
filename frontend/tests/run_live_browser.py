r"""Opt-in real M5-3 browser verification; owns and cleans only new UUID resources.

From E:\Dev\knowflow:
  $env:KNOWFLOW_M53_LIVE='1'
  .venv\Scripts\python.exe frontend/tests/run_live_browser.py
No model downloads, business DB migrations, container recreation or global cleanup.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import UUID, uuid4


def main():
    if os.getenv("KNOWFLOW_M53_LIVE")!="1":
        raise RuntimeError("Explicit real infrastructure test opt-in required")
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    from sqlalchemy import create_engine,text
    from alembic import command
    from alembic.config import Config
    import httpx
    import pymupdf
    from huggingface_hub import try_to_load_from_cache
    from project import config
    from project.db.business_database import get_database_url
    from project.core.version_index import collection_for

    source=get_database_url()
    if (source.host,source.port,source.database)!=("127.0.0.1",5432,"knowflow"):
        raise RuntimeError("Expected the local maintenance instance")
    for port in (8109,3109):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1",port))
    cached=try_to_load_from_cache(config.DENSE_MODEL,"config.json")
    if not isinstance(cached,str) or not (Path(cached).parent/"model.safetensors").is_file():
        raise RuntimeError("Existing embedding cache unavailable; no downloads allowed")
    network=httpx.Client(trust_env=False,timeout=10)
    if config.LLM_MODEL not in [m["name"] for m in network.get("http://127.0.0.1:11434/api/tags").raise_for_status().json()["models"]]:
        raise RuntimeError("Existing Ollama model unavailable")
    qurl="http://127.0.0.1:6333"
    version=network.get(qurl).raise_for_status().json()["version"]
    if version!="1.19.2":raise RuntimeError("Unexpected Qdrant version")
    def collections():
        names={c["name"] for c in network.get(qurl+"/collections").raise_for_status().json()["result"]["collections"]}
        return {name:network.get(qurl+"/collections/"+name).raise_for_status().json()["result"]["points_count"] for name in sorted(names)}
    before_collections=collections()
    admin=create_engine(source.set(database="postgres"),isolation_level="AUTOCOMMIT",hide_parameters=True)
    business=create_engine(source,hide_parameters=True)
    tables=("users","knowledge_bases","documents","document_versions","document_parse_stages")
    def business_snapshot():
        with business.connect() as connection:
            if connection.scalar(text("SELECT current_database()"))!="knowflow":raise RuntimeError("Unexpected business identity")
            result={"revision":connection.scalar(text("SELECT version_num FROM alembic_version"))}
            for table in tables:
                rows=list(connection.scalars(text(f"SELECT row_to_json(r)::text FROM {table} r ORDER BY row_to_json(r)::text")))
                result[table]={"rows":len(rows),"sha256":hashlib.sha256("\n".join(rows).encode()).hexdigest()}
            return result
    before=business_snapshot()
    if before["revision"]!="c842a9f713e0":raise RuntimeError("Unexpected existing development revision")
    identity=uuid4().hex
    name="knowflow_test_m53_"+identity
    run=root/".t"/"m53"/identity[:12]
    run.mkdir(parents=True,exist_ok=False)
    upload_root=run/"uploads"
    pdf=pymupdf.open()
    for words in ("Apollo project launched on 2024-04-15.","","Apollo budget is 42 credits."):
        page=pdf.new_page()
        if words:page.insert_text((72,72),words)
    pdf.save(run/"public-demo.pdf"); pdf.close()
    report={"run":str(run),"database":name,"postgres":"18.6","qdrant":version,
            "ollama_model":config.LLM_MODEL,"business_before":before,"collections_before":before_collections,
            "mock":False,"model_downloads":False,"cleanup":{},"browser_exit_code":None}
    created=False
    test_engine=None
    process=None
    exit_code=1
    try:
        with admin.connect() as connection:
            db,user=connection.execute(text("SELECT current_database(),current_user")).one()
            if db!="postgres" or user!=source.username:raise RuntimeError("Unexpected maintenance identity")
            pg=connection.scalar(text("SELECT version()"))
            if not pg.startswith("PostgreSQL 18.6"):raise RuntimeError("Unexpected PostgreSQL version")
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created=True
        test_url=source.set(database=name)
        test_engine=create_engine(test_url,hide_parameters=True)
        with test_engine.connect() as connection:
            if connection.scalar(text("SELECT current_database()"))!=name:raise RuntimeError("Wrong disposable DB")
        alembic=Config(str(root/"alembic.ini"))
        alembic.set_main_option("script_location",str(root/"migrations"))
        with patch("project.db.business_database.get_database_url",return_value=test_url):
            command.upgrade(alembic,"head")
        env=os.environ.copy()
        env.update(KNOWFLOW_M53_LIVE="1",KNOWFLOW_M53_DB=name,KNOWFLOW_M53_RUN_ROOT=str(run),
            KNOWFLOW_UPLOAD_ROOT=str(upload_root),KNOWFLOW_JWT_SECRET=secrets.token_hex(32),
            KNOWFLOW_QDRANT_URL=qurl,NO_PROXY="127.0.0.1,localhost",NEXT_TELEMETRY_DISABLED="1")
        print("Created owned database "+name+"; starting real cached-model backend on 8109",flush=True)
        process=subprocess.Popen([sys.executable,str(root/"frontend/tests/live_backend.py")],cwd=root,env=env)
        deadline=time.monotonic()+180
        while True:
            if process.poll() is not None:raise RuntimeError("Owned test backend startup failed")
            try:
                if network.get("http://127.0.0.1:8109/health").status_code==200:break
            except httpx.HTTPError:pass
            if time.monotonic()>deadline:raise RuntimeError("Owned cached-model startup timed out")
            time.sleep(.25)
        print("Real FastAPI/PostgreSQL/Qwen/Qdrant backend ready; running browser tests",flush=True)
        result=subprocess.run(["npx.cmd","--no-install","playwright","test","--config","playwright.live.config.ts"],
                              cwd=root/"frontend",env=env,timeout=1900)
        exit_code=result.returncode
        report["browser_exit_code"]=exit_code
        if (run/"backend-metrics.json").exists():report["backend_metrics"]=json.loads((run/"backend-metrics.json").read_text())
        if (run/"browser-summary.jsonl").exists():
            report["browser_checks"]=[json.loads(line) for line in (run/"browser-summary.jsonl").read_text().splitlines()]
        if exit_code==0 and (report.get("backend_metrics",{}).get("llm_completed",0)<6 or
                "insufficient_evidence" not in report.get("backend_metrics",{}).get("model_statuses",[])):
            raise RuntimeError("Actual LLM answer/refusal evidence is missing")
    except Exception as error:
        report["failure_type"]=type(error).__name__
        exit_code=1
        print("Live verification failed safely: "+type(error).__name__,flush=True)
    finally:
        stopped=True
        if process is not None:
            (run/"shutdown").touch()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:stopped=False
        report["cleanup"]["owned_backend_stopped"]=stopped
        cleanup_errors=[]
        if created and stopped:
            try:
                with test_engine.connect() as connection:
                    if connection.scalar(text("SELECT current_database()"))!=name:raise RuntimeError("Cleanup target mismatch")
                    kb_ids=list(connection.scalars(text("SELECT id FROM knowledge_bases")))
                    report["test_row_counts"]={table:connection.scalar(text(f"SELECT count(*) FROM {table}")) for table in tables}
                removed=[]
                for kb_id in kb_ids:
                    kb=UUID(str(kb_id))
                    collection=collection_for(kb)
                    if collection in before_collections:raise RuntimeError("Test collection collided with old data")
                    response=network.get(qurl+"/collections/"+collection)
                    if response.status_code==404:continue
                    metadata=response.raise_for_status().json()["result"]["config"]["metadata"]
                    if metadata.get("knowledge_base_id")!=str(kb) or metadata.get("knowflow_index_schema")!=1:
                        raise RuntimeError("Collection ownership mismatch; preserved")
                    points=network.post(qurl+"/collections/"+collection+"/points/scroll",json={"limit":100,"with_payload":True,"with_vector":False}).raise_for_status().json()["result"]["points"]
                    if any(p["payload"]["metadata"]["knowledge_base_id"]!=str(kb) for p in points):
                        raise RuntimeError("Stored point ownership mismatch")
                    report.setdefault("stored_index",[]).append({"collection":collection,"point_count":len(points),
                        "pages":sorted({p["payload"]["metadata"]["page_number"] for p in points})})
                    network.delete(qurl+"/collections/"+collection).raise_for_status()
                    removed.append(collection)
                report["cleanup"]["removed_owned_collections"]=removed
                test_engine.dispose()
                if not re.fullmatch(r"knowflow_test_m53_[0-9a-f]{32}",name):raise RuntimeError("Unsafe DB cleanup name")
                with admin.connect() as connection:connection.execute(text(f'DROP DATABASE "{name}"'))
                report["cleanup"]["owned_database_removed"]=True
                expected=(run.resolve()/"uploads")
                if upload_root.exists():
                    if upload_root.resolve()!=expected or not upload_root.resolve().is_relative_to(run.resolve()) or any(p.is_symlink() or p.is_junction() for p in upload_root.rglob("*")):
                        raise RuntimeError("Unsafe upload cleanup path; preserved")
                    shutil.rmtree(upload_root)
                report["cleanup"]["owned_uploads_removed"]=True
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        if test_engine is not None:test_engine.dispose()
        try:
            after=business_snapshot()
            after_collections=collections()
            report["business_after"]=after
            report["collections_after"]=after_collections
            report["original_data_unchanged"]=before==after and before_collections==after_collections
            if not report["original_data_unchanged"]:cleanup_errors.append("OriginalStateChanged")
        except Exception as error:cleanup_errors.append(type(error).__name__)
        report["cleanup_errors"]=cleanup_errors
        if cleanup_errors or not stopped:exit_code=1
        report["exit_code"]=exit_code
        (run/"summary.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        business.dispose(); admin.dispose(); network.close()
        print("Live summary retained at "+str(run/"summary.json"),flush=True)
    return exit_code


if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as error:
        print("Live preflight unavailable: "+type(error).__name__,flush=True)
        raise SystemExit(1) from None
