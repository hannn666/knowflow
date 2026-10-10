"""Reuse the installed HuggingFace wrapper, exclusively from existing cache."""
from functools import lru_cache
from pathlib import Path
import re
from threading import Lock

from huggingface_hub import try_to_load_from_cache

from project import config
from project.core.index_models import EmbeddingSpec, IndexingError, normalized_vectors

_MODEL_LOCK = Lock()


@lru_cache(maxsize=1)
def _cached_bundle():
    cached = try_to_load_from_cache(config.DENSE_MODEL, "config.json")
    if not isinstance(cached, str):
        raise IndexingError("embedding_cache_missing")
    snapshot = Path(cached).parent
    if (not re.fullmatch("[0-9a-f]{40}", snapshot.name)
            or not all((snapshot / name).is_file() for name in ("model.safetensors", "modules.json", "tokenizer.json"))):
        raise IndexingError("embedding_cache_incomplete")
    try:
        # Local path + local_files_only forbid a fallback model download. No
        # cache moves, global environment changes or upstream RAG settings.
        from langchain_huggingface import HuggingFaceEmbeddings
        encoder = HuggingFaceEmbeddings(
            model_name=str(snapshot), model_kwargs={"local_files_only": True, "device": "cpu"},
            encode_kwargs={"normalize_embeddings": True, "batch_size": 8}, show_progress=False,
        )
        probe = encoder.embed_query("KnowFlow embedding dimension probe")
        spec = EmbeddingSpec(config.DENSE_MODEL, snapshot.name, len(probe))
        normalized_vectors([probe], 1, spec)
        return encoder, spec
    except IndexingError:
        raise
    except Exception:
        raise IndexingError("embedding_initialization_failed") from None


def get_index_embeddings():
    # Prevent concurrent first requests from loading duplicate large models.
    with _MODEL_LOCK:
        return _cached_bundle()
