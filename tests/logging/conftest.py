from collections.abc import Iterator

import pytest
from aiohttp.log import access_logger

from aiohttp_tiny_mcp.access_log import TOKEN_QUERY_FILTER


@pytest.fixture
def restore_access_filters() -> Iterator[None]:
    filters = list(access_logger.filters)
    access_logger.removeFilter(TOKEN_QUERY_FILTER)
    try:
        yield
    finally:
        access_logger.filters[:] = filters
