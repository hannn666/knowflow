import os
import glob
if __package__:
    from . import config
else:
    import config
from pathlib import Path
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

class DocumentChunker:
    def __init__(self, *, min_parent_size=None, max_parent_size=None,
                 child_chunk_size=None, child_chunk_overlap=None, headers=None):
        min_parent_size = config.MIN_PARENT_SIZE if min_parent_size is None else min_parent_size
        max_parent_size = config.MAX_PARENT_SIZE if max_parent_size is None else max_parent_size
        child_chunk_size = config.CHILD_CHUNK_SIZE if child_chunk_size is None else child_chunk_size
        child_chunk_overlap = config.CHILD_CHUNK_OVERLAP if child_chunk_overlap is None else child_chunk_overlap
        if min_parent_size <= 0 or max_parent_size < min_parent_size:
            raise ValueError("Parent chunk sizes must be positive and MIN_PARENT_SIZE <= MAX_PARENT_SIZE.")
        if not 0 <= child_chunk_overlap < child_chunk_size:
            raise ValueError("CHILD_CHUNK_OVERLAP must be smaller than CHILD_CHUNK_SIZE.")
        if child_chunk_overlap >= max_parent_size:
            raise ValueError("CHILD_CHUNK_OVERLAP must be smaller than MAX_PARENT_SIZE.")

        self.__parent_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=list(config.HEADERS_TO_SPLIT_ON if headers is None else headers),
            strip_headers=False,
        )
        self.__child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=child_chunk_size, chunk_overlap=child_chunk_overlap,
        )
        self.__min_parent_size = min_parent_size
        self.__max_parent_size = max_parent_size
        self.__child_chunk_overlap = child_chunk_overlap

    @staticmethod
    def __merge_metadata(target, source, prepend=False):
        for key, value in source.items():
            if key not in target:
                target[key] = value
            else:
                first, second = (value, target[key]) if prepend else (target[key], value)
                values = [
                    item.strip()
                    for raw in (first, second)
                    for item in str(raw).split(" -> ")
                    if item.strip()
                ]
                target[key] = " -> ".join(dict.fromkeys(values))

    def create_chunks(self, path_dir=config.MARKDOWN_DIR):
        all_parent_chunks, all_child_chunks = [], []

        for doc_path_str in sorted(glob.glob(os.path.join(path_dir, "*.md"))):
            doc_path = Path(doc_path_str)
            parent_chunks, child_chunks = self.create_chunks_single(doc_path)
            all_parent_chunks.extend(parent_chunks)
            all_child_chunks.extend(child_chunks)
        
        return all_parent_chunks, all_child_chunks

    def create_chunks_single(self, md_path, source_name=None):
        doc_path = Path(md_path)
        source_name = source_name or f"{doc_path.stem}.pdf"
        
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()
        return self.create_chunks_text(content, source_id=doc_path.stem, source_name=source_name)

    def create_chunks_text(self, content, *, source_id, source_name):
        """Reuse the existing algorithm without writing a shared Markdown file."""
        doc_path = Path(source_id)  # ID only; this method performs no filesystem IO.
        parent_chunks = self.__parent_splitter.split_text(content)
        merged_parents = self.__merge_small_parents(parent_chunks)
        split_parents = self.__split_large_parents(merged_parents)
        cleaned_parents = self.__clean_small_chunks(split_parents)
        if any(len(chunk.page_content) > self.__max_parent_size for chunk in cleaned_parents):
            raise ValueError("Parent chunking produced a chunk larger than MAX_PARENT_SIZE.")
        
        all_parent_chunks, all_child_chunks = [], []
        self.__create_child_chunks(
            all_parent_chunks,
            all_child_chunks,
            cleaned_parents,
            doc_path,
            source_name,
        )
        return all_parent_chunks, all_child_chunks

    def __merge_small_parents(self, chunks):
        if not chunks:
            return []
        
        merged, current = [], None
        
        for chunk in chunks:
            if current is None:
                current = chunk
            else:
                current.page_content += "\n\n" + chunk.page_content
                self.__merge_metadata(current.metadata, chunk.metadata)

            if len(current.page_content) >= self.__min_parent_size:
                merged.append(current)
                current = None
        
        if current:
            if merged:
                merged[-1].page_content += "\n\n" + current.page_content
                self.__merge_metadata(merged[-1].metadata, current.metadata)
            else:
                merged.append(current)
        
        return merged

    def __split_large_parents(self, chunks):
        split_chunks = []
        
        for chunk in chunks:
            if len(chunk.page_content) <= self.__max_parent_size:
                split_chunks.append(chunk)
            else:
                splitter = RecursiveCharacterTextSplitter(
                    chunk_size=self.__max_parent_size,
                    chunk_overlap=self.__child_chunk_overlap
                )
                sub_chunks = splitter.split_documents([chunk])
                split_chunks.extend(sub_chunks)
        
        return split_chunks

    def __rebalance_pair(self, first, second):
        combined = first.page_content.rstrip() + "\n\n" + second.page_content.lstrip()
        lower = max(1, len(combined) - self.__max_parent_size)
        upper = min(self.__max_parent_size, len(combined) - 1)
        if len(combined) >= 2 * self.__min_parent_size:
            lower = max(lower, self.__min_parent_size)
            upper = min(upper, len(combined) - self.__min_parent_size)
        preferred = min(max(len(combined) // 2, lower), upper)

        split_at = preferred
        for separator in ("\n\n", "\n", " "):
            before = combined.rfind(separator, lower, preferred + 1)
            after = combined.find(separator, preferred, upper + 1)
            if before >= lower:
                split_at = before
                break
            if after != -1:
                split_at = after
                break

        left_text = combined[:split_at].rstrip()
        right_text = combined[split_at:].lstrip()
        if len(combined) >= 2 * self.__min_parent_size and (
            len(left_text) < self.__min_parent_size
            or len(right_text) < self.__min_parent_size
        ):
            split_at = preferred
            left_text, right_text = combined[:split_at], combined[split_at:]
        if not left_text or not right_text:
            return first, second

        metadata = dict(first.metadata)
        self.__merge_metadata(metadata, second.metadata)
        first.page_content, first.metadata = left_text, dict(metadata)
        second.page_content, second.metadata = right_text, dict(metadata)
        return first, second

    def __clean_small_chunks(self, chunks):
        cleaned = []
        
        for i, chunk in enumerate(chunks):
            if len(chunk.page_content) < self.__min_parent_size:
                if cleaned and len(cleaned[-1].page_content) + 2 + len(chunk.page_content) <= self.__max_parent_size:
                    cleaned[-1].page_content += "\n\n" + chunk.page_content
                    self.__merge_metadata(cleaned[-1].metadata, chunk.metadata)
                elif (
                    i < len(chunks) - 1
                    and len(chunk.page_content) + 2 + len(chunks[i + 1].page_content) <= self.__max_parent_size
                ):
                    chunks[i + 1].page_content = chunk.page_content + "\n\n" + chunks[i + 1].page_content
                    self.__merge_metadata(chunks[i + 1].metadata, chunk.metadata, prepend=True)
                else:
                    cleaned.append(chunk)
            else:
                cleaned.append(chunk)

        for i, chunk in enumerate(cleaned):
            if len(chunk.page_content) >= self.__min_parent_size or len(cleaned) == 1:
                continue
            if i < len(cleaned) - 1:
                cleaned[i], cleaned[i + 1] = self.__rebalance_pair(chunk, cleaned[i + 1])
            else:
                cleaned[i - 1], cleaned[i] = self.__rebalance_pair(cleaned[i - 1], chunk)
        
        return cleaned

    def __create_child_chunks(self, all_parent_pairs, all_child_chunks, parent_chunks, doc_path, source_name):
        for i, p_chunk in enumerate(parent_chunks):
            parent_id = f"{doc_path.stem}_p{i}"
            p_chunk.metadata.update({"source": source_name, "parent_id": parent_id})
            
            all_parent_pairs.append((parent_id, p_chunk))
            all_child_chunks.extend(self.__child_splitter.split_documents([p_chunk]))
