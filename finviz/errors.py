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
    """Raised when no FinViz token is set at all.

    Kept distinct from FinvizTokenError only to name the cause; both are
    reported to the user the same way. A missing token is NOT a benign state:
    FinViz supplies the consolidated 1-minute bars that scale the tick stream
    (roughly a tenth of the tape) up to real traded volume, so without it every
    volume and CVD figure on the chart is far too low while still looking
    plausible. Silently carrying on would misrepresent the data, so the app
    surfaces this in the UI rather than only in the log.
    """
    pass
