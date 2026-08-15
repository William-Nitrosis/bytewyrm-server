from typing import Any

from pydantic import RootModel


class StoreRecord(RootModel[dict[str, Any]]):
    """A raw JSON object validated against the authenticated Project's Store schema."""
