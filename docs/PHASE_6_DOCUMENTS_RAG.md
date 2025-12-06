# Phase 6: Document Management - Metadata-First Approach

> **Status**: COMPLETE
> **Implemented**: 2024-12
> **Approach**: Metadata-first agentic (NOT traditional RAG)

---

## Overview

Document management system using **metadata extraction** instead of embeddings/vector search. At ~100 docs/project scale, structured metadata queries outperform semantic similarity.

**Key Decision**: Following Anthropic Dec 2025 guidance on "intelligent orchestration" - we extract structured metadata at upload time, then query by fields (vendor, date, amount, category) rather than embedding similarity.

---

## Why Not Traditional RAG?

| Factor | Traditional RAG | Our Approach |
|--------|----------------|--------------|
| Scale | Optimized for 10k+ docs | ~100 docs/project |
| Images | Embeddings meaningless | Metadata from vision API |
| Query Type | "Find similar" | "Find receipts from Amazon over $50" |
| Complexity | Embeddings + vector DB | SQL queries |
| Speed | Similarity calculation | Direct index lookup |

---

## Files Implemented

### `document_manager.py` (~825 lines)

Core document management with metadata-first approach.

**Text Extraction:**
- `extract_text_from_file()` - Routes to appropriate extractor
- `extract_text_from_pdf()` - PyPDF2 extraction
- `extract_text_from_image()` - Tesseract OCR

**Metadata Extraction:**
- `EXTRACTION_PROMPTS` - Type-specific prompts for Claude/Grok
- `build_metadata_extraction_request()` - Prepares AI extraction request
- `parse_extracted_metadata()` - Parses JSON from AI response
- `update_document_metadata()` - Applies extracted metadata to document

**Document Operations:**
- `upload_document()` - Upload with text extraction, returns AI extraction request
- `search_documents()` - Rich filtering (vendor, date range, amount, category)
- `get_document()` / `delete_document()` - CRUD operations
- `get_document_stats()` - Counts and totals by type

**Agentic Tools:**
- `DOCUMENT_TOOLS` - Tool definitions for agent use
- `execute_document_tool()` - Tool executor
- `build_question_request()` - Prepare doc Q&A for AI
- `list_documents_for_context()` - Lightweight list for AI context

### `db.py` additions

New document functions:
- `search_documents()` - Flexible filtering with 9 parameters
- `update_document()` - Update any document field
- `delete_document()` - Delete with cascade to chunks
- `get_document_stats()` - Aggregations by type

### `server.py` endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/documents` | GET | List/search with filters |
| `/api/documents` | POST | Upload new document |
| `/api/documents/<id>` | GET | Get document details |
| `/api/documents/<id>` | PUT | Update metadata |
| `/api/documents/<id>` | DELETE | Delete document |
| `/api/documents/<id>/extract` | POST | Apply AI-extracted metadata |
| `/api/documents/<id>/question` | POST | Build Q&A request for AI |
| `/api/documents/stats` | GET | Get document statistics |

---

## Document Types

```
receipt, invoice, quote, contract, manual, note,
screenshot, email, meeting_notes, other
```

Each type has a specific extraction prompt in `EXTRACTION_PROMPTS`.

---

## Workflow

### Upload Flow
```
1. POST /api/documents {file_path, document_type, project_id}
2. Server extracts text (PDF/OCR/direct)
3. Server returns document + extraction_request
4. Client sends extraction_request to Claude/Grok
5. Client POSTs extracted metadata to /api/documents/<id>/extract
6. Document now has structured metadata for querying
```

### Search Flow
```
GET /api/documents?vendor=Amazon&date_from=2024-01-01&amount_min=50

Returns documents matching ALL criteria.
```

---

## Database Schema

Uses existing `documents` table with these key fields:

```sql
-- Core
id, filename, file_path, file_type, file_size_bytes, file_hash
project_id, task_id, document_type

-- Extracted Metadata
vendor, amount, currency, transaction_date, category

-- Content
content_text, content_summary, tags, notes
```

The `document_chunks` table exists but is unused in this approach.

---

## Dependencies

**Required:**
- None (graceful degradation)

**Optional:**
- `PyPDF2` - PDF text extraction
- `pytesseract` + `pillow` - Image OCR
- Tesseract binary: `brew install tesseract`

---

## Storage

Documents stored in `./documents/{project_id}/` or `./documents/unassigned/`.

File naming handles duplicates automatically.

---

## Testing

```bash
python3 -c "
import db
import document_manager

# Test search
results = db.search_documents(vendor='Amazon', amount_min=20)
print(f'Found {len(results)} documents')

# Test stats
stats = db.get_document_stats()
print(f'Stats: {stats}')
"
```

---

## Future Considerations

If scale grows beyond ~1000 docs/project:
1. Add FTS5 full-text search index
2. Consider embeddings for content_text only (not images)
3. Could add pgvector if migrating to Postgres

For now, metadata queries are faster and more precise.
