import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import DocumentsWorkspace from "@/components/documents-workspace";
import { SafeError, kb, token, session as readSession } from "@/lib/backend";
import { uuid } from "@/lib/contracts";
export const dynamic = "force-dynamic";
export default async function Page({ params }: { params: Promise<{ knowledgeBaseId: string }> }) {
  const { knowledgeBaseId } = await params;
  if (!uuid.test(knowledgeBaseId)) notFound();
  let data;
  try {
    const session = await readSession();
    data = { session, knowledgeBase: await kb(`/knowledge-bases/${knowledgeBaseId}`, await token()) };
  } catch (e) {
    if (e instanceof SafeError && e.status === 401) redirect(`/session/renew?returnTo=${encodeURIComponent(`/knowledge-bases/${knowledgeBaseId}`)}`);
    if (e instanceof SafeError && e.status === 404) notFound();
    return <main className="shell"><div className="panel"><h1>暂时无法读取知识库</h1><p>请稍后刷新，或返回知识库列表。</p><Link className="button" href="/knowledge-bases">返回知识库</Link></div></main>;
  }
  return <DocumentsWorkspace knowledgeBase={data.knowledgeBase as import("@/lib/contracts").KnowledgeBase} session={data.session} />;
}
