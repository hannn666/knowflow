import { uuid } from "./contracts";

export type DocumentVersion = {
  knowledge_base_id: string;
  document_id: string;
  document_version_id: string;
  original_filename: string;
  status: "pending" | "processing" | "ready" | "failed";
  created_at: string;
};
export type DocumentItem = {
  id: string;
  knowledge_base_id: string;
  created_at: string;
  latest_version: DocumentVersion | null;
};
export type ParseStage = {
  knowledge_base_id: string;
  document_id: string;
  document_version_id: string;
  original_filename: string;
  parse_status: "not_started" | "running" | "succeeded" | "failed";
  attempt_id: string | null;
  page_count: number | null;
  has_text: boolean | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
};
export type IndexReceipt = {
  knowledge_base_id: string;
  document_id: string;
  document_version_id: string;
  index_status: "verified";
  point_count: number;
  parent_count: number;
  parse_attempt_id: string;
  parse_sha256: string;
  chunking_fingerprint: string;
  embedding_fingerprint: string;
};
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const date = (v: unknown) => typeof v === "string" && Number.isFinite(Date.parse(v));
const hash = (v: unknown) => typeof v === "string" && /^[0-9a-f]{64}$/i.test(v);
const nonnegative = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
export function isVersion(v: unknown, kb: string, doc: string): v is DocumentVersion {
  if (!record(v) || !uuid.test(kb) || !uuid.test(doc)) return false;
  return v.knowledge_base_id === kb && v.document_id === doc &&
    typeof v.document_version_id === "string" && uuid.test(v.document_version_id) &&
    typeof v.original_filename === "string" && v.original_filename.length > 0 && v.original_filename.length <= 255 &&
    ["pending", "processing", "ready", "failed"].includes(String(v.status)) && date(v.created_at);
}
export function isDocument(v: unknown, kb: string): v is DocumentItem {
  if (!record(v) || typeof v.id !== "string" || !uuid.test(v.id)) return false;
  return v.knowledge_base_id === kb && date(v.created_at) &&
    (v.latest_version === null || isVersion(v.latest_version, kb, v.id));
}
export function isParse(v: unknown, kb: string, doc: string, ver: string): v is ParseStage {
  if (!record(v)) return false;
  return v.knowledge_base_id === kb && v.document_id === doc && v.document_version_id === ver &&
    typeof v.original_filename === "string" &&
    ["not_started", "running", "succeeded", "failed"].includes(String(v.parse_status)) &&
    (v.attempt_id === null || (typeof v.attempt_id === "string" && uuid.test(v.attempt_id))) &&
    (v.page_count === null || nonnegative(v.page_count)) &&
    (v.has_text === null || typeof v.has_text === "boolean") &&
    (v.error_code === null || (typeof v.error_code === "string" && /^[a-z0-9_]{1,64}$/.test(v.error_code))) &&
    (v.error_message === null || typeof v.error_message === "string") &&
    (v.started_at === null || date(v.started_at)) && (v.finished_at === null || date(v.finished_at)) &&
    (v.parse_status !== "succeeded" || (v.page_count !== null && v.page_count > 0 && v.has_text !== null && v.attempt_id !== null)) &&
    (v.parse_status !== "failed" || v.error_code !== null);
}
export function isReceipt(v: unknown, kb: string, doc: string, ver: string): v is IndexReceipt {
  if (!record(v)) return false;
  return v.knowledge_base_id === kb && v.document_id === doc && v.document_version_id === ver &&
    v.index_status === "verified" && nonnegative(v.point_count) && v.point_count > 0 &&
    nonnegative(v.parent_count) && v.parent_count > 0 &&
    typeof v.parse_attempt_id === "string" && uuid.test(v.parse_attempt_id) &&
    hash(v.parse_sha256) && hash(v.chunking_fingerprint) && hash(v.embedding_fingerprint);
}
