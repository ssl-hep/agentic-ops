"""Custom search attributes of the production workflows.

Registered in namespace ewkit-production by phase0/shim/admin.py --search-attributes; the two
lists must agree (tests/test_search_attributes.py checks it).
"""

from temporalio.common import SearchAttributeKey

PRODUCTION_ID = SearchAttributeKey.for_keyword("ProductionId")
DATASET = SearchAttributeKey.for_keyword("Dataset")
DATASET_STATE = SearchAttributeKey.for_keyword("DatasetState")
UNITS_DONE = SearchAttributeKey.for_int("UnitsDone")
UNITS_TOTAL = SearchAttributeKey.for_int("UnitsTotal")
FILES_WITHOUT_REPLICA = SearchAttributeKey.for_int("FilesWithoutReplica")
NEEDS_DECISION = SearchAttributeKey.for_bool("NeedsDecision")

ALL = (PRODUCTION_ID, DATASET, DATASET_STATE, UNITS_DONE, UNITS_TOTAL, FILES_WITHOUT_REPLICA, NEEDS_DECISION)
