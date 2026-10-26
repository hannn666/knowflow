import RenewSession from "@/components/renew-session";
import { safeReturn } from "@/lib/session-contracts";
export const dynamic = "force-dynamic";
export default async function Page({ searchParams }: { searchParams: Promise<{ returnTo?: string }> }) {
  return <RenewSession returnTo={safeReturn((await searchParams).returnTo)} />;
}
