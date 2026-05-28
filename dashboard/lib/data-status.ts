/**
 * Loader for the data-corpus snapshot written by
 * `scripts/07_status_snapshot.py` → `data/_status.json`.
 *
 * The dashboard server-component reads this file via `fs/promises` — no
 * Python, no ChromaDB driver, no DB query needed. The pipeline regenerates
 * the snapshot as its final stage; manual refresh:
 *     .venv/bin/python scripts/07_status_snapshot.py
 */
import { promises as fs } from "fs";
import path from "path";

export type ChatSection = {
  total_messages: number;
  content_messages: number;
  service_messages: number;
  first_message: string | null;
  last_message: string | null;
  topics_top: [string, number][];
  topics_total: number;
  source_file_mtime: string | null;
};

export type PdfItem = {
  file: string;
  pages: number | null;
  chars: number;
  language?: string | null;
};

export type PdfsSection = {
  native: {
    files: number;
    non_empty: number;
    total_chars: number;
    items: PdfItem[];
    summary_mtime: string | null;
  };
  ocr: {
    files: number;
    total_chars: number;
    photos_ocr_count: number;
    photos_total_chars: number;
    items: PdfItem[];
    summary_mtime: string | null;
  };
};

export type EnsiaSection = {
  pages: number;
  total_chars: number;
  by_language: Record<string, number>;
  by_kind: Record<string, number>;
  last_modified_seen: string | null;
  summary_mtime: string | null;
};

export type V2vSection = {
  pages: number;
  total_chars: number;
  last_modified_seen: string | null;
  summary_mtime: string | null;
};

export type ChatLinkHost = {
  host: string;
  pages: number;
  chars: number;
  last_fetched: string | null;
  backend: string | null;
};

export type ChatLinksSection = {
  total_urls: number;
  total_hosts: number;
  total_chars: number;
  generated_at: string | null;
  hosts: ChatLinkHost[];
  manifest_mtime: string | null;
};

export type IndexSection = {
  built: boolean;
  error?: string;
  total_chunks?: number;
  by_source?: Record<string, number>;
  by_external_site?: Record<string, number>;
  enriched_chat_chunks?: number;
  chat_chunks_total?: number;
  index_mtime?: string | null;
};

export type DataStatus = {
  generated_at: string;
  chat: ChatSection;
  pdfs: PdfsSection;
  web: {
    ensia_edu_dz: EnsiaSection;
    v2v_ensia: V2vSection;
    chat_links: ChatLinksSection;
  };
  index: IndexSection;
};

/**
 * The snapshot lives at `<repo>/data/_status.json`. The dashboard runs from
 * `<repo>/dashboard`, so we go up one level.
 */
const SNAPSHOT_PATH = path.resolve(process.cwd(), "..", "data", "_status.json");

export async function loadDataStatus(): Promise<DataStatus | null> {
  try {
    const raw = await fs.readFile(SNAPSHOT_PATH, "utf8");
    return JSON.parse(raw) as DataStatus;
  } catch {
    return null;
  }
}
