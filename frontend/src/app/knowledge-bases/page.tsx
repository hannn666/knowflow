import { redirect } from "next/navigation";
import Dashboard from "@/components/dashboard";
import { SafeError, me, token, expiry } from "@/lib/backend";
export const dynamic = "force-dynamic";
export default async function Page() {
  let session;
  try {
    const value = await token();
    session = { user: await me(value), expiresAt: expiry(value) };
  } catch (e) {
    if (e instanceof SafeError && e.status === 401)
      redirect("/login?expired=1");
    return (
      <main className="shell">
        <div className="panel">
          <h1>暂时无法连接服务</h1>
          <p>请检查后端是否已启动，然后刷新页面。</p>
          <a className="button" href="/knowledge-bases">
            重新连接
          </a>
        </div>
      </main>
    );
  }
  return <Dashboard user={session.user} expiresAt={session.expiresAt} />;
}
