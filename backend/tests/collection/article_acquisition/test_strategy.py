"""ソースのlookupと、補完方針参照の副作用を検証する。"""

from unittest.mock import patch

from app.collection.article_acquisition.strategy import SOURCES
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.registry import completion_policy_for
from app.collection.sources.source_name import SourceName


def test_source_names_dispatch_round_trip() -> None:
    for key, source in SOURCES.items():
        assert source.name == key
        assert SOURCES[SourceName(str(key))] is source


def test_completion_policy_lookup_does_not_construct_readers() -> None:
    with patch.object(ReaderTools, "__init__", side_effect=AssertionError("I/O setup")):
        for key, source in SOURCES.items():
            assert completion_policy_for(key) is source.completion_policy
