"""Resolve an extraction's evidence_quote back to a real record and its
source_locations, using the paper's matched_records from retrieval.

landscape.extraction already checks evidence_quote is a literal substring of
the PaperCard text it was shown (the LLM-trust boundary). This is a separate,
code-only step: given that same quote and the paper's matched_records (which
carry real source_locations, now available via research.retrieval's
RetrievedRecord -- see landscape/builder.py's build_landscape_for_query),
find which specific record the quote most likely came from.

This is a best-effort lookup, not a guarantee -- a paper's PaperCard field
can state something that the retrieval step's matched_records never
surfaced (retrieval found the paper for a different reason). No match means
no match: callers get an empty list, never a fabricated location.
"""


def resolve_evidence(quote: str, matched_records: list[dict]) -> tuple[list[str], list[dict]]:
    """Return (record_ids, source_locations) for every matched_record whose
    text overlaps with `quote`. Matching is substring containment, either
    direction, case-insensitive -- lenient because record text may carry a
    title prefix or differ in trailing punctuation from the PaperCard
    rendering the quote was copied from (landscape.extraction._card_to_text).
    """
    if not quote or not matched_records:
        return [], []
    needle = quote.strip().casefold()
    if not needle:
        return [], []

    record_ids: list[str] = []
    source_locations: list[dict] = []
    seen_locations: set[str] = set()

    for record in matched_records:
        text = (record.get("text") or "").casefold()
        if not text:
            continue
        if needle in text or text in needle:
            record_id = record.get("record_id")
            if record_id:
                record_ids.append(record_id)
            for location in record.get("source_locations") or []:
                key = repr(sorted(location.items())) if isinstance(location, dict) else repr(location)
                if key not in seen_locations:
                    seen_locations.add(key)
                    source_locations.append(location)

    return record_ids, source_locations
