"""Self-authored fictional sources and current-question intents, without gold."""
from .schema import Document, Question


def documents():
    return (
        Document("aster", "synthetic-v1", "Aster fictional player", "Aster | points | 10\nAster | rebounds | 4\n"),
        Document("beryl", "synthetic-v1", "Beryl fictional player", "Beryl | points | 20\nBeryl | rebounds | 7\n"),
    )


def related():
    return Question("R", "How many points do Aster and Beryl have?", ("Aster", "Beryl"), "points")


def target():
    return Question("Q", "How many rebounds do Aster and Beryl have?", ("Aster", "Beryl"), "rebounds")
