# Structured Data Detection in Free Text: Library Evaluation for LeakGuard

*2026-09-28 · research note, nothing run · for the LeakGuard structured data detection layer*

## Context

LeakGuard runs as a LiteLLM pre_call hook. It needs to detect whether a free-text block
contains structured data (CSV, JSON, Markdown tables) so it can apply appropriate
masking. Constraints:

- **Fast**: runs in a hot path (pre-call hook), latency matters
- **Deterministic**: no ML models
- **Lightweight**: no heavy dependencies
- **Three formats**: CSV, JSON, Markdown tables

The existing `code_guard.py` already handles JSON-in-JSON (via `json_document()`) and
uses `pygments` + `DATA_EXTENSIONS` to distinguish code files from data files. This
research evaluates what to add for **content-level** detection (is this text block a
CSV/JSON/Markdown table?) vs the existing **path-level** detection (is this a .csv file?).

## Comparison

| Approach | Accuracy | Performance | Dependencies | Distinguishes data from code? | Notes |
|---|---|---|---|---|---|
| **csv.Sniffer** (stdlib) | Medium — detects dialect, not "is CSV" | Fast (~ms for KB-scale) | None (stdlib) | No | Best for parsing, not detection. `has_header()` is useful but Sniffer can false-positive on prose with commas |
| **pandas.read_csv** | High for well-formed CSV | Slow (~100ms+ for small files, scales with size) | Heavy (pandas + numpy) | No | Overkill for detection. Pulls in numpy. Unsuitable for hot path |
| **Custom CSV heuristics** | High with tuning | Very fast (pure Python, ~µs) | None | No | Best for detection. Count delimiter consistency across lines, check column count uniformity |
| **json.loads()** (stdlib) | Very high — JSON is unambiguous | Very fast (~µs) | None (stdlib) | No | JSON has a strict grammar. `json.loads()` in try/except is the gold standard for detection. Already used in `code_guard.py` |
| **jsonschema** | N/A (validation, not detection) | Fast | Lightweight (jsonschema + referencing) | No | Useful for validating detected JSON against a schema, not for detection itself |
| **markdown-it-py** | High — proper Markdown AST | Medium (~ms for KB-scale) | Lightweight (mdit-py-plugins) | No | Full parser, can extract tables from AST. Heavier than needed for detection-only |
| **mistune** | High — proper Markdown parser | Fast (~sub-ms for KB-scale) | Lightweight (pure Python, no deps) | No | v3 has a proper AST with table support. Lighter than markdown-it-py |
| **Custom Markdown table regex** | Medium-High | Very fast (~µs) | None | No | Detect `| ... |` rows with `|---|` separator. Can false-positive on code with pipe characters |
| **pygments** (already used) | High for code detection | Fast (~ms for lexer lookup) | Already installed (via LiteLLM→rich→pygments) | **Yes** | Already used in `is_code_file()`. Has lexers for JSON, CSV, Markdown but they're for syntax highlighting, not detection |
| **filetype** | Medium — magic number based | Very fast | Lightweight (pure Python, ~100KB) | No | Detects binary formats by magic numbers. CSV/JSON/Markdown are text formats — filetype can't help here |
| **python-magic** | Medium — libmagic bindings | Medium | Heavy (libmagic C library) | No | Same limitation as filetype for text formats. Adds native dependency. Not worth it |

## Detailed Evaluation

### 1. CSV Detection

**`csv.Sniffer`** (stdlib, `csv` module):
- `Sniffer().sniff(text)` detects delimiter, quote char, etc.
- `Sniffer().has_header(sample)` checks for a header row
- **Problem**: Sniffer is designed to *parse* CSV, not to *detect* whether text is CSV.
  It can return a dialect for prose that happens to contain commas.
- **Verdict**: Useful as a secondary signal (e.g., "does this have a consistent delimiter?"),
  not as a primary detector.

**`pandas.read_csv`**:
- Pulls in numpy, pandas — ~100MB+ of dependencies.
- Way too heavy for a pre-call hook.
- **Verdict**: Rejected.

**Custom heuristics** (recommended):
- Check that the text has multiple lines.
- Check that lines have a consistent number of delimiters (comma, tab, semicolon).
- Check that the delimiter appears at least N times per line.
- Check for a header row (first line has different character class than rest).
- **Accuracy**: High with proper tuning. False positives on code with consistent comma
  usage (e.g., function calls) are possible but rare.
- **Performance**: Pure Python, ~µs for KB-scale text.
- **Verdict**: **Recommended** for CSV detection.

### 2. JSON Detection

**`json.loads()`** (stdlib):
- JSON has an unambiguous grammar. If `json.loads()` succeeds and returns a dict/list,
  it's JSON.
- **Accuracy**: Near-perfect. The only false positives are Python literals that happen
  to be valid JSON (e.g., `{"a": 1}` is both valid Python and valid JSON).
- **Performance**: ~µs for KB-scale text. C-accelerated in CPython.
- **Verdict**: **Recommended** — this is the gold standard. Already used in `code_guard.py`.

**`jsonschema`**:
- Validates JSON against a schema. Not a detector.
- Could be used post-detection to check "is this a customer record?" but that's
  a different problem.
- **Verdict**: Not needed for detection.

### 3. Markdown Table Detection

**`markdown-it-py`**:
- Full CommonMark parser. Can extract tables from the AST.
- **Accuracy**: High — proper parsing, not regex.
- **Performance**: ~ms for KB-scale text. Slower than regex but still fast enough.
- **Dependencies**: `mdit-py-plugins` for table support. Lightweight (~200KB).
- **Verdict**: Good if you need to *extract* table data. Overkill if you only need
  to *detect* presence.

**`mistune`**:
- v3 is a complete rewrite with a proper AST. Has table support.
- **Accuracy**: High.
- **Performance**: Faster than markdown-it-py (~sub-ms for KB-scale).
- **Dependencies**: Pure Python, no dependencies. Very lightweight.
- **Verdict**: Good middle ground if you need parsing.

**Custom regex** (recommended for detection-only):
- Match `| ... |` rows with a `|---|` separator row.
- Pattern: lines starting with `|`, containing `|`, with a separator line
  of `|---|---|` or similar.
- **Accuracy**: Medium-high. False positives on code with pipe characters
  (e.g., `a || b`, shell pipes `|`). Can be mitigated by requiring the
  separator row.
- **Performance**: ~µs.
- **Verdict**: **Recommended** for detection-only. Use a parser only if you
  need to extract cell values.

### 4. General "Data File" Detection

**`pygments`** (already in use):
- Has lexers for JSON (`JsonLexer`), Markdown (`MarkdownLexer`), and CSV
  (`CsvLexer` — actually a "csv" lexer exists for RFC 4180).
- **But**: Pygments lexers are for *syntax highlighting*, not detection.
  A lexer will tokenize any text, not tell you "this is CSV."
- **Current use**: `is_code_file()` uses `find_lexer_class_for_filename()`
  to check if a *filename* has a known lexer. This is path-level detection,
  not content-level.
- **Verdict**: Keep using for path-level code-vs-data. Not suitable for
  content-level detection.

**`filetype`**:
- Detects file types by magic numbers (binary signatures).
- CSV, JSON, Markdown are text formats with no magic numbers.
- **Verdict**: Not useful for these formats.

**`python-magic`**:
- Bindings to libmagic. Same limitation as filetype for text formats.
- Adds a native C dependency.
- **Verdict**: Not useful, not worth the dependency.

### 5. Distinguishing Data from Code

This is the hardest problem. A CSV and a Python function call can look similar:

```
# CSV
name,email,city
Jean,jean@acme.fr,Lyon

# Python
name, email, city = "Jean", "jean@acme.fr", "Lyon"
```

**Approaches**:

1. **Path-based** (already implemented): Check file extension. `.csv` → data, `.py` → code.
   This is what `is_code_file()` does. Works when you have a file path.

2. **Content-based heuristics**:
   - **JSON**: Unambiguous. If `json.loads()` succeeds, it's JSON (data).
   - **CSV**: Check for consistent delimiter count across lines. Code rarely has
     consistent comma counts across multiple lines.
   - **Markdown table**: Check for `|---|` separator row. This is very specific
     to Markdown tables and won't appear in code.
   - **Pygments lexer**: Try lexing the text as a known data format. If it
     tokenizes cleanly as CSV/JSON/Markdown, it's likely data. But this is
     unreliable — Pygments will lex anything.

3. **Hybrid** (recommended):
   - Use `json.loads()` for JSON detection (deterministic, unambiguous).
   - Use delimiter-consistency heuristics for CSV.
   - Use separator-row pattern for Markdown tables.
   - Use Pygments for code detection (already done).
   - If text matches a data pattern AND doesn't match a code pattern → data.
   - If text matches a code pattern AND doesn't match a data pattern → code.
   - If both or neither → use path-based fallback (already implemented).

## Recommendation for LeakGuard

### Architecture

```
Text block
  │
  ├─ Is it JSON? → json.loads() → YES → mask as JSON
  │
  ├─ Is it a Markdown table? → regex for |---| separator → YES → mask as Markdown table
  │
  ├─ Is it CSV? → delimiter consistency heuristic → YES → mask as CSV
  │
  ├─ Is it code? → pygments lexer (already implemented) → YES → don't mask
  │
  └─ Fallback → mask as plain text (current behavior)
```

### Specific Recommendations

| Format | Method | Library | New deps? |
|---|---|---|---|
| **JSON** | `json.loads()` in try/except | stdlib `json` | No |
| **Markdown table** | Regex: `^\|.*\|$` lines + `^\|[-:]+\|$` separator | stdlib `re` | No |
| **CSV** | Delimiter consistency: ≥3 lines, same delimiter count, ≥2 fields | stdlib | No |
| **Code** | Pygments lexer lookup (already implemented) | pygments (already installed) | No |

### Why no new dependencies

- **JSON**: stdlib `json` is perfect. No reason to add anything.
- **Markdown tables**: A regex for the separator row (`|---|---|`) is sufficient
  for detection. If LeakGuard later needs to *extract* cell values, add `mistune`
  (lightweight, pure Python) — but detection doesn't need it.
- **CSV**: Custom heuristics are fast, deterministic, and dependency-free.
  `csv.Sniffer` can be a secondary signal but isn't needed as a primary detector.
- **Code vs data**: Already handled by `is_code_file()` with Pygments.

### What to avoid

- **pandas**: Too heavy for a pre-call hook.
- **markdown-it-py**: Only needed if extracting table data, not for detection.
- **python-magic / filetype**: Useless for text formats.
- **ML models**: Violates the deterministic requirement.

### Performance estimate

All recommended methods are pure Python / stdlib and run in ~µs for KB-scale text.
For a typical Claude Code text block (1-10KB), total detection latency should be
<1ms. This is negligible compared to the NER analysis that follows.

### Integration with existing code

The existing `code_guard.py` already has:
- `json_document()` — detects and parses JSON (used for JSON-in-JSON)
- `is_code_file()` — path-based code detection with Pygments
- `DATA_EXTENSIONS` — known data file extensions

The new structured data detection layer should:
1. Add a `detect_structure(text)` function that returns `"json"`, `"csv"`,
   `"markdown_table"`, or `None`.
2. Call it in `mask_texts()` before NER analysis, to tag the block type.
3. Use the tag to inform masking strategy (e.g., for CSV, mask each field;
   for JSON, mask string values; for Markdown tables, mask cell contents).
4. Keep `is_code_file()` for path-based decisions (Read tool results).
5. Use `detect_structure()` for content-based decisions (Bash results, MCP results,
   user text).

## Open Questions

1. **Should detection be per-block or per-line?** Grep results mix many files.
   The existing `_restore_code_lines()` handles per-line attribution for code.
   Structured data detection might need per-line detection too (e.g., a Grep
   result that contains a CSV line among code lines).

2. **How to handle nested structures?** JSON containing CSV in a string value
   (already handled by `_decode_json()`). CSV containing JSON in a cell?
   Markdown table containing JSON in a cell? These edge cases need decisions.

3. **Threshold tuning for CSV heuristics**: How many lines? How many delimiters?
   These need empirical tuning against the bench corpus.

## Sources

- Python stdlib `csv` module: `csv.Sniffer` — https://docs.python.org/3/library/csv.html#csv.Sniffer
- Python stdlib `json` module: `json.loads()` — https://docs.python.org/3/library/json.html
- Pygments: `find_lexer_class_for_filename` — https://pygments.org/docs/api/#pygments.lexers.find_lexer_class_for_filename
- mistune v3: https://mistune.readthedocs.io/en/latest/
- markdown-it-py: https://markdown-it-py.readthedocs.io/
- filetype: https://github.com/h2non/filetype.py
- Existing project code: `guardrail/code_guard.py`, `guardrail/test_code_guard.py`
