import { uuid } from "./contracts";

export type Citation = {
  knowledge_base_id: string;
  document_id: string;
  document_version_id: string;
  original_filename: string;
  page_number: number;
  chunk_id: string;
  parent_id: string;
  evidence_text: string;
};
export type GroundedAnswer = {
  status: "answered" | "insufficient_evidence";
  answer: string;
  citations: Citation[];
};
const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const characters = (v: string) => Array.from(v).length;
function unicode(value: string) {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (code >= 0xdc00 && code <= 0xdfff) return false;
  }
  return true;
}
export function validQuestion(value: unknown): value is string {
  return typeof value === "string" && !!value.trim() && unicode(value) &&
    characters(value) <= 1000 && new TextEncoder().encode(value).length <= 2000;
}
export function checkedAnswer(value: unknown, kb: string, doc: string, ver: string): GroundedAnswer | null {
  if (!record(value) || Object.keys(value).sort().join() !== "answer,citations,status" ||
      typeof value.answer !== "string" || !value.answer.trim() || !unicode(value.answer) ||
      characters(value.answer) > 1000 || !Array.isArray(value.citations)) return null;
  if (value.status === "insufficient_evidence") {
    return value.citations.length === 0 ? {status:value.status,answer:value.answer,citations:[]} : null;
  }
  if (value.status !== "answered" || value.citations.length < 1 || value.citations.length > 3) return null;
  const citations: Citation[] = [];
  const seen = new Set<string>();
  for (const c of value.citations) {
    if (!record(c) || c.knowledge_base_id !== kb || c.document_id !== doc || c.document_version_id !== ver ||
        typeof c.original_filename !== "string" || !c.original_filename.trim() || !unicode(c.original_filename) ||
        characters(c.original_filename) > 255 || typeof c.page_number !== "number" ||
        !Number.isSafeInteger(c.page_number) || c.page_number < 1 ||
        typeof c.chunk_id !== "string" || !uuid.test(c.chunk_id) || seen.has(c.chunk_id) ||
        typeof c.parent_id !== "string" || !uuid.test(c.parent_id) ||
        typeof c.evidence_text !== "string" || !c.evidence_text.trim() || !unicode(c.evidence_text) ||
        characters(c.evidence_text) < 8 || characters(c.evidence_text) > 300) return null;
    seen.add(c.chunk_id);
    citations.push({knowledge_base_id:kb,document_id:doc,document_version_id:ver,
      original_filename:c.original_filename,page_number:c.page_number,
      chunk_id:c.chunk_id,parent_id:c.parent_id,evidence_text:c.evidence_text});
  }
  // FastAPI's current extractive contract joins exactly these checked excerpts.
  if (citations.some(c => c.original_filename !== citations[0].original_filename) ||
      value.answer !== citations.map(c => c.evidence_text).join("\n")) return null;
  return {status:"answered",answer:value.answer,citations};
}
