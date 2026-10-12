"""Synchronous single-version MVP: bounded concurrency, checked extractive QA.

A server-authenticated owner is mandatory. No tools, history or readiness writes.
The per-process admission slot is held until work actually finishes; disconnects
cannot cause a timed-out worker to be replaced while it still runs.
"""
from contextlib import contextmanager
from dataclasses import asdict
from functools import lru_cache
import json
import math
import re
from threading import BoundedSemaphore, Lock

from project.api.document_chunking_service import generate_document_version_chunks, read_document_version_chunks
from project.api.document_indexing_service import index_document_version
from project.api.document_parsing_service import get_authorized_parse_source
from project.api.document_retrieval_service import retrieve_document_version
from project.api.grounded_schemas import AskResponse, Citation, IndexResponse
from project.core.grounded_llm import LLMUnavailable, get_grounded_llm, bounded_input, PromptBudgetExceeded
from project.core.index_models import IndexingError

MAX_CHILDREN = 512
MAX_INDEX_CHARACTERS = 256000
MAX_EVIDENCE = 3
MAX_CONTEXT_CHARACTERS = 3000
MIN_SCORE = 0.55
STOP = set("what when where who why how is are was were the a an of to in on and or did does do tell me about please according document page text information give answer start started".split())
REFUSAL = "Insufficient evidence in this document version to answer the question."


class QABusy(Exception):
    pass


class QAInvalidOutput(Exception):
    pass


def anchors(text):
    text=text.casefold()
    words={w for w in re.findall(r"[a-z0-9]+",text) if len(w)>=3 and w not in STOP}
    for run in re.findall(r"[\u4e00-\u9fff]+",text):
        words.update(run[i:i+2] for i in range(len(run)-1))
    return words


def admitted_evidence(question, result):
    terms=anchors(question)
    if not terms:
        return ()
    candidates=[]
    for item in result.evidence:
        matches=sum(term in item.child.text.casefold() for term in terms)
        if math.isfinite(item.score) and item.score>=MIN_SCORE and matches>=min(2,len(terms)):
            candidates.append(item)
    selected=[]
    total=0
    # Greedy, score-ordered admission of complete excerpts. Use the exact final
    # serializer so question, system prompt, labels and JSON escaping all count.
    for item in sorted(candidates, key=lambda hit: hit.score, reverse=True):
        text=item.child.text
        if total+len(text)>MAX_CONTEXT_CHARACTERS:
            continue
        proposed=selected+[item]
        inputs=[{"evidence_id":f"E{i}","text":hit.child.text}
                for i,hit in enumerate(proposed,1)]
        try:
            bounded_input(question,inputs)
        except PromptBudgetExceeded:
            continue
        selected.append(item)
        total+=len(text)
        if len(selected)==MAX_EVIDENCE:
            break
    if candidates and not selected:
        # Capacity failure is not evidence that the document cannot answer.
        raise PromptBudgetExceeded
    return tuple(selected)


def checked_quotes(raw, allowed):
    try:
        if not isinstance(raw,str) or len(raw)>8000:
            raise ValueError
        value=json.loads(raw)
        if not isinstance(value,dict) or set(value)!={"status","quotes"}:
            raise ValueError
        if value["status"]=="insufficient_evidence" and value["quotes"]==[]:
            return []
        quotes=value["quotes"]
        if value["status"]!="answered" or not isinstance(quotes,list) or not 1<=len(quotes)<=3:
            raise ValueError
        seen=set()
        for quote in quotes:
            if not isinstance(quote,dict) or set(quote)!={"evidence_id","text"}:
                raise ValueError
            identity=quote["evidence_id"]
            text=quote["text"]
            if (not isinstance(identity,str) or identity not in allowed or identity in seen
                    or not isinstance(text,str) or not 8<=len(text)<=300 or not text.strip()
                    or text not in allowed[identity].child.text):
                raise ValueError
            seen.add(identity)
        return quotes
    except (ValueError,TypeError,KeyError):
        raise QAInvalidOutput from None


class GroundedService:
    def __init__(self, *, index=None, encoder=None, spec=None, llm=None):
        self.index=index
        self.encoder=encoder
        self.spec=spec
        self.llm=llm
        self._capacity=BoundedSemaphore(1)

    @contextmanager
    def slot(self):
        if not self._capacity.acquire(blocking=False):
            raise QABusy
        try:
            yield
        finally:
            self._capacity.release()

    def options(self):
        return {"index":self.index,"encoder":self.encoder,"spec":self.spec}

    def index_version(self, session, owner, kb, document, version, storage):
        get_authorized_parse_source(session,owner,kb,document,version)
        with self.slot():
            chunks=generate_document_version_chunks(session,owner,kb,document,version,storage)
            if len(chunks.children)>MAX_CHILDREN or sum(len(c.text) for c in chunks.children)>MAX_INDEX_CHARACTERS:
                raise IndexingError("index_resource_limit")
            receipt=index_document_version(session,owner,kb,document,version,storage,**self.options())
            return IndexResponse(**asdict(receipt.source), point_count=receipt.point_count,
                parent_count=receipt.parent_count,parse_attempt_id=receipt.parse_attempt_id,
                parse_sha256=receipt.parse_sha256,chunking_fingerprint=receipt.chunking_fingerprint,
                embedding_fingerprint=receipt.embedding.fingerprint)

    def retrieve(self, session, owner, kb, document, version, storage, question):
        return retrieve_document_version(session,owner,kb,document,version,storage,question,limit=5,**self.options())

    def ask(self, session, owner, kb, document, version, storage, question):
        get_authorized_parse_source(session,owner,kb,document,version)
        with self.slot():
            chunks=read_document_version_chunks(session,owner,kb,document,version,storage)
            if len(chunks.children)>MAX_CHILDREN or sum(len(c.text) for c in chunks.children)>MAX_INDEX_CHARACTERS:
                raise IndexingError("index_resource_limit")
            if not chunks.children:
                return AskResponse(status="insufficient_evidence",answer=REFUSAL,citations=[])
            result=self.retrieve(session,owner,kb,document,version,storage,question)
            selected=admitted_evidence(question,result)
            if not selected:
                return AskResponse(status="insufficient_evidence",answer=REFUSAL,citations=[])
            allowed={f"E{i}":item for i,item in enumerate(selected,1)}
            inputs=[{"evidence_id":k,"text":v.child.text} for k,v in allowed.items()]
            bounded_input(question,inputs)
            llm=self.llm if self.llm is not None else get_grounded_llm()
            try:
                raw=llm.generate(question,inputs)
            except Exception:
                raise LLMUnavailable from None
            quotes=checked_quotes(raw,allowed)
            # LLM can be slow: reauthorize and verify current source/index again.
            fresh=self.retrieve(session,owner,kb,document,version,storage,question)
            if (fresh.binding!=result.binding or fresh.chunking_fingerprint!=result.chunking_fingerprint
                    or fresh.embedding!=result.embedding):
                raise IndexingError("source_changed")
            fresh_by_id={hit.child.chunk_id:hit for hit in fresh.evidence}
            for quote in quotes:
                prior=allowed[quote["evidence_id"]]
                current=fresh_by_id.get(prior.child.chunk_id)
                if current is None or current.child!=prior.child or current.parent!=prior.parent:
                    raise IndexingError("source_changed")
            if not quotes:
                return AskResponse(status="insufficient_evidence",answer=REFUSAL,citations=[])
            citations=[]
            for quote in quotes:
                c=allowed[quote["evidence_id"]].child
                citations.append(Citation(knowledge_base_id=c.knowledge_base_id,document_id=c.document_id,
                    document_version_id=c.document_version_id,original_filename=c.original_filename,
                    page_number=c.page_number,chunk_id=c.chunk_id,parent_id=c.parent_id,evidence_text=quote["text"]))
            # No unchecked free-form LLM prose can become a trusted answer.
            return AskResponse(status="answered",answer="\n".join(q["text"] for q in quotes),citations=citations)


_SERVICE_LOCK = Lock()


@lru_cache(maxsize=1)
def _cached_service():
    return GroundedService()


def get_grounded_service():
    with _SERVICE_LOCK:
        return _cached_service()
