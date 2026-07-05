# Phase 2 EDB Probe

Date: 2026-05-04

## Summary

LINE Desktop for Mac stores local data under:

```text
~/Library/Containers/jp.naver.line.mac/Data/Library/Containers/jp.naver.line/Data
```

The likely chat database is (the `qwe…`/`qw…`/`f…` hash filenames throughout
this document are placeholders — the real names are derived per LINE
account/device and will differ on your machine; inspect your own `Data/db/`
directory with `linecrawl edb-doctor`):

```text
Data/db/qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb
Data/db/qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb-wal
Data/db/qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb-shm
```

The `.edb` files are not plain SQLite. `sqlite3` reports `file is not a
database`, the header is not `SQLite format 3`, and entropy is approximately
8.0. The WAL is also encrypted/wrapped.

## Local Files Found

Main database candidates:

```text
qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb      48 MB
qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb-wal   4 MB
qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb-shm  32 KB
album_qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb
chatStats_qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb
keep_qweXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.edb
AutoSuggest/qwYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY.edb
```

There is one plain SQLite file:

```text
Data/db/fZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ
```

It contains sticker metadata tables, not chat messages.

## Static Analysis Findings

The LINE binary contains StorageService-related symbols and source paths:

```text
line::StorageMetaInfo
line::StorageCondition
line::StorageIndexInfo
StorageDDLOperator.cpp
StorageMigrator.cpp
StorageOperation.cpp
StorageOperator.cpp
StorageQueryBuilder.cpp
StorageManagerImpl.cpp
StorageMetaInfoManager.cpp
StorageSearchTask.cpp
TalkBizService/src/Service/Chat/ChatMessage.cpp
TalkBizService/src/Service/Chat/ChatServiceImpl.cpp
TalkBizService/src/Service/Chat/ChatSearchServiceImpl.cpp
```

This suggests LINE uses its own shared StorageService abstraction over the
encrypted `.edb` files.

The binary also includes:

```text
UICKeyChainStore
VUTU7AKEUR.jp.naver.line.mac
encryptedKeyChain
decryptKeyChain
hashKeyChain
myE2EEKeyChain
e2eeKeyChain
```

These are probably related to E2EE/Letter Sealing keys. They did not appear as
plain default Keychain generic-password service/account names via `security
find-generic-password`.

## Current Conclusion

Direct `.edb` reading is plausible but not solved in Phase 2. The blocker is
identifying the StorageService encryption key path and page format.

The most likely route is not SQLCipher CLI trial-and-error. The file has no
plain SQLite header, and LINE's own `line::Storage*` layer is present. The next
useful work is dynamic analysis:

1. Attach to LINE and observe `SecItemCopyMatching` / `UICKeyChainStore` calls.
2. Trace file opens and reads for `qwe...edb`.
3. Identify the function that opens/decrypts StorageService databases.
4. Once schema/key are known, implement `linecrawl edb-import`.

## Future Import Bridge Concept

The phrase "future import bridge" means a small translation layer between
LINE's private local storage and the existing `linecrawl` database.

Today, Phase 1 already has a safe and working path:

```text
LINE Desktop Save chat
  -> [LINE] chat.txt in Downloads
  -> linecrawl import-downloads
  -> normalized messages in ~/.linecrawl/linecrawl.db
  -> chats/search/messages/sql
```

The direct `.edb` route, if it becomes possible, should feed the exact same
normalized database instead of creating a second product:

```text
LINE local .edb files
  -> read/extract only the needed chat records
  -> convert LINE's internal fields into linecrawl fields
  -> normalize dates, senders, chat names, attachments, and message text
  -> deduplicate with stable IDs
  -> import into ~/.linecrawl/linecrawl.db
  -> reuse chats/search/messages/sql
```

The bridge has three jobs:

1. Convert: read LINE's internal storage shape and turn it into plain message
   records.
2. Normalize: make those records look the same as messages imported from
   `Save chat` text files.
3. Import: insert only new or changed messages into the existing SQLite
   database without duplicating old data.

The safety rule is that Phase 2 should stay read-oriented. It should not modify
LINE's own files, should avoid printing secrets or raw key material, and should
make snapshots before experimental parsing. If direct `.edb` import works, it
should be an additional route, not a dependency for the already-working
text-export workflow.

## Useful Commands

```bash
./linecrawl.py edb-doctor
file ~/Library/Containers/jp.naver.line.mac/Data/Library/Containers/jp.naver.line/Data/db/*
strings -a -n 6 /Applications/LINE.app/Contents/MacOS/LINE | rg 'Storage|KeyChain|E2EE'
```
