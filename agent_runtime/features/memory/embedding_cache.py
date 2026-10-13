"""Project-owned, disposable embedding data; never a memory or permission store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from agent_runtime.state_root import managed_state_base, native_io_path

SEMANTIC_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
# Immutable snapshot present in the project's local HuggingFace model cache.
SEMANTIC_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
CACHE_FORMAT_VERSION = 2


def _encoder_version() -> str:
    packages = []
    for name in ("sentence-transformers", "transformers", "torch", "numpy"):
        try:
            packages.append(f"{name}={version(name)}")
        except PackageNotFoundError:
            packages.append(f"{name}=unavailable")
    return ";".join(packages)


@dataclass(frozen=True)
class EmbeddingConfig:
    model_id: str = SEMANTIC_MODEL_ID
    revision: str = SEMANTIC_MODEL_REVISION
    dimensions: int = 384
    normalize_embeddings: bool = False
    max_seq_length: int = 256
    max_chars: int = 800
    encoder_version: str = field(default_factory=_encoder_version)
    # Bump when text preparation/chunking changes, even if model stays fixed.
    text_version: str = "head-tail-v1/chunk-whitespace-v1/query-v1"

    def __post_init__(self):
        if (
            not self.model_id.strip()
            or not re.fullmatch(r"[a-f0-9]{40,64}", self.revision)
            or any(
                type(n) is not int or n <= 0
                for n in (
                    self.dimensions,
                    self.max_seq_length,
                    self.max_chars,
                )
            )
            or type(self.normalize_embeddings) is not bool
            or not self.encoder_version
            or not self.text_version
        ):
            raise ValueError("invalid embedding configuration: immutable revision required")


def project_embedding_cache_root(project_root: str | Path, state_base=None) -> Path:
    """Use a trusted source project and shared storage, never a run state directory."""
    if not Path(project_root).is_absolute():
        raise ValueError("embedding cache requires an absolute project root")
    return managed_state_base(project_root, state_base) / "caches" / "embeddings-v2"


class EmbeddingCache:
    def __init__(self, root: str | Path, config: EmbeddingConfig):
        if not Path(root).is_absolute():
            raise ValueError("embedding cache requires an absolute cache root")
        self.root = Path(root).resolve()
        self.config = config

    def path(self, text: str) -> Path:
        descriptor = {
            "format": CACHE_FORMAT_VERSION,
            "encoding": asdict(self.config),
            "dtype": "float32",
            "device": "cpu",
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        key = hashlib.sha256(
            json.dumps(
                descriptor,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return self.root / f"{key}.npy"

    def valid(self, vector) -> bool:
        import numpy as np

        return (
            isinstance(vector, np.ndarray)
            and vector.dtype == np.dtype("float32")
            and vector.shape == (self.config.dimensions,)
            and bool(np.isfinite(vector).all())
            and bool(np.any(vector != 0))
        )

    def load(self, text: str):
        """Missing, corrupt and incompatible vectors are cache misses."""
        try:
            import numpy as np

            with native_io_path(self.path(text)).open("rb") as stream:
                vector = np.load(stream, allow_pickle=False)
            return vector if self.valid(vector) else None
        except (OSError, ValueError, EOFError, zipfile.BadZipFile):
            return None

    def save(self, text: str, vector) -> None:
        """Publish one complete file; concurrent writers never share a temporary name."""
        import numpy as np

        if not self.valid(vector):
            raise ValueError("invalid embedding vector")
        logical = self.path(text)
        target = native_io_path(logical)
        temporary = native_io_path(logical.with_name(f".{logical.name}.{uuid.uuid4().hex}.tmp"))
        try:
            native_io_path(self.root).mkdir(mode=0o700, parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                np.save(stream, vector, allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        except OSError:
            pass  # Optional derived data: retain the newly encoded in-memory vector.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
