"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ensureSession, sessionNavigation } from "@/lib/session-client";
import { safeReturn } from "@/lib/session-contracts";
export default function RenewSession({ returnTo }: { returnTo: string }) {
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    ensureSession().then(() => { if (active) window.location.replace(safeReturn(returnTo)); })
      .catch(e => { if (active) { setError(e instanceof Error ? e.message : "暂时无法恢复会话"); sessionNavigation(e); } });
    return () => { active = false; };
  }, [returnTo, attempt]);
  return <main className="shell"><section className="panel">
    <h1>正在恢复登录会话</h1>
    {error ? <><p className="error" role="alert">{error}</p>
      <button className="button" onClick={() => { setError(""); setAttempt(v => v + 1); }}>重新检查</button>
      <Link href="/login" prefetch={false}>重新登录</Link></> : <p role="status">请稍候，正在安全地检查会话。</p>}
  </section></main>;
}
