Fixtures copied into a throwaway server root by the tests.

`v1/` is a pre-containers (schema 1) app root: items with free-text `location`
strings, a split item, an unfiled item, an undelivered item, a pending duplicate,
one processed and one unprocessed capture sidecar. `tests/test_migration.py`
checks it migrates to containers.
