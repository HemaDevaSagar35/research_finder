"""Query-time reasoning stages (component 8 onward of the research path
generator). Currently: the Cross-Paper Reasoner.

    reasoning.schemas       input / internal / output contracts + validators
    reasoning.evidence      PaperStore, paper_card, evidence candidates, bundles
    reasoning.budget        attempt counter and token estimates
    reasoning.retrieval     optional ranking adapter over indexing.search
    reasoning.cross_paper   the reasoner and its CLI
    reasoning.fixtures      synthetic corpus + landscape for offline runs/tests

Design and implementation notes: docs/cross_paper_reasoner.md.
"""
