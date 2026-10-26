"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ClientError } from "@/lib/client";
import GroundedQuestion from "./grounded-question";
import type { KnowledgeBase } from "@/lib/contracts";
import type { DocumentItem, DocumentVersion, IndexReceipt, ParseStage } from "@/lib/document-contracts";

import { watchSession } from "@/lib/session-client";
import type { BrowserSession } from "@/lib/session-contracts";

const SIZE = 10 * 1024 * 1024;
const pageSize = 50;
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const versionState = (value: DocumentVersion["status"]) =>
  ({ pending: "原文件已保存", processing: "整体处理中", ready: "整体就绪", failed: "整体处理失败" })[value];
const parseState = (value: ParseStage | null) => {
  if (!value || value.parse_status === "not_started") return "尚未解析";
  return ({ running: "解析正在执行", succeeded: "解析成功", failed: "解析失败" })[value.parse_status];
};
export default function DocumentsWorkspace({ knowledgeBase, session }: {
  knowledgeBase: KnowledgeBase; session: BrowserSession;
}) {
  const user = session.user;
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [documentOffset, setDocumentOffset] = useState(0);
  const [versions, setVersions] = useState<DocumentVersion[]>([]);
  const [versionOffset, setVersionOffset] = useState(0);
  const [document, setDocument] = useState<DocumentItem | null>(null);
  const [version, setVersion] = useState<DocumentVersion | null>(null);
  const [stage, setStage] = useState<ParseStage | null>(null);
  const [verified, setVerified] = useState<IndexReceipt | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState<"upload" | "parse" | "index" | null>(null);
  const [qaPending, setQaPending] = useState<string | null>(null);
  const processing = !!busy || qaPending !== null;
  const qaBusy = useCallback((identity: string, pending: boolean) => {
    setQaPending(previous => pending ? identity : previous === identity ? null : previous);
  },[]);
  const [loading, setLoading] = useState(true);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const lock = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const selectedDocument = useRef<string | null>(null);
  const selectedVersion = useRef<string | null>(null);
  const kb = knowledgeBase.id;
  const path = (doc: string, ver: string) => `knowledge-bases/${kb}/documents/${doc}/versions/${ver}`;
  const showFailure = (e: unknown) => {
    setVerified(null);
    if (e instanceof ClientError && e.status === 504)
      setNotice("请求已结束但结果尚不确定。请刷新列表或解析状态，确认后再决定是否重试；本页不会将它显示为成功。");
    setError(e instanceof Error ? e.message : "操作暂不可用，请稍后重试");
  };
  const loadDocuments = useCallback(async (offset: number) => {
    setLoading(true); setError("");
    try {
      const result = await api<DocumentItem[]>(`knowledge-bases/${kb}/documents?offset=${offset}`, undefined, session);
      setDocuments(result); setDocumentOffset(offset);
      return result;
    } catch (e) { showFailure(e); return null; }
    finally { setLoading(false); }
  }, [kb, session]);
  const refreshStage = async (doc: string, ver: string) => {
    try {
      const result = await api<ParseStage>(path(doc,ver)+"/parse", undefined, session);
      if (selectedVersion.current === ver) setStage(result);
    } catch (e) { showFailure(e); }
  };
  const chooseVersion = (item: DocumentVersion) => {
    selectedVersion.current = item.document_version_id;
    setVersion(item); setStage(null); setVerified(null); setNotice(""); setError("");
    void refreshStage(item.document_id,item.document_version_id);
  };
  const openDocument = async (item: DocumentItem, offset = 0, prefer?: string) => {
    if (lock.current) return;
    selectedDocument.current = item.id;
    selectedVersion.current = null;
    setDocument(item); setVersions([]); setVersion(null); setStage(null); setVerified(null);
    setLoadingVersions(true); setError(""); setNotice("");
    try {
      const result = await api<DocumentVersion[]>(`knowledge-bases/${kb}/documents/${item.id}/versions?offset=${offset}`, undefined, session);
      if (selectedDocument.current !== item.id) return;
      setVersions(result); setVersionOffset(offset);
      const chosen = result.find(v => v.document_version_id === prefer) ?? result[0];
      if (chosen) chooseVersion(chosen);
    } catch (e) { showFailure(e); }
    finally { if (selectedDocument.current === item.id) setLoadingVersions(false); }
  };
  useEffect(() => {
    const initial = setTimeout(() => void loadDocuments(0), 0);
    const stop = watchSession(session, e => {
      setDocuments([]); setDocument(null); setVersion(null); setVerified(null);
      setError(e instanceof Error ? e.message : "Session unavailable");
    });
    return () => { clearTimeout(initial); stop(); };
  }, [loadDocuments, session]);
  const upload = async (event: React.FormEvent) => {
    event.preventDefault();
    if (lock.current || qaPending !== null || !file) return;
    if (file.size > SIZE || !file.name.toLowerCase().endsWith(".pdf")) {
      setError("仅支持不超过 10 MiB 的 PDF 文件"); return;
    }
    lock.current = true; setBusy("upload"); setError(""); setNotice("");
    try {
      const result = await api<DocumentVersion>(`knowledge-bases/${kb}/documents`, undefined, session,
        { file, timeoutMs: 60000 });
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      const list = await loadDocuments(0);
      const item = list?.find(d => d.id === result.document_id);
      if (item) {
        // Selection occurs after the upload lock is released below.
        setTimeout(() => void openDocument(item,0,result.document_version_id),0);
      }
      setNotice("PDF 已保存。解析和索引仍需分别触发。");
    } catch (e) { showFailure(e); }
    finally { lock.current = false; setBusy(null); }
  };
  const trigger = async (kind: "parse" | "index") => {
    if (lock.current || qaPending !== null || !version) return;
    if (kind === "index" && (stage?.parse_status !== "succeeded" || stage.has_text !== true)) return;
    lock.current = true; setBusy(kind); setError(""); setNotice(""); setVerified(null);
    const doc = version.document_id, ver = version.document_version_id;
    try {
      if (kind === "parse") {
        const result = await api<ParseStage>(path(doc,ver)+"/parse", undefined, session,
          { noBody:true, timeoutMs:85000 });
        if (selectedVersion.current === ver) setStage(result);
        setNotice(result.parse_status === "succeeded" ? "解析结果已保存；还没有建立检索索引。" : "解析请求已返回，请查看阶段状态。");
      } else {
        const result = await api<IndexReceipt>(path(doc,ver)+"/index", undefined, session,
          { noBody:true, timeoutMs:315000 });
        if (selectedVersion.current === ver) setVerified(result);
        setNotice("本次索引写入已验证。刷新页面后需要重新核验，不能仅凭旧回执判断仍有效。");
      }
    } catch (e) { showFailure(e); }
    finally { lock.current = false; setBusy(null); }
  };
  const changePage = (offset: number) => {
    if (lock.current) return;
    selectedDocument.current=null; selectedVersion.current=null;
    setDocument(null); setVersions([]); setVersion(null); setStage(null); setVerified(null);
    void loadDocuments(offset);
  };
  const logout = async () => {
    if (lock.current) return;
    try { await api("auth/logout", {}, session); window.location.replace("/login"); }
    catch (e) { showFailure(e); }
  };
  return <div className="workspace">
    <aside className="sidebar">
      <Link className="brand" href="/knowledge-bases" prefetch={false}>KnowFlow<span>知识有出处</span></Link>
      <nav aria-label="主导航"><Link className="nav-active" href="/knowledge-bases" prefetch={false}>知识库</Link>
        <p className="subtle nav-note">选择文档版本，管理 PDF 并查看有出处的回答。</p></nav>
      <div className="account"><span className="avatar" aria-hidden="true">{user.email[0].toUpperCase()}</span>
        <span className="email">{user.email}</span><button className="text-button" onClick={logout}>退出登录</button></div>
    </aside>
    <main className="workspace-main document-main">
      <header className="workspace-header"><div><Link className="breadcrumb" href="/knowledge-bases" prefetch={false}>← 返回知识库</Link>
        <span className="eyebrow">DOCUMENT WORKSPACE</span><h1>{knowledgeBase.name}</h1>
        <p className="subtle">管理 PDF 原件与版本，按顺序完成解析和索引。</p></div><span className="badge">仅限当前知识库</span></header>
      <section className="panel upload-panel" aria-label="上传 PDF"><div><h2>添加 PDF 文档</h2>
        <p className="subtle">单文件最大 10 MiB。上传只保存原件，不会自动解析或索引。</p></div>
        <form onSubmit={upload}><label htmlFor="document-file">选择 PDF</label>
          <input id="document-file" ref={fileInput} type="file" accept=".pdf,application/pdf" disabled={processing}
            onChange={e => setFile(e.target.files?.[0] ?? null)} />
          <button className="button" disabled={!file || processing}>{busy === "upload" ? "正在上传…" : "上传 PDF"}</button></form></section>
      {error && <div className="error" role="alert">{error}</div>}
      {notice && <div className="notice" role="status">{notice}</div>}
      <div className="document-layout">
        <section aria-label="文档列表"><div className="section-title"><h2>文档</h2>
          <button className="text-button" disabled={!!busy || loading} onClick={() => void loadDocuments(documentOffset)}>刷新列表</button></div>
          {loading ? <div className="panel empty" role="status">正在读取文档…</div> : documents.length === 0 ?
            <div className="panel empty"><h3>还没有 PDF 文档</h3><p className="subtle">选择文件上传后，会在这里看到真实记录。</p></div> :
            <div className="document-list">{documents.map(item => <button key={item.id}
              className={`document-row ${document?.id===item.id ? "selected" : ""}`}
              onClick={() => void openDocument(item)} disabled={!!busy}>
              <strong>{item.latest_version?.original_filename ?? "未命名文档"}</strong>
              <span>上传于 {date(item.created_at)}</span><span>最新版本：{item.latest_version ? versionState(item.latest_version.status) : "无版本"}</span>
              </button>)}</div>}
          <div className="pagination"><button className="text-button" disabled={!documentOffset || !!busy}
            onClick={() => changePage(Math.max(0,documentOffset-pageSize))}>上一页</button>
            <button className="text-button" disabled={documents.length < pageSize || !!busy}
            onClick={() => changePage(documentOffset+pageSize)}>下一页</button></div>
        </section>
        <section className="panel version-panel" aria-label="版本与处理">
          {!document ? <div className="empty"><h2>选择一份文档</h2><p className="subtle">选择左侧文档，查看其版本和处理阶段。</p></div> : <>
            <span className="eyebrow">DOCUMENT VERSIONS</span><h2>{document.latest_version?.original_filename ?? "文档版本"}</h2>
            <p className="subtle">每次上传创建新文档和首个版本；当前页面不支持追加版本。</p>
            {loadingVersions ? <p role="status">正在读取版本…</p> : <div className="version-list">{versions.map(item =>
              <button key={item.document_version_id} className={`version-row ${version?.document_version_id===item.document_version_id ? "selected" : ""}`}
                onClick={() => chooseVersion(item)} disabled={!!busy}>
                <strong>{item.original_filename}</strong><span>{date(item.created_at)}</span>
                <span>整体状态：{item.status} · {versionState(item.status)}</span></button>)}</div>}
            <div className="pagination"><button className="text-button" disabled={!versionOffset || !!busy}
              onClick={() => void openDocument(document,Math.max(0,versionOffset-pageSize))}>上一页</button>
              <button className="text-button" disabled={versions.length < pageSize || !!busy}
              onClick={() => void openDocument(document,versionOffset+pageSize)}>下一页</button></div>
            {version && <div className="stage-panel"><h3>当前版本处理</h3>
              <p><strong>原件：</strong>{version.original_filename}</p>
              <p><strong>整体状态：</strong>{version.status}。上传后的 pending 只表示文件已保存。</p>
              <p><strong>解析阶段：</strong>{parseState(stage)}
                {stage?.parse_status === "succeeded" ? ` · ${stage.page_count} 页 · ${stage.has_text ? "有可提取文本" : "无可提取文本"}` : ""}</p>
              {stage?.parse_status === "failed" && <p className="subtle">解析未完成；可核对文件后重试。错误码：{stage.error_code ?? "未知"}</p>}
              <p><strong>索引：</strong>{verified?.document_version_id === version.document_version_id ?
                `本次请求已验证 ${verified.point_count} 个检索块（仅当前页面回执）` : "尚无本页有效核验回执"}</p>
              <div className="stage-actions"><button className="text-button" disabled={!!busy}
                onClick={() => void refreshStage(version.document_id,version.document_version_id)}>刷新解析状态</button>
                <button className="button" disabled={processing || stage?.parse_status === "running"}
                  onClick={() => void trigger("parse")}>{busy === "parse" ? "正在解析…" : "开始解析"}</button>
                <button className="button secondary" disabled={processing || stage?.parse_status !== "succeeded" || stage.has_text !== true}
                  onClick={() => void trigger("index")}>{busy === "index" ? "正在索引…" : "建立索引"}</button></div>
              {stage?.parse_status !== "succeeded" && <p className="subtle">解析成功后才能建立索引。</p>}
              {stage?.parse_status === "succeeded" && !stage.has_text && <p className="subtle">PDF 没有可提取文本，当前无法建立索引。</p>}
              <p className="identifier">版本 ID：{version.document_version_id}</p>
              <GroundedQuestion key={`${user.id}:${version.document_version_id}:${stage?.attempt_id ?? "unknown"}`}
                version={version} session={session} onBusy={qaBusy}
                externalBusy={!!busy || qaPending !== null} />
            </div>}
          </>}
        </section>
      </div>
      <footer className="workspace-footer">处理是同步请求，可能需要等待。超时意味着结果尚不确定，请先刷新状态；索引回执不会在刷新后伪造恢复。</footer>
    </main>
  </div>;
}
