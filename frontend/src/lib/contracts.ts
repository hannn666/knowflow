export type User = { id: string; email: string; created_at: string };
export type KnowledgeBase = {
  id: string;
  owner_id: string;
  name: string;
  created_at: string;
};
export const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function isUser(v: unknown): v is User {
  if (!v || typeof v !== "object") return false;
  const x = v as User;
  return (
    typeof x.id === "string" &&
    uuid.test(x.id) &&
    typeof x.email === "string" &&
    typeof x.created_at === "string"
  );
}
export function isKnowledgeBase(v: unknown): v is KnowledgeBase {
  if (!v || typeof v !== "object") return false;
  const x = v as KnowledgeBase;
  return (
    typeof x.id === "string" &&
    uuid.test(x.id) &&
    typeof x.owner_id === "string" &&
    uuid.test(x.owner_id) &&
    typeof x.name === "string" &&
    x.name.length <= 200 &&
    typeof x.created_at === "string"
  );
}
