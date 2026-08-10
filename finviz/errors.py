"""FinViz exception types, kept in their own module on purpose.

finviz/new_finviz.py opens a MongoClient and builds an index at import time, so
importing it just to reference an exception class would make the importer wait
on MongoDB. The dashboard needs these types at module scope (to tell a missing
FinViz account apart from a real fetch failure), so they live here where the
import is free.
"""


class FinvizTokenError(Exception):
    """Raised when FinViz rejects the auth token (HTTP 401/403)."""
    pass


class FinvizNotConfigured(Exception):
    """Raised when no FinViz token is set at all (fresh clone, no Elite account).

    Kept distinct from FinvizTokenError: a *rejected* token is a real failure
    worth reporting loudly, whereas "no token configured" is the expected state
    for anyone running the dashboard without a FinViz Elite subscription. The
    app treats this one as "this feed is simply unavailable" rather than an
    error, so the chart still renders from whatever IBKR data exists.
    """
    pass
