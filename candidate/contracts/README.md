# Fast installer contracts v1

These contracts are the shared interface for the fast installer. Builders must treat this directory as read-only. A change requires a maintainer to revise the contract and its digest before affected implementations continue.

| Contract | Authority |
| --- | --- |
| C1 | [Target profile](C1-target-profile.md) |
| C2 | [Media and package lock](C2-media.md) |
| C5 | [Run binding](C5-run-binding.md) |

The schemas describe JSON data, not permission to run an installer. Implementations must enforce the semantic requirements in these documents as well as structural validation. Unknown fields, duplicate JSON keys, non-finite numbers, symbolic-link inputs, path traversal, partial data and unsupported schema versions must fail closed.

All hashes are lowercase SHA-256 hex unless explicitly identified otherwise. JSON uses UTF-8. Signed JSON is the exact serialized file, not a reparsed representation. Identifiers use lowercase ASCII letters, digits and hyphens, start with a letter and have at most 64 characters. Timestamps use RFC 3339 UTC. Paths inside media are relative POSIX paths without empty components, dot components, backslashes, control characters or whitespace.

Shipped source, profiles, fixtures and media must contain no personal identity, private host path, account identifier, credential, internal report or runtime history. Host configuration is separate and private. A test pass never substitutes for native installation evidence. A failed or interrupted install cannot resume or automatically retry; a new install uses a fresh target and a new run ID.
