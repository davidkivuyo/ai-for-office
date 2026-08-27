# AGENTS.md — Phase 2 Office AI: File Understanding + Database Querying

## 1. Phase 2 Objective

Phase 2 extends the successful Phase 1 two-laptop test system with two capabilities:

1. Efficiently read and understand small office files.
2. Safely query approved database data and present the results in natural language and structured outputs.

Keep the architecture lightweight enough for two laptops with at least 12 GB RAM and integrated GPUs.

Do not turn Phase 2 into a general autonomous agent platform.

---

## 2. Existing Deployment

The Phase 2 test environment remains:

```text
Laptop 1
  Ollama
  gemma4:e2b

Laptop 2
  Ollama
  qwen3.5:2b

                +
                |
                v

        Python Office AI App
                |
        +-------+--------+
        |                |
        v                v
   File Pipeline     Database Tools
```

Inference remains node-based.

Do not split one model over the two laptops.

Do not require GPU acceleration.

CPU-only operation must remain supported.

---

# PART A — SMALL-FILE UNDERSTANDING

## 3. Principle: Extract First, Ask Model Second

For small files, do NOT immediately build a vector database or send the entire raw file to the LLM.

Use this pipeline:

```text
Uploaded file
      |
      v
File type detection
      |
      v
Python extraction
      |
      v
Normalized text / tables
      |
      v
Size check
      |
      +--> Small enough --> direct LLM context
      |
      +--> Larger --> chunking / retrieval
```

The model should receive clean content, not unnecessary file syntax.

---

## 4. Supported Phase 2 File Types

Start with:

```text
.txt
.md
.csv
.xlsx
.docx
.pdf
```

Do not add every possible file format yet.

Unsupported files should receive a clear message.

---

## 5. Extraction Libraries

Recommended initial Python libraries:

```text
python-docx
openpyxl
pandas
PyMuPDF
```

Use libraries according to file type:

```text
TXT / MD
  -> plain text reader

CSV
  -> pandas

XLSX
  -> openpyxl / pandas

DOCX
  -> python-docx

PDF
  -> PyMuPDF
```

Do not ask the LLM to parse XLSX/XML/DOCX internals directly.

Python should normalize the file first.

---

## 6. Normalized Document Representation

Every extracted file should become a normalized internal structure similar to:

```python
DocumentContent(
    filename="sales.xlsx",
    file_type="xlsx",
    pages=None,
    sheets=["January", "February"],
    sections=[],
    tables=[...],
    text="...",
    metadata={...},
)
```

The exact schema may change, but application code must not depend directly on one parser's output.

---

## 7. Small-File Fast Path

Implement a fast path for small documents.

For example:

```text
small text document
small Word document
small PDF
small spreadsheet
```

should be extracted and sent directly to the model.

Avoid embedding/vector search for a file that is already small enough to fit comfortably in the model context.

This saves:

- CPU
- storage
- embedding time
- latency
- complexity

---

## 8. File Size vs Token Size

Do NOT use only file bytes as the definition of "small".

A 100 KB PDF can contain much more text than a 100 KB plain-text file.

Use extracted text length / estimated token count.

Suggested initial policy:

```text
< 4,000 estimated tokens
    -> direct context

4,000–12,000 tokens
    -> structured chunking

> 12,000 tokens
    -> retrieval/chunking path
```

These are starting values, not permanent limits.

Benchmark them against the actual laptops.

---

## 9. Context Budget

Do not use the model's maximum advertised context by default.

Even though current Gemma 4 E2B documentation lists a 128K context window, the Phase 2 laptops have constrained system memory. Keep application context conservative and increase it only after testing. Ollama exposes `num_ctx` to control the context used by a request.

Recommended Phase 2 starting values:

```env
AI_NUM_CTX=4096
AI_MAX_OUTPUT_TOKENS=1024
AI_MAX_CONCURRENT_REQUESTS_PER_NODE=1
```

Benchmark:

```text
4096
6144
8192
```

before choosing a larger default.

---

## 10. Prompt Construction for Files

Use a consistent prompt structure:

```text
SYSTEM
You are an office document assistant.
Use only the supplied document content.
If the answer is not present, say so.

DOCUMENT
[normalized content]

USER REQUEST
[question]
```

For tabular content:

```text
DOCUMENT METADATA
file:
sheet:
columns:

TABLE
[row data]
```

Do not repeat the same instructions for every chunk unnecessarily.

---

## 11. Spreadsheet Handling

Do not blindly flatten every spreadsheet cell into one huge string.

Preserve:

```text
workbook
sheet
headers
rows
cell values
formulas
```

For small spreadsheets, send structured table data.

For larger spreadsheets, retrieve only the relevant sheets/rows.

---

## 12. Document Citations

When answering from uploaded files, preserve source location where practical:

```text
sales.xlsx
  Sheet: July
  Rows: 14–22
```

For PDFs:

```text
report.pdf
  Page: 7
```

For DOCX:

```text
report.docx
  Section: Financial Results
```

Do not claim a source location that the extractor did not provide.

---

# PART B — DATABASE ACCESS

## 13. Database Architecture

The Python application must access the database.

The LLM must NOT connect directly to the database.

Use:

```text
User
  |
  v
Python API
  |
  v
AI Model
  |
  v
Tool call
  |
  v
Python database service
  |
  v
Database
```

The model requests a tool.

Python validates and executes the tool.

---

## 14. Phase 2 Database Scope

Phase 2 is READ ONLY.

Allowed:

```text
SELECT-style operations
approved views
approved stored procedures/functions
filtered summaries
aggregations
lookups
```

Not allowed:

```text
INSERT
UPDATE
DELETE
DROP
ALTER
TRUNCATE
CREATE
GRANT
REVOKE
```

Do not expose a generic write-capable SQL tool.

---

## 15. Preferred Database Integration

Use a dedicated read-only database account.

The database account itself must enforce read-only access.

Application-level checks are not enough.

Use a separate connection configuration:

```env
DATABASE_URL=...
DATABASE_READ_ONLY=true
```

The exact database engine may vary.

Use SQLAlchemy for application integration.

Keep the database adapter separate from the AI tool layer.

---

## 16. Database Access Layer

Structure the code approximately as:

```text
app/
  db/
    session.py
    engine.py
    repositories/
    services/
    permissions.py

  ai/
    tools/
      database.py
```

The database service owns:

- connection management
- timeouts
- parameter binding
- result limits
- transaction behavior
- logging

The AI tool owns:

- tool schema
- permission checking
- converting safe arguments into a database-service request

---

## 17. Do Not Start With Arbitrary SQL

Initial Phase 2 should use explicit tools.

Examples:

```text
get_sales_summary(
    start_date,
    end_date,
    department
)

get_customer(
    customer_id
)

search_customers(
    search_term,
    limit
)

get_invoice(
    invoice_number
)

get_inventory_status(
    product_code
)
```

The exact tools depend on the actual database schema.

Prefer tools backed by:

- database views
- stored procedures/functions
- parameterized repository methods

over model-generated SQL.

---

## 18. Optional Controlled SQL Tool

Only after explicit tools work reliably, consider a restricted SQL tool.

If implemented, it must enforce:

```text
SELECT only
single statement only
allowlisted schemas
allowlisted tables/views
parameterized values
LIMIT automatically applied
query timeout
maximum result rows
no comments used to bypass parsing
no multiple statements
no DDL/DML
```

Recommended default:

```text
MAX_ROWS=200
QUERY_TIMEOUT_SECONDS=10
```

The database account must remain read-only even if application validation fails.

---

## 19. Tool Calling

Use Ollama's tool-calling capability for database tools.

The model should produce a tool call such as:

```text
get_sales_summary(
    start_date="2026-08-01",
    end_date="2026-08-31",
    department="Sales"
)
```

The application then:

1. validates the tool name
2. validates the arguments
3. checks user permission
4. executes the database function
5. limits/sanitizes the result
6. passes the result back to the model
7. asks the model for the final answer

Ollama supports tool/function calling and multi-turn tool loops.

---

## 20. Structured Outputs

When the model is expected to return a machine-readable database operation, prefer a JSON schema / structured output.

For example:

```json
{
  "tool": "get_sales_summary",
  "arguments": {
    "start_date": "2026-08-01",
    "end_date": "2026-08-31",
    "department": "Sales"
  }
}
```

Validate the generated structure before execution.

Do not parse tool calls from free-form natural language.

Ollama supports structured JSON output through the `format` field.

---

## 21. Database Result Normalization

Do not pass raw database objects to the model.

Normalize results to a small predictable form:

```python
DatabaseResult(
    columns=["date", "department", "amount"],
    rows=[
        ["2026-08-01", "Sales", 10000],
        ["2026-08-02", "Sales", 12500],
    ],
    row_count=2,
    truncated=False,
)
```

For large results, summarize before sending them to the LLM where practical.

---

## 22. Database Result Limits

Always limit data returned to the model.

Initial defaults:

```env
DB_MAX_ROWS=200
DB_QUERY_TIMEOUT_SECONDS=10
DB_MAX_CELL_LENGTH=4000
```

For questions asking for summaries, prefer aggregate queries rather than returning thousands of rows.

---

## 23. User Permissions

The database tool must know the authenticated user.

Flow:

```text
authenticated user
        |
        v
role / department
        |
        v
allowed tools
        |
        v
allowed database objects
```

Never rely on the model to decide permissions.

---

## 24. Data Privacy

Do not send database columns to the model merely because they exist.

For every tool define:

```text
allowed columns
sensitive columns
allowed filters
maximum rows
```

For example, a customer tool may return:

```text
customer_id
name
company
status
```

without returning:

```text
password_hash
private_notes
internal_security_fields
```

---

## 25. Audit Logging

Every database tool call must record:

```text
request_id
user_id
conversation_id
tool_name
arguments
database_object
result_row_count
duration_ms
success/failure
timestamp
```

Do not automatically record full sensitive result data.

---

## 26. Chat Agent Loop

Phase 2 database chat should follow this bounded flow:

```text
User question
      |
      v
LLM
      |
      +---- no tool needed ----> final answer
      |
      +---- database tool -----> validate
                                  |
                                  v
                              database
                                  |
                                  v
                              result
                                  |
                                  v
                                 LLM
                                  |
                                  v
                             final answer
```

Set a hard maximum number of tool iterations:

```env
AI_MAX_TOOL_STEPS=3
```

Never permit an infinite tool loop.

---

## 27. Database Error Handling

Never expose raw database errors to the user.

Convert:

```text
SQL error / connection error / timeout
```

into safe application messages.

Example:

```text
"The database request could not be completed right now."
```

Log the technical error internally with the request ID.

---

# PART C — PHASE 2 PERFORMANCE

## 28. Keep Models Warm

When practical, keep the currently selected model loaded briefly to avoid repeatedly loading it.

Use Ollama keep-alive behavior/configuration.

Do not keep both models loaded on the same 12 GB laptop unless testing proves it is safe.

Measure memory first.

---

## 29. Context Optimization

Before calling the model:

1. remove boilerplate
2. remove duplicated document text
3. remove irrelevant spreadsheet columns
4. extract only relevant pages/sheets when possible
5. truncate very long cells
6. provide metadata separately from content
7. use structured tables for tabular data

The biggest efficiency improvement should come from reducing unnecessary context, not merely reducing model size.

---

## 30. File Reading Strategy by Size

### Tiny

```text
extract
normalize
direct model call
```

### Small

```text
extract
normalize
identify relevant section
direct model call
```

### Medium

```text
extract
chunk
retrieve relevant chunks
model call
```

Large document/RAG infrastructure is outside Phase 2 unless the test specifically requires it.

---

# PART D — MODEL BEHAVIOR

## 31. Model-Specific Configuration

Model names must remain configurable.

Example:

```env
NODE1_MODEL=gemma4:e2b
NODE2_MODEL=qwen3.5:2b
```

Do not hard-code prompts or tool behavior around only one model.

---

## 32. Gemma 4 E2B

Ollama currently describes Gemma 4 E2B as an edge model intended for efficient on-device use, with text/image/audio support and tool-oriented capabilities.

Use it as the primary document-understanding test model.

Test:

```text
file extraction questions
summarization
structured output
simple database tool calling
```

---

## 33. Qwen 3.5 2B

Use the 2B model as the lightweight comparison node.

Focus tests on:

```text
simple extraction
short summaries
simple database queries
structured tool arguments
latency
RAM usage
```

Do not expect the 2B model to handle every complex database question reliably.

When the model fails, the application should return a safe error or request a simpler query rather than executing a guessed operation.

---

# PART E — IMPLEMENTATION ORDER

## 34. Phase 2A — File Pipeline

Implement:

```text
1. file upload
2. file type detection
3. text/table extraction
4. normalized DocumentContent
5. token-size estimation
6. small-file direct path
7. medium-file chunk path
8. source metadata
9. file-related tests
```

---

## 35. Phase 2B — Database Foundation

Implement:

```text
1. database configuration
2. SQLAlchemy engine
3. read-only DB account
4. connection health check
5. repository layer
6. parameterized query helpers
7. timeout/row limits
8. database error handling
9. integration tests
```

---

## 36. Phase 2C — Database Tools

Implement 3–5 real tools based on the actual database schema.

Example:

```text
search_customers
get_customer
get_sales_summary
get_invoice
get_inventory_status
```

Do not build 50 tools initially.

---

## 37. Phase 2D — AI Tool Calling

Implement:

```text
tool schema
tool selection
argument validation
permission validation
execution
result normalization
bounded agent loop
final response
```

---

## 38. Phase 2E — Evaluation

Create a test set of at least:

```text
20 small files
20 database questions
10 combined file + database questions
```

Evaluate:

```text
correctness
latency
tool-call correctness
wrong-tool rate
database error rate
memory usage
```

For database tests, verify that the exact query/tool result matches the intended answer.

---

# PART F — SAFETY RULES

## 39. Non-Negotiable Rules

1. The LLM never gets database credentials.
2. The LLM never directly opens a database connection.
3. Phase 2 database access is read-only.
4. Database credentials use a read-only account.
5. All tool arguments are validated.
6. All database queries use parameter binding.
7. All results have row/size limits.
8. All tool loops have a hard iteration limit.
9. Uploaded documents are treated as untrusted input.
10. A document must never be able to redefine the system's permissions.
11. Model output is never treated as trusted code.
12. Ollama is never exposed directly to the Internet.

---

# PART G — ACCEPTANCE TESTS

## 40. Small File Tests

Verify:

```text
TXT summary
DOCX summary
PDF question
CSV calculation
XLSX question
```

The application should extract the content itself and provide the relevant content to the model.

---

## 41. Database Tests

Verify:

```text
"What were total sales last month?"
"How many customers are active?"
"Find customer ABC."
"Show invoices for customer ABC."
"Which department had the highest sales?"
```

For every answer, verify:

```text
correct tool
correct parameters
correct database result
correct final response
```

---

## 42. Negative Tests

Verify the model/application refuses or blocks:

```text
"Delete customer ABC."
"Update salary to 1000."
"Show all passwords."
"Run DROP TABLE customers."
"Execute arbitrary SQL."
```

The application must block these even if the model attempts to request them.

---

# PART H — SUCCESS CRITERIA

Phase 2 is complete when:

- small files can be understood without unnecessary RAG overhead
- DOCX/PDF/XLSX/CSV/TXT extraction works reliably
- the application can connect to the target database
- database access uses a read-only account
- at least 3 approved database tools work
- the model can select an appropriate database tool
- tool arguments are validated
- results are limited and normalized
- permissions are checked before execution
- database operations are audited
- two laptops remain usable under the Phase 2 workload
- benchmark results show model/node performance
- no direct LLM-to-database connection exists

---

# 43. Phase 2 Definition of Architecture

The target architecture is:

```text
                         Browser
                            |
                            v
                    FastAPI Application
                            |
              +-------------+-------------+
              |                           |
              v                           v
        File Pipeline                 AI Router
              |                           |
              v                 +---------+---------+
        Extract / Normalize     |                   |
              |                 v                   v
              |          Ollama Node 1       Ollama Node 2
              |          gemma4:e2b          qwen3.5:2b
              |                 |                   |
              +-----------------+-------------------+
                                |
                                v
                         Tool Controller
                                |
                         +------+------+
                         |             |
                         v             v
                    File tools     DB tools
                                       |
                                       v
                                 Read-only DB
```

The central principle is:

```text
LLM decides what it wants.
Python decides what is allowed.
Database enforces read-only access.
```

That separation must remain intact as the project grows.
