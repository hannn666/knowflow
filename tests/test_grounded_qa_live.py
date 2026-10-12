"""Opt-in actual socket HTTP + Qdrant + cached embedding; real LLM is explicit."""
from contextlib import contextmanager
import os
import socket
from threading import Thread
import time
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn

from project.api.grounded_service import GroundedService,get_grounded_service
from project.core.grounded_llm import get_grounded_llm
from project.core.index_embeddings import get_index_embeddings
from project.core.index_models import IndexSettings
from project.core.parse_executor import BoundedParseExecutor,get_parse_executor
from project.core.version_index import VersionIndex,collection_for
from test_grounded_qa import ExtractiveTestLLM
from test_parse_stage import stage_context,public_pdf


@contextmanager
def http_server(app):
    sock=socket.socket()
    sock.bind(("127.0.0.1",0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level="warning",lifespan="off"))
    thread=Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started:
            if not thread.is_alive() or time.monotonic()>deadline:
                raise RuntimeError("Isolated test HTTP server did not start")
            time.sleep(.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit=True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "Owned test server must terminate; never touch port 8000"


@pytest.fixture
def live_qa(stage_context):
    if os.getenv("KNOWFLOW_TEST_QA_HTTP")!="1" or os.getenv("KNOWFLOW_TEST_QDRANT")!="1":
        pytest.skip("Explicit real Qdrant and socket HTTP required")
    client,session,storage,_,_,_,_=stage_context
    backend=VersionIndex(IndexSettings(batch_size=2))
    baseline={c.name for c in backend._client.get_collections().collections}
    allocated={}
    def own(kb):
        name=collection_for(UUID(kb))
        if name not in allocated:
            if name in baseline:
                raise RuntimeError("Test cannot reuse a pre-existing collection")
            allocated[name]=kb
    try:
        encoder,spec=get_index_embeddings()
        service=GroundedService(index=backend,encoder=encoder,spec=spec,llm=ExtractiveTestLLM())
        client.app.dependency_overrides[get_grounded_service]=lambda:service
        executor=BoundedParseExecutor(timeout_seconds=30,max_processes=1)
        client.app.dependency_overrides[get_parse_executor]=lambda:executor
        with http_server(client.app) as url:
            with httpx.Client(base_url=url,trust_env=False,timeout=300) as network:
                yield network,session,storage,backend,service,own
    finally:
        for name,kb in allocated.items():
            if backend._client.collection_exists(name):
                metadata=backend._client.get_collection(name).config.metadata
                if metadata.get("knowledge_base_id")!=kb or metadata.get("knowflow_index_schema")!=1:
                    raise RuntimeError("Test collection ownership mismatch; preserve it")
                backend._client.delete_collection(name)
        assert {c.name for c in backend._client.get_collections().collections}==baseline
        backend.close()


def register(network):
    credentials={"email":f"qa-http-{uuid4().hex}@example.com","password":"isolated QA HTTP test password"}
    assert network.post('/auth/register',json=credentials).status_code==201
    r=network.post('/auth/login',json=credentials)
    assert r.status_code==200
    headers={"Authorization":"Bearer "+r.json()["access_token"]}
    kb=network.post('/knowledge-bases',headers=headers,json={"name":"isolated QA HTTP"})
    assert kb.status_code==201
    return headers,kb.json()["id"]


@pytest.mark.parametrize("real_llm",[False,True],ids=["real-http-vector-mock-llm","real-http-vector-real-llm"])
def test_socket_end_to_end_with_citations_and_refusal(live_qa,real_llm):
    if real_llm and os.getenv("KNOWFLOW_TEST_REAL_LLM")!="1":
        pytest.skip("Explicit existing Ollama model inference required")
    network,session,storage,backend,service,own=live_qa
    if real_llm:
        service.llm=get_grounded_llm()
    headers,kb=register(network)
    own(kb)
    upload=network.post(f'/knowledge-bases/{kb}/documents',headers=headers,
        files={"file":("public-http-qa.pdf",public_pdf(("Apollo project launched on 2024-04-15.","","Apollo budget is 42 credits.")),"application/pdf")})
    assert upload.status_code==201
    item=upload.json()
    path=f"/knowledge-bases/{kb}/documents/{item['document_id']}/versions/{item['document_version_id']}"
    parsed=network.post(path+'/parse',headers=headers)
    assert parsed.status_code==200 and parsed.json()['parse_status']=='succeeded'
    first=network.post(path+'/index',headers=headers)
    assert first.status_code==200 and first.json()['point_count']==2
    second=network.post(path+'/index',headers=headers)
    assert second.json()==first.json()
    answered=network.post(path+'/ask',headers=headers,json={"question":"When did the Apollo project launch?"})
    assert answered.status_code==200,answered.text
    assert answered.json()['status']=='answered',answered.json()
    assert '2024-04-15' in answered.json()['answer']
    assert answered.headers['cache-control']=='no-store'
    assert answered.json()['citations'][0]['page_number']==1
    assert answered.json()['citations'][0]['document_version_id']==item['document_version_id']
    refusal=network.post(path+'/ask',headers=headers,json={"question":"What is the oxygen temperature on Jupiter?"})
    assert refusal.status_code==200 and refusal.json()['status']=='insufficient_evidence'
    assert refusal.json()['citations']==[]
    if real_llm:
        # This shares lexical launch anchors; the evidence gives a date, no location.
        # Count actual LLM calls to distinguish model refusal from pre-LLM gate.
        original=service.llm.generate
        calls=[]
        def observed(question,evidence):
            calls.append(True)
            return original(question,evidence)
        service.llm.generate=observed
        try:
            unsupported=network.post(path+'/ask',headers=headers,json={"question":"What is the Apollo project launch location?"})
            assert calls, "A real LLM refusal must actually invoke the model"
            assert unsupported.status_code==200 and unsupported.json()['status']=='insufficient_evidence',unsupported.text
        finally:
            service.llm.generate=original
    other_headers,other_kb=register(network)
    denied=network.post(path+'/ask',headers=other_headers,json={"question":"Apollo project launch?"})
    assert denied.status_code==404 and denied.headers['cache-control']=='no-store'
    assert backend._client.count(collection_for(UUID(kb)),exact=True).count==2
