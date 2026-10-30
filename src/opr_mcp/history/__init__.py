"""Version history for Forge army books and core rules.

The Army Forge API only ever serves the *current* revision of a book
(there is no history endpoint and version query params are ignored), so
every distinct revision the sync observes is captured locally into
``book_snapshots`` / ``core_rule_snapshots`` and can be exported as a
seed bundle for new installs.

* :mod:`.normalize` — strip volatile fields + content hashing.
* :mod:`.store`     — record / list / resolve snapshots.
* :mod:`.diff`      — pure structural diffs between two payloads.
* :mod:`.views`     — render snapshot payloads in the live tools' row shape.
* :mod:`.bundle`    — gzip-NDJSON export / import / URL seeding.
"""
