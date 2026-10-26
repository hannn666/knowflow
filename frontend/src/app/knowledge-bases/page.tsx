import Link from "next/link";
import { redirect } from "next/navigation";
import Dashboard from "@/components/dashboard";
import { SafeError, session as readSession } from "@/lib/backend";
export const dynamic = "force-dynamic";
export default async function Page() {
  let session;
  try {
    session = await readSession();
  } catch (e) {
    if (e instanceof SafeError && e.status === 401)
      redirect("/session/renew?returnTo=%2Fknowledge-bases");
    return (
      <main className="shell">
        <div className="panel">
          <h1>暂时无法连接服务</h1>
          <p>请检查后端是否已启动，然后刷新页面。</p>
          <Link className="button" href="/knowledge-bases">
            重新连接
          </Link>
        </div>
      </main>
    );
  }
  return <Dashboard session={session} />;
}
