"use client";
import { useEffect, useRef, useState } from "react";
import { api, ClientError } from "@/lib/client";
import type { DocumentVersion } from "@/lib/document-contracts";
import { validQuestion, type GroundedAnswer } from "@/lib/grounded-contracts";

export default function GroundedQuestion({ version, userId, externalBusy, onBusy }: {
  version: DocumentVersion; userId: string; externalBusy: boolean;
  onBusy: (versionId: string, pending: boolean) => void;
}) {
  const [question,setQuestion] = useState("");
  const [result,setResult] = useState<GroundedAnswer | null>(null);
  const [submitted,setSubmitted] = useState("");
  const [error,setError] = useState("");
  const [busy,setBusy] = useState(false);
  const lock = useRef(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; },[]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (lock.current || externalBusy) return;
    if (!validQuestion(question)) {
      setError("问题不能为空，最多 1000 字符且 UTF-8 不超过 2000 字节"); return;
    }
    lock.current = true; setBusy(true); setError(""); setResult(null); setSubmitted(question);
    onBusy(version.document_version_id,true);
    try {
      const answer = await api<GroundedAnswer>(
        `knowledge-bases/${version.knowledge_base_id}/documents/${version.document_id}/versions/${version.document_version_id}/ask`,
        {question},userId,{timeoutMs:315000});
      if (!active.current) return;
      const current = await api<{user:{id:string}}>("auth/me");
      if (current.user.id !== userId) { window.location.replace("/knowledge-bases"); return; }
      if (active.current) setResult(answer);
    } catch (e) {
      if (active.current) {
        setResult(null);
        setError(e instanceof ClientError ? e.message : "问答暂不可用，请稍后重试");
      }
    } finally {
      lock.current = false;
      if (active.current) setBusy(false);
      // Keep the parent's admission guard until this request actually returns,
      // even if selecting another version has unmounted this component.
      onBusy(version.document_version_id,false);
    }
  };
  return <section className="qa-panel" aria-label="指定版本问答">
    <span className="eyebrow">DOCUMENT QUESTION</span><h3>向此版本提问</h3>
    <p className="qa-source"><strong>{version.original_filename}</strong>
      <span className="identifier">版本 {version.document_version_id.slice(0,8)}</span></p>
    <p className="subtle">仅支持当前版本的原文摘录式问答。后端会重新核验索引；本页没有回执，也可以尝试提问。</p>
    <form onSubmit={submit} method="post">
      <label htmlFor="version-question">你的问题</label>
      <textarea id="version-question" rows={4} value={question} disabled={busy || externalBusy}
        onChange={event => setQuestion(event.target.value)} placeholder="例如：这个项目何时启动？" />
      <div className="qa-controls"><span className="subtle">{Array.from(question).length}/1000 字符 · {new TextEncoder().encode(question).length}/2000 字节</span>
        <button className="button" disabled={busy || externalBusy || !question.trim()}>
          {busy ? "正在核验与生成…" : "提交问题"}</button></div>
    </form>
    {(busy || externalBusy) && <p className="subtle" role="status">{busy ?
      "正在检索当前版本并等待模型，可能需要几分钟。请勿重复提交。" : "当前还有处理请求在执行，请等待它返回。"}</p>}
    {error && <div className="error" role="alert">{error}</div>}
    {result && <p className="subtle qa-submitted">本次问题：{submitted}</p>}
    {result?.status === "insufficient_evidence" && <div className="qa-refusal" role="status">
      <h4>当前版本证据不足</h4><p>没有足够的文档证据回答这个问题。可以换一个问题，或检查所选版本。</p></div>}
    {result?.status === "answered" && <div className="qa-result" aria-label="问答结果">
      <h4>原文摘录回答</h4><p className="answer-text">{result.answer}</p>
      <h4>来源与证据</h4><div className="citation-list">{result.citations.map((citation,i) =>
        <details className="citation" key={citation.chunk_id} data-document-id={citation.document_id}
          data-version-id={citation.document_version_id} data-chunk-id={citation.chunk_id} data-parent-id={citation.parent_id}>
          <summary><span>{i+1}. {citation.original_filename}</span><strong>第 {citation.page_number} 页</strong>
            <span className="subtle">展开证据原文</span></summary>
          <blockquote>{citation.evidence_text}</blockquote>
        </details>)}</div></div>}
  </section>;
}
