# Phase 6: Document Management & RAG Implementation Plan

> **Location**: `docs/PHASE_6_DOCUMENTS_RAG.md`
> **Branch**: `feature/phase-6-documents-rag`
> **Status**: Ready to implement
> **Dependencies**: Phase 1 (Database) - DONE

---

## Overview

Build a document management system with RAG (Retrieval-Augmented Generation) capabilities. Handle receipts, invoices, quotes, and PDFs. Enable semantic search across documents.

**Key Principle**: Start simple with JSON embeddings in SQLite. Scale to pgvector later if needed.

---

## Existing Database Schema (in db.py)

### Documents Table
```sql
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT,
    document_type TEXT,  -- 'receipt', 'invoice', 'quote', 'contract', 'meeting_notes', etc.
    file_path TEXT,
    file_size INTEGER,
    mime_type TEXT,
    extracted_text TEXT,
    source_type TEXT,    -- 'email', 'upload', 'scan'
    source_reference TEXT,
    tags TEXT,           -- JSON array
    metadata TEXT,       -- JSON object
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### Document Chunks Table
```sql
CREATE TABLE IF NOT EXISTS document_chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT,      -- JSON array of floats
    token_count INTEGER,
    metadata TEXT,       -- JSON: {page_number, section, etc.}
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**Existing db.py functions**:
- `create_document()`
- `get_document()`
- `list_documents()`

---

## Files to Create

### 1. `document_manager.py` (NEW - ~350 lines)

```python
#!/usr/bin/env python3
"""
Document Manager for Project Manager Agent.

Handles document upload, text extraction, chunking, and metadata management.
Supports PDFs, images (with OCR), and text files.
"""

import os
import json
import logging
import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

import db

logger = logging.getLogger(__name__)

# Configuration
DOCS_BASE_DIR = "./pm-docs"
CHUNK_SIZE = 500  # tokens
CHUNK_OVERLAP = 50  # tokens
CHARS_PER_TOKEN = 4  # estimate


# ============================================
# TEXT EXTRACTION
# ============================================

def extract_text_from_pdf(file_path: str) -> str:
    """
    Extract text from a PDF file.

    Uses PyPDF2 if available, otherwise returns placeholder.

    Args:
        file_path: Path to PDF file

    Returns:
        Extracted text
    """
    try:
        import PyPDF2
        text = ""
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() + "\n"
        return text.strip()
    except ImportError:
        logger.warning("PyPDF2 not installed. Install with: pip install PyPDF2")
        return f"[PDF text extraction requires PyPDF2: {file_path}]"
    except Exception as e:
        logger.error(f"Error extracting PDF text: {e}")
        return f"[Error extracting text from {file_path}: {e}]"


def extract_text_from_image(file_path: str) -> str:
    """
    Extract text from an image using OCR.

    Uses pytesseract if available, otherwise returns placeholder.

    Args:
        file_path: Path to image file

    Returns:
        Extracted text
    """
    try:
        import pytesseract
        from PIL import Image
        image = Image.open(file_path)
        text = pytesseract.image_to_string(image)
        return text.strip()
    except ImportError:
        logger.warning("pytesseract/PIL not installed. Install with: pip install pytesseract pillow")
        return f"[OCR requires pytesseract and pillow: {file_path}]"
    except Exception as e:
        logger.error(f"Error extracting image text: {e}")
        return f"[Error extracting text from {file_path}: {e}]"


def extract_text_from_file(file_path: str) -> str:
    """
    Extract text from a file based on its type.

    Args:
        file_path: Path to file

    Returns:
        Extracted text
    """
    mime_type, _ = mimetypes.guess_type(file_path)

    if mime_type == "application/pdf":
        return extract_text_from_pdf(file_path)

    elif mime_type and mime_type.startswith("image/"):
        return extract_text_from_image(file_path)

    elif mime_type and mime_type.startswith("text/"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"[Error reading text file: {e}]"

    else:
        # Try reading as text
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return f"[Unsupported file type: {mime_type or 'unknown'}]"


# ============================================
# CHUNKING
# ============================================

def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP
) -> List[Dict[str, Any]]:
    """
    Split text into overlapping chunks.

    Args:
        text: Text to chunk
        chunk_size: Target size in tokens
        overlap: Overlap between chunks in tokens

    Returns:
        List of chunk dicts with content and metadata
    """
    if not text:
        return []

    # Convert to character counts (approximate)
    char_chunk_size = chunk_size * CHARS_PER_TOKEN
    char_overlap = overlap * CHARS_PER_TOKEN

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        end = start + char_chunk_size

        # Try to break at sentence boundary
        if end < len(text):
            # Look for sentence end near the boundary
            for i in range(min(100, char_chunk_size // 4)):
                check_pos = end - i
                if check_pos > start and text[check_pos] in ".!?\n":
                    end = check_pos + 1
                    break

        chunk_text = text[start:end].strip()

        if chunk_text:
            token_count = len(chunk_text) // CHARS_PER_TOKEN
            chunks.append({
                "chunk_index": chunk_index,
                "content": chunk_text,
                "token_count": token_count,
                "char_start": start,
                "char_end": end
            })
            chunk_index += 1

        # Move start, accounting for overlap
        start = end - char_overlap
        if start >= len(text) - char_overlap:
            break

    return chunks


# ============================================
# DOCUMENT MANAGEMENT
# ============================================

def upload_document(
    file_path: str,
    project_id: str = None,
    task_id: str = None,
    title: str = None,
    document_type: str = None,
    description: str = None,
    source_type: str = "upload",
    tags: List[str] = None,
    extract_text: bool = True,
    generate_chunks: bool = True
) -> Dict[str, Any]:
    """
    Upload and process a document.

    Args:
        file_path: Path to source file
        project_id: Optional project to link to
        task_id: Optional task to link to
        title: Document title (defaults to filename)
        document_type: Type (receipt, invoice, etc.)
        description: Optional description
        source_type: How document was obtained
        tags: List of tags
        extract_text: Whether to extract text
        generate_chunks: Whether to generate chunks for RAG

    Returns:
        Document dict with chunks if generated
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    # Get file info
    file_stat = os.stat(file_path)
    mime_type, _ = mimetypes.guess_type(file_path)
    filename = os.path.basename(file_path)

    # Determine storage path
    if project_id:
        dest_dir = os.path.join(DOCS_BASE_DIR, project_id)
    else:
        dest_dir = os.path.join(DOCS_BASE_DIR, "general")

    os.makedirs(dest_dir, exist_ok=True)

    # Copy file to storage (with unique name if exists)
    dest_path = os.path.join(dest_dir, filename)
    if os.path.exists(dest_path):
        name, ext = os.path.splitext(filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = os.path.join(dest_dir, f"{name}_{timestamp}{ext}")

    # Copy file
    import shutil
    shutil.copy2(file_path, dest_path)

    # Extract text if requested
    extracted_text = None
    if extract_text:
        extracted_text = extract_text_from_file(dest_path)

    # Create document record
    doc = db.create_document(
        project_id=project_id,
        task_id=task_id,
        title=title or filename,
        description=description,
        document_type=document_type or _guess_document_type(filename, mime_type),
        file_path=dest_path,
        file_size=file_stat.st_size,
        mime_type=mime_type,
        extracted_text=extracted_text,
        source_type=source_type,
        tags=tags
    )

    # Generate chunks if requested
    if generate_chunks and extracted_text:
        chunks = create_chunks_for_document(doc["id"], extracted_text)
        doc["chunks_created"] = len(chunks)

    logger.info(f"Uploaded document: {doc['title']} (id={doc['id']})")

    return doc


def create_chunks_for_document(
    document_id: str,
    text: str = None
) -> List[Dict[str, Any]]:
    """
    Create text chunks for a document.

    Args:
        document_id: Document ID
        text: Text to chunk (if None, uses document's extracted_text)

    Returns:
        List of created chunk records
    """
    if text is None:
        doc = db.get_document(document_id)
        if not doc:
            raise ValueError(f"Document not found: {document_id}")
        text = doc.get("extracted_text", "")

    if not text:
        return []

    # Generate chunks
    chunks = chunk_text(text)

    # Store in database
    conn = db.get_connection()
    cursor = conn.cursor()

    created_chunks = []
    for chunk in chunks:
        import uuid
        chunk_id = str(uuid.uuid4())

        cursor.execute("""
            INSERT INTO document_chunks (id, document_id, chunk_index, content, token_count, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            chunk_id,
            document_id,
            chunk["chunk_index"],
            chunk["content"],
            chunk["token_count"],
            json.dumps({"char_start": chunk["char_start"], "char_end": chunk["char_end"]})
        ))

        created_chunks.append({
            "id": chunk_id,
            "document_id": document_id,
            "chunk_index": chunk["chunk_index"],
            "content": chunk["content"],
            "token_count": chunk["token_count"]
        })

    conn.commit()
    conn.close()

    logger.info(f"Created {len(created_chunks)} chunks for document {document_id}")

    return created_chunks


def get_document_with_chunks(document_id: str) -> Optional[Dict[str, Any]]:
    """
    Get a document with all its chunks.

    Args:
        document_id: Document ID

    Returns:
        Document dict with chunks list
    """
    doc = db.get_document(document_id)
    if not doc:
        return None

    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM document_chunks
        WHERE document_id = ?
        ORDER BY chunk_index ASC
    """, (document_id,))

    doc["chunks"] = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return doc


def search_documents(
    query: str = None,
    project_id: str = None,
    document_type: str = None,
    tags: List[str] = None,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """
    Search documents by text, project, type, or tags.

    Args:
        query: Text search query
        project_id: Filter by project
        document_type: Filter by type
        tags: Filter by tags (any match)
        limit: Max results

    Returns:
        List of matching documents
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    sql = "SELECT * FROM documents WHERE 1=1"
    params = []

    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)

    if document_type:
        sql += " AND document_type = ?"
        params.append(document_type)

    if query:
        sql += " AND (title LIKE ? OR description LIKE ? OR extracted_text LIKE ?)"
        like_query = f"%{query}%"
        params.extend([like_query, like_query, like_query])

    if tags:
        # Search in JSON tags array
        tag_conditions = " OR ".join(["tags LIKE ?" for _ in tags])
        sql += f" AND ({tag_conditions})"
        params.extend([f'%"{tag}"%' for tag in tags])

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cursor.execute(sql, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return results


def delete_document(document_id: str) -> bool:
    """
    Delete a document and its chunks.

    Args:
        document_id: Document ID

    Returns:
        True if deleted
    """
    doc = db.get_document(document_id)
    if not doc:
        return False

    # Delete file if exists
    if doc.get("file_path") and os.path.exists(doc["file_path"]):
        try:
            os.remove(doc["file_path"])
        except Exception as e:
            logger.warning(f"Could not delete file {doc['file_path']}: {e}")

    # Delete from database (chunks cascade)
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()

    return deleted


def link_document_to_task(document_id: str, task_id: str) -> bool:
    """
    Link a document to a task.

    Args:
        document_id: Document ID
        task_id: Task ID

    Returns:
        True if updated
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE documents SET task_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task_id, document_id)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


# ============================================
# UTILITY FUNCTIONS
# ============================================

def _guess_document_type(filename: str, mime_type: str) -> str:
    """Guess document type from filename and mime type."""
    filename_lower = filename.lower()

    if "receipt" in filename_lower:
        return "receipt"
    elif "invoice" in filename_lower:
        return "invoice"
    elif "quote" in filename_lower or "proposal" in filename_lower:
        return "quote"
    elif "contract" in filename_lower:
        return "contract"
    elif mime_type == "application/pdf":
        return "pdf"
    elif mime_type and mime_type.startswith("image/"):
        return "image"
    else:
        return "document"


def get_storage_stats() -> Dict[str, Any]:
    """Get storage statistics."""
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as count, SUM(file_size) as total_size FROM documents")
    row = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) as count FROM document_chunks")
    chunk_count = cursor.fetchone()["count"]

    conn.close()

    return {
        "document_count": row["count"],
        "total_size_bytes": row["total_size"] or 0,
        "chunk_count": chunk_count,
        "storage_directory": DOCS_BASE_DIR
    }


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing document_manager...")

    # Test chunking
    test_text = "This is a test. " * 100
    chunks = chunk_text(test_text, chunk_size=50, overlap=10)
    print(f"  Chunked {len(test_text)} chars into {len(chunks)} chunks")
    assert len(chunks) > 1

    # Test document type guessing
    assert _guess_document_type("receipt_2024.pdf", "application/pdf") == "receipt"
    assert _guess_document_type("invoice.pdf", "application/pdf") == "invoice"

    print("All tests passed!")
```

---

### 2. `rag_engine.py` (NEW - ~300 lines)

```python
#!/usr/bin/env python3
"""
RAG Engine for Project Manager Agent.

Provides semantic search over document chunks using embeddings.
Starts with JSON embeddings in SQLite, can scale to pgvector.
"""

import os
import json
import logging
import math
from typing import Dict, List, Any, Optional, Tuple

import db

logger = logging.getLogger(__name__)

# Configuration
EMBEDDING_MODEL = "text-embedding-3-small"  # OpenAI model
EMBEDDING_DIMENSION = 1536  # Default for OpenAI small
USE_LOCAL_EMBEDDINGS = False  # Set True to use sentence-transformers
TOP_K_DEFAULT = 5


# ============================================
# EMBEDDING GENERATION
# ============================================

def generate_embedding_openai(text: str) -> List[float]:
    """
    Generate embedding using OpenAI API.

    Args:
        text: Text to embed

    Returns:
        Embedding vector as list of floats
    """
    try:
        import openai

        # Check for API key
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set. Using zero vector.")
            return [0.0] * EMBEDDING_DIMENSION

        client = openai.OpenAI(api_key=api_key)

        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000]  # Truncate to model limit
        )

        return response.data[0].embedding

    except ImportError:
        logger.warning("openai package not installed. Install with: pip install openai")
        return [0.0] * EMBEDDING_DIMENSION
    except Exception as e:
        logger.error(f"OpenAI embedding error: {e}")
        return [0.0] * EMBEDDING_DIMENSION


def generate_embedding_local(text: str) -> List[float]:
    """
    Generate embedding using local sentence-transformers.

    Args:
        text: Text to embed

    Returns:
        Embedding vector as list of floats
    """
    try:
        from sentence_transformers import SentenceTransformer

        # Use a small, fast model
        model = SentenceTransformer("all-MiniLM-L6-v2")
        embedding = model.encode(text[:512])  # Truncate for local model

        return embedding.tolist()

    except ImportError:
        logger.warning("sentence-transformers not installed. Install with: pip install sentence-transformers")
        return [0.0] * 384  # MiniLM dimension
    except Exception as e:
        logger.error(f"Local embedding error: {e}")
        return [0.0] * 384


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding using configured method.

    Args:
        text: Text to embed

    Returns:
        Embedding vector
    """
    if USE_LOCAL_EMBEDDINGS:
        return generate_embedding_local(text)
    else:
        return generate_embedding_openai(text)


# ============================================
# EMBEDDING STORAGE
# ============================================

def store_chunk_embedding(chunk_id: str, embedding: List[float]) -> bool:
    """
    Store embedding for a chunk.

    Args:
        chunk_id: Chunk ID
        embedding: Embedding vector

    Returns:
        True if stored
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE document_chunks SET embedding = ? WHERE id = ?",
        (json.dumps(embedding), chunk_id)
    )

    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()

    return updated


def get_chunk_embedding(chunk_id: str) -> Optional[List[float]]:
    """
    Get embedding for a chunk.

    Args:
        chunk_id: Chunk ID

    Returns:
        Embedding vector or None
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT embedding FROM document_chunks WHERE id = ?", (chunk_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row["embedding"]:
        try:
            return json.loads(row["embedding"])
        except json.JSONDecodeError:
            return None

    return None


def generate_embeddings_for_document(document_id: str) -> int:
    """
    Generate and store embeddings for all chunks of a document.

    Args:
        document_id: Document ID

    Returns:
        Number of embeddings generated
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, content FROM document_chunks
        WHERE document_id = ? AND (embedding IS NULL OR embedding = '')
    """, (document_id,))

    chunks = cursor.fetchall()
    conn.close()

    count = 0
    for chunk in chunks:
        embedding = generate_embedding(chunk["content"])
        if store_chunk_embedding(chunk["id"], embedding):
            count += 1

    logger.info(f"Generated {count} embeddings for document {document_id}")
    return count


# ============================================
# SIMILARITY SEARCH
# ============================================

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Similarity score (-1 to 1)
    """
    if len(vec1) != len(vec2):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


def search_similar_chunks(
    query: str,
    project_id: str = None,
    top_k: int = TOP_K_DEFAULT,
    min_score: float = 0.3
) -> List[Dict[str, Any]]:
    """
    Search for chunks similar to query.

    Args:
        query: Search query
        project_id: Optional project filter
        top_k: Number of results
        min_score: Minimum similarity score

    Returns:
        List of {chunk, document, score} dicts
    """
    # Generate query embedding
    query_embedding = generate_embedding(query)

    # Get all chunks with embeddings
    conn = db.get_connection()
    cursor = conn.cursor()

    if project_id:
        cursor.execute("""
            SELECT dc.*, d.title as doc_title, d.project_id, d.document_type
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE dc.embedding IS NOT NULL
              AND dc.embedding != ''
              AND d.project_id = ?
        """, (project_id,))
    else:
        cursor.execute("""
            SELECT dc.*, d.title as doc_title, d.project_id, d.document_type
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE dc.embedding IS NOT NULL
              AND dc.embedding != ''
        """)

    chunks = cursor.fetchall()
    conn.close()

    # Calculate similarities
    results = []
    for chunk in chunks:
        try:
            chunk_embedding = json.loads(chunk["embedding"])
            score = cosine_similarity(query_embedding, chunk_embedding)

            if score >= min_score:
                results.append({
                    "chunk_id": chunk["id"],
                    "document_id": chunk["document_id"],
                    "doc_title": chunk["doc_title"],
                    "project_id": chunk["project_id"],
                    "document_type": chunk["document_type"],
                    "content": chunk["content"],
                    "chunk_index": chunk["chunk_index"],
                    "score": round(score, 4)
                })
        except (json.JSONDecodeError, TypeError):
            continue

    # Sort by score and return top_k
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ============================================
# CONTEXT BUILDING
# ============================================

def get_context_for_query(
    query: str,
    project_id: str = None,
    max_tokens: int = 2000,
    top_k: int = 5
) -> str:
    """
    Build context string from relevant chunks for a query.

    Args:
        query: User query
        project_id: Optional project filter
        max_tokens: Maximum context tokens
        top_k: Number of chunks to consider

    Returns:
        Context string for LLM
    """
    chunks = search_similar_chunks(query, project_id=project_id, top_k=top_k)

    if not chunks:
        return ""

    context_parts = []
    total_tokens = 0

    for chunk in chunks:
        chunk_tokens = chunk.get("token_count", len(chunk["content"]) // 4)

        if total_tokens + chunk_tokens > max_tokens:
            break

        context_parts.append(
            f"[From: {chunk['doc_title']}]\n{chunk['content']}"
        )
        total_tokens += chunk_tokens

    return "\n\n---\n\n".join(context_parts)


def get_related_documents(
    document_id: str,
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    Find documents similar to a given document.

    Args:
        document_id: Source document ID
        top_k: Number of results

    Returns:
        List of related documents with scores
    """
    # Get first chunk of source document as representative
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT content FROM document_chunks
        WHERE document_id = ?
        ORDER BY chunk_index ASC
        LIMIT 1
    """, (document_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
        return []

    # Search for similar chunks
    results = search_similar_chunks(row["content"], top_k=top_k * 2)

    # Deduplicate by document and exclude source
    seen_docs = {document_id}
    unique_docs = []

    for result in results:
        if result["document_id"] not in seen_docs:
            seen_docs.add(result["document_id"])
            unique_docs.append({
                "document_id": result["document_id"],
                "title": result["doc_title"],
                "score": result["score"]
            })

            if len(unique_docs) >= top_k:
                break

    return unique_docs


# ============================================
# BATCH OPERATIONS
# ============================================

def generate_all_missing_embeddings() -> Dict[str, int]:
    """
    Generate embeddings for all chunks that don't have them.

    Returns:
        Stats dict with counts
    """
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT dc.id, dc.content, d.id as document_id
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE dc.embedding IS NULL OR dc.embedding = ''
    """)

    chunks = cursor.fetchall()
    conn.close()

    stats = {
        "total_missing": len(chunks),
        "generated": 0,
        "errors": 0
    }

    for chunk in chunks:
        try:
            embedding = generate_embedding(chunk["content"])
            if store_chunk_embedding(chunk["id"], embedding):
                stats["generated"] += 1
        except Exception as e:
            logger.error(f"Error generating embedding for chunk {chunk['id']}: {e}")
            stats["errors"] += 1

    logger.info(f"Generated {stats['generated']} embeddings ({stats['errors']} errors)")
    return stats


def get_rag_stats() -> Dict[str, Any]:
    """Get RAG system statistics."""
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM document_chunks")
    total = cursor.fetchone()["total"]

    cursor.execute("SELECT COUNT(*) as with_embedding FROM document_chunks WHERE embedding IS NOT NULL AND embedding != ''")
    with_embedding = cursor.fetchone()["with_embedding"]

    conn.close()

    return {
        "total_chunks": total,
        "chunks_with_embeddings": with_embedding,
        "chunks_without_embeddings": total - with_embedding,
        "embedding_model": EMBEDDING_MODEL if not USE_LOCAL_EMBEDDINGS else "local (MiniLM)",
        "using_local_embeddings": USE_LOCAL_EMBEDDINGS
    }


# ============================================
# TEST
# ============================================

if __name__ == "__main__":
    print("Testing rag_engine...")

    # Test cosine similarity
    vec1 = [1.0, 0.0, 0.0]
    vec2 = [1.0, 0.0, 0.0]
    vec3 = [0.0, 1.0, 0.0]

    sim_same = cosine_similarity(vec1, vec2)
    sim_diff = cosine_similarity(vec1, vec3)

    print(f"  Same vectors similarity: {sim_same}")
    print(f"  Different vectors similarity: {sim_diff}")

    assert sim_same == 1.0
    assert sim_diff == 0.0

    # Test stats
    stats = get_rag_stats()
    print(f"  RAG stats: {stats}")

    print("All tests passed!")
```

---

## Files to Modify

### `server.py` - Add Document & RAG Endpoints

Add this section **at the end of the file, before `if __name__ == "__main__":`**:

```python
# ============================================
# DOCUMENT & RAG ENDPOINTS (Phase 6)
# ============================================

@app.route("/api/documents", methods=["GET"])
def list_documents():
    """List documents with optional filters."""
    import document_manager

    project_id = request.args.get("project_id")
    document_type = request.args.get("type")
    query = request.args.get("q")
    limit = request.args.get("limit", 20, type=int)

    docs = document_manager.search_documents(
        query=query,
        project_id=project_id,
        document_type=document_type,
        limit=limit
    )

    return jsonify({
        "ok": True,
        "count": len(docs),
        "documents": docs
    })


@app.route("/api/documents", methods=["POST"])
def upload_document():
    """Upload a new document (metadata only, file must exist)."""
    import document_manager

    data = request.get_json() or {}

    if not data.get("file_path"):
        return jsonify({"ok": False, "error": "file_path required"}), 400

    try:
        doc = document_manager.upload_document(
            file_path=data["file_path"],
            project_id=data.get("project_id"),
            task_id=data.get("task_id"),
            title=data.get("title"),
            document_type=data.get("document_type"),
            description=data.get("description"),
            source_type=data.get("source_type", "upload"),
            tags=data.get("tags"),
            extract_text=data.get("extract_text", True),
            generate_chunks=data.get("generate_chunks", True)
        )

        return jsonify({
            "ok": True,
            "document": doc
        })

    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/documents/<doc_id>", methods=["GET"])
def get_document(doc_id):
    """Get a document with its chunks."""
    import document_manager

    doc = document_manager.get_document_with_chunks(doc_id)

    if not doc:
        return jsonify({"ok": False, "error": "Document not found"}), 404

    return jsonify({
        "ok": True,
        "document": doc
    })


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    """Delete a document."""
    import document_manager

    success = document_manager.delete_document(doc_id)

    return jsonify({
        "ok": success,
        "message": "Document deleted" if success else "Document not found"
    })


@app.route("/api/documents/<doc_id>/embeddings", methods=["POST"])
def generate_document_embeddings(doc_id):
    """Generate embeddings for a document's chunks."""
    import rag_engine

    count = rag_engine.generate_embeddings_for_document(doc_id)

    return jsonify({
        "ok": True,
        "embeddings_generated": count
    })


@app.route("/api/rag/search", methods=["POST"])
def rag_search():
    """Search documents using semantic similarity."""
    import rag_engine

    data = request.get_json() or {}

    if not data.get("query"):
        return jsonify({"ok": False, "error": "query required"}), 400

    results = rag_engine.search_similar_chunks(
        query=data["query"],
        project_id=data.get("project_id"),
        top_k=data.get("top_k", 5),
        min_score=data.get("min_score", 0.3)
    )

    return jsonify({
        "ok": True,
        "count": len(results),
        "results": results
    })


@app.route("/api/rag/context", methods=["POST"])
def rag_context():
    """Get context for a query (for LLM augmentation)."""
    import rag_engine

    data = request.get_json() or {}

    if not data.get("query"):
        return jsonify({"ok": False, "error": "query required"}), 400

    context = rag_engine.get_context_for_query(
        query=data["query"],
        project_id=data.get("project_id"),
        max_tokens=data.get("max_tokens", 2000),
        top_k=data.get("top_k", 5)
    )

    return jsonify({
        "ok": True,
        "context": context,
        "context_length": len(context)
    })


@app.route("/api/rag/stats", methods=["GET"])
def rag_stats():
    """Get RAG system statistics."""
    import rag_engine
    import document_manager

    return jsonify({
        "ok": True,
        "rag": rag_engine.get_rag_stats(),
        "storage": document_manager.get_storage_stats()
    })
```

---

## Implementation Steps

### Step 1: Create Branch
```bash
cd /Users/alanknudson/Project_manager
git checkout -b feature/phase-6-documents-rag
```

### Step 2: Create document_manager.py
Create the file with the code above.

### Step 3: Create rag_engine.py
Create the file with the code above.

### Step 4: Add Server Endpoints
Add the endpoint section to `server.py` (at end, before main block).

### Step 5: Create Storage Directory
```bash
mkdir -p pm-docs
```

### Step 6: Test Imports
```bash
python3 -c "import document_manager; import rag_engine; print('OK')"
```

### Step 7: Commit
```bash
git add document_manager.py rag_engine.py server.py
git commit -m "Add Phase 6: Document Management & RAG

- Create document_manager.py with upload, chunking, extraction
- Create rag_engine.py with embeddings and similarity search
- Support PDF and image text extraction
- JSON embeddings in SQLite (scalable to pgvector)
- Add /api/documents/* and /api/rag/* endpoints

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Embedding Storage | JSON in SQLite | Simple, works for <10k chunks |
| Embedding Model | OpenAI text-embedding-3-small | High quality, cheap |
| Local Fallback | sentence-transformers MiniLM | Works offline |
| Chunk Size | 500 tokens with 50 overlap | Balance retrieval and context |
| Text Extraction | PyPDF2 + pytesseract | Common formats covered |
| File Storage | ./pm-docs/{project_id}/ | Organized by project |

---

## Optional Dependencies

```bash
# For PDF extraction
pip install PyPDF2

# For OCR (image text extraction)
pip install pytesseract pillow
# Also need tesseract binary: brew install tesseract

# For OpenAI embeddings
pip install openai

# For local embeddings (optional)
pip install sentence-transformers
```

---

## Environment Variables

```bash
# For OpenAI embeddings
export OPENAI_API_KEY="your-key"
```

---

## Testing Checklist

- [ ] `document_manager.py` imports without error
- [ ] `rag_engine.py` imports without error
- [ ] Text chunking works correctly
- [ ] Document upload creates record and chunks
- [ ] Cosine similarity calculation works
- [ ] `/api/documents` GET lists documents
- [ ] `/api/documents` POST uploads document
- [ ] `/api/rag/search` returns similar chunks
- [ ] `/api/rag/context` returns formatted context

---

## Notes for Agent

1. **DO NOT modify `db.py`** - All needed functions exist
2. **Add endpoints to server.py in a clearly marked section**
3. **Optional dependencies** - Code handles missing packages gracefully
4. **Create pm-docs directory** before testing uploads
5. **Test with**: `python3 -c "import document_manager; import rag_engine"`
6. **For embeddings**: Need OPENAI_API_KEY or use local mode
