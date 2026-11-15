"""?? ?? ??? ?? DB? ?? ???? ?? ???? ????."""
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from scripts.catalog_sample import _local_simulation


def sample_session(host="127.0.0.1", database="be_simulation", options="-csearch_path=simulation_0123456789", driver="postgresql"):
    """DB? ???? ?? ?? ?? ??? ?? ?? ??? ????."""
    url = make_url(f"{driver}://test@{host}/{database}").update_query_dict({"options": options})
    return SimpleNamespace(get_bind=lambda: SimpleNamespace(url=url))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_explicit_loopback_simulation_schema_is_allowed(host):
    """??? loopback? ?? ?? ???? ??? ???? ??."""
    _local_simulation(sample_session(host=host))


@pytest.mark.parametrize("kwargs", [
    {"host": "database.example.com"}, {"host": "127.0.0.1.example.com"},
    {"database": "production"}, {"database": "be_test"},
    {"options": "-csearch_path=public"},
    {"options": "-csearch_path=simulation_0123456789,public"},
    {"options": "-csearch_path=simulation_0123456789 -csearch_path=public"},
    {"options": "-csearch_path=simulation_0123456789_bad"},
    {"options": "-csearch_path=simulation_0123456789\n"},
    {"options": "-csearch_path=simulation_"}, {"driver": "mysql"},
])
def test_other_databases_and_extra_schema_options_are_rejected(kwargs):
    """??? ???? ?? search_path? ??? ?? ???? ?? ??? ??."""
    with pytest.raises(ValueError, match="isolated local simulation schema required"):
        _local_simulation(sample_session(**kwargs))
