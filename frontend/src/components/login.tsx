"use client";
import Link from "next/link";
import { useRef, useState, useSyncExternalStore } from "react";
import { api } from "@/lib/client";
const subscribe = () => () => {};
export default function Login() {
  const ready = useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      await api("auth/login", {
        email: String(data.get("email")).trim(),
        password: String(data.get("password")),
      });
      window.location.replace("/knowledge-bases");
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败，请重试");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <section className="login-story">
        <Link className="brand" href="/" prefetch={false}>
          KnowFlow<span>知识有出处</span>
        </Link>
        <div>
          <span className="eyebrow">YOUR KNOWLEDGE WORKSPACE</span>
          <h1>
            让知识，
            <br />
            有一个清晰的归处。
          </h1>
          <p>
            从个人知识库开始，整理文档与可信证据。
            <br />
            你的空间，由你掌握。
          </p>
        </div>
        <p className="subtle">独立空间 · 文档来源 · 可信证据</p>
      </section>
      <section className="login-form">
        <div className="login-card">
          <span className="eyebrow">欢迎回来</span>
          <h2>登录 KnowFlow</h2>
          <p className="subtle">使用已有账户，进入你的知识库工作台。</p>
          <form method="post" action="/api/auth/login" onSubmit={submit}>
            <label htmlFor="email">邮箱</label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              required
              maxLength={320}
              placeholder="you@example.com"
              disabled={busy || !ready}
            />
            <label htmlFor="password">密码</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              maxLength={128}
              disabled={busy || !ready}
            />
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            <button className="button" disabled={busy || !ready}>
              {busy ? "正在登录…" : "进入工作台 →"}
            </button>
          </form>
          <p className="footnote">
            登录凭证有效期为15分钟。过期后请重新登录。
            <br />
            本阶段使用已有账户，尚未提供注册页面。
          </p>
        </div>
      </section>
    </main>
  );
}
