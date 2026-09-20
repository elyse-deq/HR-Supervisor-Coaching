"""
learning-coaching-resources: a read-only MCP server that exposes approved
coaching guides, training, manager resources, and documentation templates to a
coaching agent.

Companion to hr-policy-library. That server answers "what does policy say, and
when must HR be involved?" This one answers "how do I hold the conversation,
and how do I document it?"

Design rules:
  * Read-only. No tool writes, deletes, or calls out to another system.
  * Approved content only. Documents must have approved_for_ai: true and a
    sensitivity of "general" to be loaded at all.
  * Every result carries source title, version, effective date, and link.
  * When nothing approved matches, the tool says so instead of guessing.
  * No employee-specific data. Queries that look like they contain personal
    identifiers are rejected.
  * No escalation decisions. Escalation rules live in hr-policy-library
    (get_escalation_rule) so there is one source of truth for when HR is
    required. Guides here only point back to it.
"""

import json
import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONTENT_DIR = Path(os.environ.get("LCR_CONTENT_DIR", BASE_DIR / "content")).resolve()
AUDIT_LOG = Path(os.environ.get("LCR_AUDIT_LOG", BASE_DIR / "audit.jsonl"))
LOG_QUERY_TEXT = os.environ.get("LCR_LOG_QUERY_TEXT", "true").lower() == "true"

ALLOWED_SENSITIVITY = {"general"}
GUIDE_CATEGORIES = {"conversation-guide", "training", "manager-resource"}
MAX_QUERY_CHARS = 500
MAX_RESULTS = 5
MAX_PASSAGE_CHARS = 1200
# Weak keyword matches are treated as "no approved source". Tune this against
# your evaluation set; raise it if the agent answers from thin matches.
MIN_RELEVANCE = float(os.environ.get("LCR_MIN_RELEVANCE", "0.3"))
MIN_TERM_COVERAGE = float(os.environ.get("LCR_MIN_TERM_COVERAGE", "0.33"))

STOPWORDS = set("""a an and are as at be but by do does for from had has have how i if in is it
its me my of on or our should so that the their them then there they this to was we what when
where which who why will with would you your can could about into than
keep every week day someone told being handle give myself help need want going get got
really very just also two both people""".split())

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("learning-coaching-resources")

# Patterns that suggest personal data is being pasted into a query.
PII_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                 # email
    re.compile(r"\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b"),         # SSN-like
    re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),  # phone
    re.compile(r"\b(?:employee|emp)\s*(?:id|#|number)\s*[:#]?\s*\w+", re.I),
]

# ----------------------------------------------------------------------------
# Data model and loading
# ----------------------------------------------------------------------------

@dataclass
class Chunk:
    section: str
    text: str
    tokens: list = field(default_factory=list)


@dataclass
class Document:
    id: str
    kind: str                # "guide" or "template"
    category: str            # guides only: conversation-guide, training, manager-resource
    title: str
    owner: str
    version: str
    effective_date: str
    review_date: str
    topics: list
    audience: list
    source_url: str
    body: str
    chunks: list = field(default_factory=list)

    @property
    def review_overdue(self) -> bool:
        try:
            return date.fromisoformat(self.review_date) < date.today()
        except (ValueError, TypeError):
            return True   # a missing or bad review date counts as overdue

    def citation(self) -> dict:
        cite = {
            "doc_id": self.id,
            "title": self.title,
            "version": self.version,
            "effective_date": self.effective_date,
            "source_url": self.source_url,
            "review_overdue": self.review_overdue,
        }
        if self.category:
            cite["category"] = self.category
        return cite


def stem(word: str) -> str:
    """Very light stemmer so 'employee/employees', 'argue/arguing' and
    'arrive/arriving' land on the same token."""
    if word.endswith("ies") and len(word) > 5:
        word = word[:-3] + "y"
    else:
        for suffix in ("ing", "ed", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                if suffix == "s" and word.endswith("ss"):
                    break
                word = word[: -len(suffix)]
                break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


def tokenize(text: str) -> list:
    return [stem(w) for w in re.findall(r"[a-z0-9'-]+", text.lower())]


def load_synonyms() -> dict:
    path = CONTENT_DIR / "synonyms.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {stem(k.lower()): [stem(v.lower()) for v in vals] for k, vals in raw.items()}


SYNONYMS = load_synonyms()


def query_groups(text: str) -> list:
    """One group per meaningful query word: the word plus any HR-approved synonyms."""
    groups = []
    for w in re.findall(r"[a-z0-9'-]+", text.lower()):
        if w in STOPWORDS or len(w) < 2:
            continue
        s = stem(w)
        groups.append({s, *SYNONYMS.get(s, [])})
    return groups


def split_front_matter(raw: str):
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return yaml.safe_load(parts[1]) or {}, parts[2].strip()
    return {}, raw.strip()


def chunk_body(body: str) -> list:
    """Split on level-2 headings so each chunk is one section of a guide."""
    chunks, current_title, current_lines = [], "Overview", []
    for line in body.splitlines():
        if line.startswith("## "):
            if current_lines:
                chunks.append((current_title, "\n".join(current_lines).strip()))
            current_title, current_lines = line[3:].strip(), []
        else:
            current_lines.append(line)
    if current_lines:
        chunks.append((current_title, "\n".join(current_lines).strip()))
    return [Chunk(t, x, tokenize(f"{t} {x}")) for t, x in chunks if x]


def load_documents() -> dict:
    docs = {}
    for kind, folder in (("guide", "guides"), ("template", "templates")):
        for path in sorted((CONTENT_DIR / folder).glob("*.md")):
            try:
                meta, body = split_front_matter(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                log.warning("Skipping %s: bad front matter (%s)", path.name, exc)
                continue
            if not meta.get("approved_for_ai", False):
                log.info("Skipping %s: not approved for AI use", path.name)
                continue
            if meta.get("sensitivity", "restricted") not in ALLOWED_SENSITIVITY:
                log.info("Skipping %s: sensitivity not allowed", path.name)
                continue
            missing = [k for k in ("id", "title", "version", "effective_date") if k not in meta]
            if missing:
                log.warning("Skipping %s: missing metadata %s", path.name, missing)
                continue
            category = str(meta.get("category", "")).lower()
            if kind == "guide" and category not in GUIDE_CATEGORIES:
                log.warning("Skipping %s: category must be one of %s", path.name,
                            sorted(GUIDE_CATEGORIES))
                continue
            doc = Document(
                id=str(meta["id"]),
                kind=kind,
                category=category if kind == "guide" else "",
                title=meta["title"],
                owner=meta.get("owner", ""),
                version=str(meta["version"]),
                effective_date=str(meta["effective_date"]),
                review_date=str(meta.get("review_date", "")),
                topics=[t.lower() for t in meta.get("topics", [])],
                audience=[a.lower() for a in meta.get("audience", [])],
                source_url=meta.get("source_url", ""),
                body=body,
            )
            doc.chunks = chunk_body(body)
            docs[doc.id] = doc
    return docs


DOCS = load_documents()
log.info("Loaded %d documents", len(DOCS))

# ----------------------------------------------------------------------------
# Search (BM25-style keyword ranking with metadata filters)
# Swap this function for embeddings or a hybrid search when you scale up.
# ----------------------------------------------------------------------------

def bm25_rank(groups: list, candidates: list, k1=1.5, b=0.75) -> list:
    """
    groups: list of sets of interchangeable terms (a word and its synonyms).
    candidates: list of (doc, chunk).
    Returns [(score, coverage, doc, chunk)], best first.
    """
    if not candidates or not groups:
        return []
    n = len(candidates)
    avg_len = sum(len(c.tokens) for _, c in candidates) / n or 1
    df = Counter()
    for _, c in candidates:
        for tok in set(c.tokens):
            df[tok] += 1
    scored = []
    for doc, chunk in candidates:
        tf = Counter(chunk.tokens)
        norm = k1 * (1 - b + b * len(chunk.tokens) / avg_len)
        total, matched = 0.0, 0
        for group in groups:
            best = 0.0
            for term in group:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                best = max(best, idf * tf[term] * (k1 + 1) / (tf[term] + norm))
            if best > 0:
                matched += 1
                total += best
        if total > 0:
            scored.append((total, matched / len(groups), doc, chunk))
    return sorted(scored, key=lambda x: x[0], reverse=True)


# ----------------------------------------------------------------------------
# Guardrails and audit
# ----------------------------------------------------------------------------

def contains_personal_data(text: str) -> bool:
    return any(p.search(text) for p in PII_PATTERNS)


def audit(tool: str, args: dict, returned_ids: list):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "args": args if LOG_QUERY_TEXT else {k: "[redacted]" for k in args},
        "returned": returned_ids,
    }
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.error("Audit log write failed: %s", exc)


def no_source(message: str = "") -> dict:
    return {
        "found": False,
        "message": message or (
            "No approved source found. Do not answer from general knowledge. "
            "Tell the supervisor this topic is not covered and direct them to HR."
        ),
    }


PERSONAL_DATA_MESSAGE = (
    "Query appears to contain personal data. Rewrite it as a generic scenario "
    "without names, IDs, emails, or phone numbers."
)

# ----------------------------------------------------------------------------
# MCP server and tools
# ----------------------------------------------------------------------------

mcp = FastMCP(
    "learning-coaching-resources",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8001")),
)


@mcp.tool()
def search_guides(query: str, topic: str = "", audience: str = "", category: str = "") -> dict:
    """Search approved coaching guides, training, and manager resources for
    passages that help a supervisor prepare for or hold a conversation. Use
    this for HOW to run the conversation. For what policy says or when HR must
    be involved, use hr-policy-library instead. Optionally filter by topic (for
    example 'attendance', 'safety', 'conflict', 'performance'), audience (for
    example 'supervisor'), and category ('conversation-guide', 'training',
    'manager-resource'). Do NOT include employee names, IDs, or other personal
    details in the query. Returns cited passages, or found=false when nothing
    approved matches."""
    query = (query or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return no_source("Empty query.")
    if contains_personal_data(query):
        audit("search_guides", {"query": "[rejected: personal data]"}, [])
        return {"found": False, "message": PERSONAL_DATA_MESSAGE}

    topic_f, audience_f, category_f = (
        topic.strip().lower(), audience.strip().lower(), category.strip().lower()
    )
    if category_f and category_f not in GUIDE_CATEGORIES:
        return no_source(f"Unknown category '{category}'. Use one of: "
                         + ", ".join(sorted(GUIDE_CATEGORIES)) + ".")

    candidates = []
    for doc in DOCS.values():
        if doc.kind != "guide":
            continue
        if category_f and doc.category != category_f:
            continue
        if topic_f and topic_f not in doc.topics:
            continue
        if audience_f and doc.audience and audience_f not in doc.audience:
            continue
        candidates.extend((doc, chunk) for chunk in doc.chunks)

    ranked = [
        (s, d, c)
        for s, cov, d, c in bm25_rank(query_groups(query), candidates)
        if s >= MIN_RELEVANCE and cov >= MIN_TERM_COVERAGE
    ][:MAX_RESULTS]
    audit("search_guides",
          {"query": query, "topic": topic_f, "audience": audience_f, "category": category_f},
          [d.id for _, d, _ in ranked])
    if not ranked:
        return no_source()

    return {
        "found": True,
        "results": [
            {
                "passage": chunk.text[:MAX_PASSAGE_CHARS],
                "section": chunk.section,
                "relevance": round(score, 2),
                **doc.citation(),
            }
            for score, doc, chunk in ranked
        ],
    }


@mcp.tool()
def get_guide(guide_id: str, section: str = "") -> dict:
    """Return the full text of one approved guide, training module, or manager
    resource by its doc_id (from search_guides). Optionally pass a section
    heading to return only that section."""
    doc = DOCS.get(guide_id.strip())
    if not doc or doc.kind != "guide":
        audit("get_guide", {"guide_id": guide_id}, [])
        return no_source(f"No approved guide with id '{guide_id}'.")

    if section:
        matches = [c for c in doc.chunks if c.section.lower() == section.strip().lower()]
        if not matches:
            audit("get_guide", {"guide_id": guide_id, "section": section}, [])
            return no_source(
                f"Section '{section}' not found. Available sections: "
                + ", ".join(c.section for c in doc.chunks)
            )
        text = "\n\n".join(f"## {c.section}\n{c.text}" for c in matches)
    else:
        text = doc.body

    audit("get_guide", {"guide_id": guide_id, "section": section}, [doc.id])
    return {"found": True, "text": text, **doc.citation()}


@mcp.tool()
def get_documentation_template(topic: str, audience: str = "") -> dict:
    """Return approved documentation templates for recording a coaching
    conversation or the follow-up to it, for a topic such as 'attendance' or
    'performance'. Templates are starting points that the supervisor adapts.
    Documentation should state facts only. Do NOT include employee-specific
    details in the topic."""
    topic_f, audience_f = topic.strip().lower(), audience.strip().lower()
    if not topic_f:
        return no_source("A topic is required.")
    if contains_personal_data(topic_f):
        return {"found": False, "message": "Remove personal details and use a generic topic."}

    matches = [
        d for d in DOCS.values()
        if d.kind == "template"
        and topic_f in d.topics
        and (not audience_f or not d.audience or audience_f in d.audience)
    ]
    audit("get_documentation_template", {"topic": topic_f, "audience": audience_f},
          [d.id for d in matches])
    if not matches:
        return no_source(f"No approved documentation template for topic '{topic}'.")

    return {
        "found": True,
        "templates": [{"text": d.body, **d.citation()} for d in matches],
    }


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")   # stdio | streamable-http
    mcp.run(transport=transport)
