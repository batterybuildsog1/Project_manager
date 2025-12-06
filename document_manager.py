#!/usr/bin/env python3
"""
Document Manager - Metadata-First Agentic Approach

Phase 6: Document Management without traditional RAG.
Uses Claude to extract structured metadata at upload time,
then queries by metadata fields instead of semantic similarity.

Following Anthropic Dec 2025 guidance:
- "Intelligent orchestration rather than reactive tool requests"
- Metadata-first for mixed document types (images, PDFs, markdown)
- No embeddings needed at ~100 docs/project scale
"""

import os
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

import db

logger = logging.getLogger(__name__)

# ============================================
# CONFIGURATION
# ============================================

# Supported file types
SUPPORTED_EXTENSIONS = {
    'image': ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic'],
    'pdf': ['.pdf'],
    'text': ['.txt', '.md', '.markdown'],
    'spreadsheet': ['.csv', '.tsv'],
}

# Document storage directory
DOCUMENT_STORAGE = Path(__file__).parent / "documents"
DOCUMENT_STORAGE.mkdir(exist_ok=True)

# Metadata extraction prompts by document type
EXTRACTION_PROMPTS = {
    'receipt': """Extract the following from this receipt:
- vendor: Store/merchant name
- date: Transaction date (YYYY-MM-DD format)
- total: Total amount (number only, no currency symbol)
- currency: Currency code (USD, EUR, etc.)
- items: List of items with name and price
- payment_method: How it was paid (Visa, Cash, etc.)
- category: Expense category (food, office, travel, etc.)

Return as JSON.""",

    'invoice': """Extract the following from this invoice:
- invoice_number: Invoice ID/number
- vendor: Company/sender name
- date: Invoice date (YYYY-MM-DD)
- due_date: Payment due date (YYYY-MM-DD)
- total: Total amount (number only)
- currency: Currency code
- line_items: List of items with description, quantity, unit_price
- payment_terms: Any payment terms mentioned
- category: Business category

Return as JSON.""",

    'quote': """Extract the following from this quote/estimate:
- quote_number: Quote/estimate ID
- vendor: Company providing quote
- date: Quote date (YYYY-MM-DD)
- valid_until: Expiration date if mentioned
- total: Total quoted amount
- currency: Currency code
- line_items: List of items with description and price
- terms: Any terms or conditions

Return as JSON.""",

    'contract': """Extract the following from this contract:
- parties: List of party names
- effective_date: Start date (YYYY-MM-DD)
- end_date: End date if mentioned (YYYY-MM-DD)
- term: Duration (e.g., "12 months")
- total_value: Contract value if mentioned
- key_terms: List of important terms (payment, termination, etc.)
- renewal: Auto-renewal terms if any

Return as JSON.""",

    'manual': """Extract the following from this manual/documentation:
- title: Document title
- product: Product/service name
- version: Version number if mentioned
- sections: List of main section headings
- key_topics: Main topics covered

Return as JSON.""",

    'meeting_notes': """Extract the following from these meeting notes:
- date: Meeting date (YYYY-MM-DD)
- attendees: List of attendees
- topics: Main topics discussed
- decisions: Key decisions made
- action_items: List of action items with assignee if mentioned
- next_meeting: Next meeting date if mentioned

Return as JSON.""",

    'email': """Extract the following from this email:
- from: Sender
- to: Recipients
- date: Email date
- subject: Email subject
- summary: Brief summary of content
- action_required: Any action items mentioned
- attachments: List of attachment names if mentioned

Return as JSON.""",

    'other': """Extract key information from this document:
- title: Document title or main subject
- date: Any date mentioned (YYYY-MM-DD)
- summary: Brief summary (2-3 sentences)
- key_points: List of main points
- entities: Important names, companies, or references

Return as JSON."""
}


# ============================================
# TEXT EXTRACTION
# ============================================

def extract_text_from_file(file_path: str) -> Tuple[str, str]:
    """
    Extract text content from a file.

    Args:
        file_path: Path to the file

    Returns:
        Tuple of (extracted_text, extraction_method)
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    # Plain text files
    if ext in SUPPORTED_EXTENSIONS['text']:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read(), 'direct'
        except UnicodeDecodeError:
            with open(path, 'r', encoding='latin-1') as f:
                return f.read(), 'direct'

    # CSV/TSV files
    if ext in SUPPORTED_EXTENSIONS['spreadsheet']:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read(), 'direct'
        except UnicodeDecodeError:
            with open(path, 'r', encoding='latin-1') as f:
                return f.read(), 'direct'

    # PDF files
    if ext in SUPPORTED_EXTENSIONS['pdf']:
        return extract_text_from_pdf(file_path)

    # Image files - return empty, will use vision API
    if ext in SUPPORTED_EXTENSIONS['image']:
        return '', 'vision_required'

    return '', 'unsupported'


def extract_text_from_pdf(file_path: str) -> Tuple[str, str]:
    """
    Extract text from a PDF file.

    Args:
        file_path: Path to PDF

    Returns:
        Tuple of (extracted_text, extraction_method)
    """
    try:
        import PyPDF2

        text_parts = []
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

        full_text = '\n'.join(text_parts)
        if full_text.strip():
            return full_text, 'pypdf2'
        else:
            return '', 'ocr_required'

    except ImportError:
        logger.warning("PyPDF2 not installed, PDF text extraction unavailable")
        return '', 'pypdf2_missing'
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return '', f'error: {str(e)}'


def extract_text_from_image(file_path: str) -> Tuple[str, str]:
    """
    Extract text from an image using OCR.

    Args:
        file_path: Path to image

    Returns:
        Tuple of (extracted_text, extraction_method)
    """
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(file_path)
        text = pytesseract.image_to_string(img)
        return text, 'tesseract'

    except ImportError:
        logger.warning("pytesseract/PIL not installed, OCR unavailable")
        return '', 'ocr_missing'
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return '', f'error: {str(e)}'


# ============================================
# METADATA EXTRACTION (Claude-powered)
# ============================================

def get_extraction_prompt(document_type: str) -> str:
    """Get the metadata extraction prompt for a document type."""
    return EXTRACTION_PROMPTS.get(document_type, EXTRACTION_PROMPTS['other'])


def parse_extracted_metadata(raw_response: str) -> Dict[str, Any]:
    """
    Parse Claude's metadata extraction response.

    Args:
        raw_response: Claude's response text

    Returns:
        Parsed metadata dict
    """
    # Try to find JSON in the response
    try:
        # Look for JSON block
        if '```json' in raw_response:
            start = raw_response.find('```json') + 7
            end = raw_response.find('```', start)
            json_str = raw_response[start:end].strip()
        elif '```' in raw_response:
            start = raw_response.find('```') + 3
            end = raw_response.find('```', start)
            json_str = raw_response[start:end].strip()
        elif '{' in raw_response:
            # Find the JSON object
            start = raw_response.find('{')
            # Find matching closing brace
            depth = 0
            end = start
            for i, c in enumerate(raw_response[start:]):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end = start + i + 1
                        break
            json_str = raw_response[start:end]
        else:
            return {"raw_response": raw_response}

        return json.loads(json_str)

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse metadata JSON: {e}")
        return {"raw_response": raw_response, "parse_error": str(e)}


def build_metadata_extraction_request(
    document_type: str,
    content_text: str = None,
    file_path: str = None
) -> Dict[str, Any]:
    """
    Build a request for metadata extraction via Grok/Claude.

    This returns the request structure - the actual API call
    is handled by grok_client or similar.

    Args:
        document_type: Type of document
        content_text: Extracted text content (for PDFs, text files)
        file_path: Path to file (for images that need vision)

    Returns:
        Request dict with prompt and optional image path
    """
    prompt = get_extraction_prompt(document_type)

    request = {
        "prompt": prompt,
        "document_type": document_type,
    }

    if content_text:
        request["content"] = content_text[:10000]  # Limit content size

    if file_path:
        path = Path(file_path)
        if path.suffix.lower() in SUPPORTED_EXTENSIONS['image']:
            request["image_path"] = str(path)
            request["requires_vision"] = True

    return request


# ============================================
# FILE HANDLING
# ============================================

def compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of file."""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def get_file_type(file_path: str) -> str:
    """Determine file type category from extension."""
    ext = Path(file_path).suffix.lower()
    for category, extensions in SUPPORTED_EXTENSIONS.items():
        if ext in extensions:
            return category
    return 'unknown'


def copy_to_storage(
    source_path: str,
    project_id: str = None,
    preserve_name: bool = True
) -> str:
    """
    Copy a file to document storage.

    Args:
        source_path: Original file path
        project_id: Optional project ID for organization
        preserve_name: Keep original filename

    Returns:
        New file path in storage
    """
    source = Path(source_path)

    # Create project subdirectory if specified
    if project_id:
        dest_dir = DOCUMENT_STORAGE / project_id
    else:
        dest_dir = DOCUMENT_STORAGE / "unassigned"
    dest_dir.mkdir(exist_ok=True)

    # Generate destination filename
    if preserve_name:
        dest_name = source.name
        # Handle duplicates
        dest_path = dest_dir / dest_name
        counter = 1
        while dest_path.exists():
            dest_name = f"{source.stem}_{counter}{source.suffix}"
            dest_path = dest_dir / dest_name
            counter += 1
    else:
        # Use hash-based name
        file_hash = compute_file_hash(str(source))[:12]
        dest_name = f"{file_hash}{source.suffix}"
        dest_path = dest_dir / dest_name

    # Copy file
    import shutil
    shutil.copy2(source, dest_path)

    return str(dest_path)


# ============================================
# MAIN DOCUMENT OPERATIONS
# ============================================

def upload_document(
    file_path: str,
    project_id: str = None,
    task_id: str = None,
    document_type: str = 'other',
    copy_file: bool = True,
    metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Upload a document with metadata extraction.

    Args:
        file_path: Path to the document file
        project_id: Optional project to associate with
        task_id: Optional task to associate with
        document_type: Type of document (receipt, invoice, etc.)
        copy_file: Whether to copy file to storage
        metadata: Optional pre-extracted metadata

    Returns:
        Document dict with extracted metadata and extraction request if needed
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Get file info
    file_size = path.stat().st_size
    file_hash = compute_file_hash(str(path))
    file_type = get_file_type(str(path))

    # Copy to storage if requested
    if copy_file:
        stored_path = copy_to_storage(str(path), project_id)
    else:
        stored_path = str(path)

    # Extract text
    content_text, extraction_method = extract_text_from_file(str(path))

    # For images, try OCR if available
    if extraction_method == 'vision_required':
        ocr_text, ocr_method = extract_text_from_image(str(path))
        if ocr_text:
            content_text = ocr_text
            extraction_method = ocr_method

    # Prepare metadata from input or defaults
    doc_metadata = metadata or {}

    # Create document record
    doc = db.create_document(
        filename=path.name,
        file_path=stored_path,
        project_id=project_id,
        task_id=task_id,
        document_type=document_type,
        content_text=content_text if content_text else None,
        file_type=file_type,
        file_size_bytes=file_size,
        file_hash=file_hash,
        vendor=doc_metadata.get('vendor'),
        amount=doc_metadata.get('amount') or doc_metadata.get('total'),
        transaction_date=doc_metadata.get('transaction_date') or doc_metadata.get('date'),
        category=doc_metadata.get('category'),
        tags=doc_metadata.get('tags'),
        notes=doc_metadata.get('notes')
    )

    # Build metadata extraction request for later processing
    extraction_request = None
    if document_type != 'other' or not metadata:
        extraction_request = build_metadata_extraction_request(
            document_type=document_type,
            content_text=content_text,
            file_path=str(path) if file_type == 'image' else None
        )

    return {
        "document": doc,
        "extraction_method": extraction_method,
        "extraction_request": extraction_request,
        "needs_ai_extraction": extraction_request is not None
    }


def update_document_metadata(
    doc_id: str,
    extracted_metadata: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Update a document with extracted metadata from AI.

    Args:
        doc_id: Document ID
        extracted_metadata: Metadata dict from Claude/Grok

    Returns:
        Updated document dict
    """
    # Map extracted fields to document columns
    updates = {}

    # Direct mappings
    if 'vendor' in extracted_metadata:
        updates['vendor'] = extracted_metadata['vendor']

    if 'total' in extracted_metadata:
        try:
            updates['amount'] = float(extracted_metadata['total'])
        except (ValueError, TypeError):
            pass
    elif 'amount' in extracted_metadata:
        try:
            updates['amount'] = float(extracted_metadata['amount'])
        except (ValueError, TypeError):
            pass

    if 'currency' in extracted_metadata:
        updates['currency'] = extracted_metadata['currency']

    if 'date' in extracted_metadata:
        updates['transaction_date'] = extracted_metadata['date']
    elif 'transaction_date' in extracted_metadata:
        updates['transaction_date'] = extracted_metadata['transaction_date']

    if 'category' in extracted_metadata:
        updates['category'] = extracted_metadata['category']

    # Store full extracted metadata as JSON in notes or content_summary
    if extracted_metadata:
        # Create a summary from key fields
        summary_parts = []
        if extracted_metadata.get('vendor'):
            summary_parts.append(f"Vendor: {extracted_metadata['vendor']}")
        if extracted_metadata.get('total'):
            summary_parts.append(f"Amount: {extracted_metadata['total']}")
        if extracted_metadata.get('date'):
            summary_parts.append(f"Date: {extracted_metadata['date']}")
        if extracted_metadata.get('summary'):
            summary_parts.append(extracted_metadata['summary'])

        if summary_parts:
            updates['content_summary'] = ' | '.join(summary_parts)

        # Store full metadata as JSON in notes if not already set
        updates['notes'] = json.dumps(extracted_metadata, indent=2)

    if updates:
        return db.update_document(doc_id, **updates)

    return db.get_document(doc_id)


def search_documents(
    project_id: str = None,
    query: str = None,
    document_type: str = None,
    vendor: str = None,
    category: str = None,
    date_from: str = None,
    date_to: str = None,
    amount_min: float = None,
    amount_max: float = None,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Search documents with flexible filtering.

    This is a pass-through to db.search_documents with the same signature.

    Args:
        project_id: Filter by project
        query: Text search
        document_type: Filter by type
        vendor: Filter by vendor (partial match)
        category: Filter by category
        date_from: Transaction date >= this
        date_to: Transaction date <= this
        amount_min: Amount >= this
        amount_max: Amount <= this
        limit: Max results

    Returns:
        List of matching documents
    """
    return db.search_documents(
        project_id=project_id,
        query=query,
        document_type=document_type,
        vendor=vendor,
        category=category,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
        limit=limit
    )


def get_document(doc_id: str) -> Optional[Dict[str, Any]]:
    """Get a document by ID."""
    return db.get_document(doc_id)


def delete_document(doc_id: str, delete_file: bool = False) -> bool:
    """
    Delete a document record and optionally the file.

    Args:
        doc_id: Document ID
        delete_file: Also delete the physical file

    Returns:
        True if deleted
    """
    if delete_file:
        doc = db.get_document(doc_id)
        if doc and doc.get('file_path'):
            try:
                Path(doc['file_path']).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to delete file: {e}")

    return db.delete_document(doc_id)


def get_document_stats(project_id: str = None) -> Dict[str, Any]:
    """Get document statistics."""
    return db.get_document_stats(project_id)


# ============================================
# ON-DEMAND EXTRACTION (Agentic Tools)
# ============================================

def build_question_request(
    doc_id: str,
    question: str
) -> Optional[Dict[str, Any]]:
    """
    Build a request for answering a question about a document.

    This prepares the request structure for the AI to answer
    questions about a specific document.

    Args:
        doc_id: Document ID
        question: User's question

    Returns:
        Request dict with document context and question
    """
    doc = db.get_document(doc_id)
    if not doc:
        return None

    request = {
        "document_id": doc_id,
        "question": question,
        "document_type": doc.get('document_type'),
        "filename": doc.get('filename'),
    }

    # Include text content if available
    if doc.get('content_text'):
        request["content"] = doc['content_text'][:15000]

    # Include stored metadata
    if doc.get('notes'):
        try:
            request["metadata"] = json.loads(doc['notes'])
        except json.JSONDecodeError:
            request["metadata"] = {"notes": doc['notes']}

    # For images, include path for vision
    if doc.get('file_type') == 'image' and doc.get('file_path'):
        request["image_path"] = doc['file_path']
        request["requires_vision"] = True

    return request


def list_documents_for_context(
    project_id: str = None,
    document_type: str = None,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """
    Get a lightweight list of documents for AI context.

    Returns only key fields to minimize token usage.

    Args:
        project_id: Filter by project
        document_type: Filter by type
        limit: Max results

    Returns:
        List of document summaries
    """
    docs = db.list_documents(
        project_id=project_id,
        document_type=document_type,
        limit=limit
    )

    # Return lightweight summaries
    return [
        {
            "id": d['id'],
            "filename": d['filename'],
            "type": d['document_type'],
            "vendor": d.get('vendor'),
            "amount": d.get('amount'),
            "date": d.get('transaction_date'),
            "summary": d.get('content_summary', '')[:100]
        }
        for d in docs
    ]


# ============================================
# GROK TOOL DEFINITIONS
# ============================================

# These tool definitions can be used by the agent
DOCUMENT_TOOLS = [
    {
        "name": "search_documents",
        "description": "Search uploaded documents by metadata filters (vendor, date range, amount, category) or text content.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project ID"},
                "query": {"type": "string", "description": "Text search in content/filename"},
                "document_type": {
                    "type": "string",
                    "enum": ["receipt", "invoice", "quote", "contract", "manual", "note", "screenshot", "email", "meeting_notes", "other"],
                    "description": "Filter by document type"
                },
                "vendor": {"type": "string", "description": "Filter by vendor name (partial match)"},
                "category": {"type": "string", "description": "Filter by expense category"},
                "date_from": {"type": "string", "description": "Transaction date >= this (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "Transaction date <= this (YYYY-MM-DD)"},
                "amount_min": {"type": "number", "description": "Amount >= this"},
                "amount_max": {"type": "number", "description": "Amount <= this"},
            }
        }
    },
    {
        "name": "get_document_details",
        "description": "Get full details of a specific document including content and metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Document ID"}
            },
            "required": ["document_id"]
        }
    },
    {
        "name": "list_recent_documents",
        "description": "List recently uploaded documents with summary info.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project"},
                "document_type": {"type": "string", "description": "Filter by type"},
                "limit": {"type": "integer", "description": "Max results (default 20)"}
            }
        }
    },
    {
        "name": "get_document_stats",
        "description": "Get statistics about uploaded documents (counts by type, totals).",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project"}
            }
        }
    }
]


def execute_document_tool(tool_name: str, params: Dict[str, Any]) -> Any:
    """
    Execute a document management tool.

    Args:
        tool_name: Name of the tool
        params: Tool parameters

    Returns:
        Tool result
    """
    if tool_name == "search_documents":
        return search_documents(**params)

    elif tool_name == "get_document_details":
        doc_id = params.get("document_id")
        if not doc_id:
            return {"error": "document_id required"}
        return get_document(doc_id)

    elif tool_name == "list_recent_documents":
        return list_documents_for_context(
            project_id=params.get("project_id"),
            document_type=params.get("document_type"),
            limit=params.get("limit", 20)
        )

    elif tool_name == "get_document_stats":
        return get_document_stats(params.get("project_id"))

    else:
        return {"error": f"Unknown tool: {tool_name}"}
