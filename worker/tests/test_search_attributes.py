import re
from pathlib import Path

from agentic_ops_worker import search_attributes as sa

ADMIN = Path(__file__).resolve().parents[2] / "phase0" / "shim" / "admin.py"


def test_same_as_registered_by_admin() -> None:
    """phase0/shim/admin.py --search-attributes registers these in ewkit-production."""
    registered = dict(re.findall(r'"(\w+)": IndexedValueType\.INDEXED_VALUE_TYPE_(\w+)', ADMIN.read_text()))
    assert registered == {key.name: key.indexed_value_type.name for key in sa.ALL}
