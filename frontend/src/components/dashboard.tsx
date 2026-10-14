"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/client";
import type { KnowledgeBase, User } from "@/lib/contracts";
export default function Dashboard({
  user,
  expiresAt,
}: {
  user: User;
  expiresAt: number;
}) {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [detail, setDetail] = useState<KnowledgeBase | null>(null);
  const [offset, setOffset] = useState(0);
  const [more, setMore] = useState(false);
  const pending = useRef(false);
  const generation = useRef(0);
  const load = useCallback(
    async (start = 0) => {
      const current = ++generation.current;
      setLoading(true);
      setError("");
      try {
        const result = await api<KnowledgeBase[]>(
          `knowledge-bases?offset=${start}`,
          undefined,
          user.id,
        );
        if (current !== generation.current) return;
        setItems(result);
        setOffset(start);
        setMore(result.length === 50);
      } catch (e) {
        if (current === generation.current)
          setError(e instanceof Error ? e.message : "加载失败");
      } finally {
        if (current === generation.current) setLoading(false);
      }
    },
    [user.id],
  );
  const invalidate = useCallback(() => {
    generation.current++;
  }, []);
  useEffect(() => {
    const initial = setTimeout(() => void load(), 0);
    const timer = setTimeout(
      () => window.location.replace("/login?expired=1"),
      Math.max(0, expiresAt - Date.now()),
    );
    const validate = () => {
      if (document.visibilityState === "visible")
        void api<{ user: User }>("auth/me")
          .then((s) => {
            if (s.user.id !== user.id)
              window.location.replace("/knowledge-bases");
          })
          .catch(() => {
            setItems([]);
            setDetail(null);
            setError("无法确认登录状态，请刷新页面");
          });
    };
    window.addEventListener("pageshow", validate);
    document.addEventListener("visibilitychange", validate);
    return () => {
      clearTimeout(initial);
      clearTimeout(timer);
      invalidate();
      window.removeEventListener("pageshow", validate);
      document.removeEventListener("visibilitychange", validate);
    };
  }, [load, expiresAt, user.id, invalidate]);
  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    pending.current = true;
    setCreating(true);
    setError("");
    try {
      await api("knowledge-bases", { name }, user.id);
      setName("");
      setDetail(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建失败");
    } finally {
      pending.current = false;
      setCreating(false);
    }
  }
  async function logout() {
    if (pending.current) return;
    pending.current = true;
    setError("");
    try {
      await api("auth/logout", {});
      setItems([]);
      window.location.replace("/login");
    } catch {
      setError("退出失败，请重试");
      pending.current = false;
    }
  }
  async function inspect(id: string) {
    setDetail(null);
    setError("");
    try {
      setDetail(
        await api<KnowledgeBase>(`knowledge-bases/${id}`, undefined, user.id),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "无法读取知识库");
    }
  }
  return (
    <div className="workspace">
      <aside className="sidebar">
        <Link className="brand" href="/knowledge-bases">
          KnowFlow<span>知识有出处</span>
        </Link>
        <nav aria-label="主导航">
          <Link className="nav-active" href="/knowledge-bases">
            知识库
          </Link>
          <p className="subtle nav-note">选择知识库管理 PDF；问答将在后续切片开放。</p>
        </nav>
        <div className="account">
          <span className="avatar" aria-hidden="true">
            {user.email[0].toUpperCase()}
          </span>
          <span className="email">{user.email}</span>
          <button className="text-button" onClick={logout}>
            退出登录
          </button>
        </div>
      </aside>
      <main className="workspace-main">
        <header className="workspace-header">
          <div>
            <span className="eyebrow">KNOWLEDGE WORKSPACE</span>
            <h1>我的知识库</h1>
            <p className="subtle">
              为不同主题建立独立空间，从这里开始整理知识。
            </p>
          </div>
          <span className="badge">个人工作台</span>
        </header>
        <section className="panel create-panel">
          <div>
            <h2>建立一个新空间</h2>
            <p className="subtle">用清晰的名称，标记你的学习与研究主题。</p>
          </div>
          <form onSubmit={create}>
            <label className="sr-only" htmlFor="kb-name">
              知识库名称
            </label>
            <input
              id="kb-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={200}
              placeholder="例如：软件工程学习"
              disabled={creating || loading}
            />
            <button
              className="button"
              disabled={creating || loading || !name.trim()}
            >
              {creating ? "正在创建…" : "+ 创建知识库"}
            </button>
          </form>
        </section>
        {error && (
          <div className="error" role="alert">
            {error}{" "}
            <button className="text-button" onClick={() => void load(offset)}>
              重试
            </button>
          </div>
        )}
        <section aria-label="知识库列表" aria-busy={loading}>
          <div className="section-title">
            <h2>知识空间</h2>
            <button
              className="text-button"
              disabled={loading}
              onClick={() => void load(offset)}
            >
              刷新列表
            </button>
          </div>
          {loading ? (
            <div className="panel empty" role="status">
              正在加载你的知识库…
            </div>
          ) : items.length === 0 ? (
            <div className="panel empty">
              <span className="empty-symbol" aria-hidden="true">
                ＋
              </span>
              <h3>第一个知识库，从一个主题开始</h3>
              <p className="subtle">在上方输入名称，创建属于你的知识空间。</p>
            </div>
          ) : (
            <div className="kb-grid">
              {items.map((item) => (
                <div key={item.id} className="kb-card">
                  <button className="kb-card-main" onClick={() => void inspect(item.id)}>
                    <span className="card-label">知识库</span>
                    <h3>{item.name}</h3>
                    <p>创建于 {new Date(item.created_at).toLocaleDateString("zh-CN")}</p>
                    <span className="card-link">查看信息 ↗</span>
                  </button>
                  <Link className="card-direct-link" href={`/knowledge-bases/${item.id}`} prefetch={false}>
                    管理文档 →
                  </Link>
                </div>
              ))}
            </div>
          )}
          <div className="pagination">
            {offset > 0 && (
              <button
                className="text-button"
                onClick={() => void load(Math.max(0, offset - 50))}
              >
                上一页
              </button>
            )}
            {more && (
              <button
                className="text-button"
                onClick={() => void load(offset + 50)}
              >
                下一页
              </button>
            )}
          </div>
        </section>
        {detail && (
          <section className="panel detail" aria-label="知识库信息">
            <button className="text-button" onClick={() => setDetail(null)}>
              收起
            </button>
            <h2>{detail.name}</h2>
            <p className="subtle">上传 PDF，查看版本，并按步骤触发解析与索引。</p>
            <Link className="button detail-link" href={`/knowledge-bases/${detail.id}`} prefetch={false}>进入文档管理 →</Link>
            <p className="identifier">知识库 ID：{detail.id}</p>
          </section>
        )}
        <footer className="workspace-footer">
          内容与操作均来自你的个人账户。问答界面尚未开放。
        </footer>
      </main>
    </div>
  );
}
