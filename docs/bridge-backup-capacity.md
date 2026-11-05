# DEV backup capacity and restoration plan

This is a proposed capacity plan, not an executed cleanup or backup. The final
read-only cleanup audit below found unresolved execution and recovery safeguards;
the frozen inventory alone is not sufficient to authorize safe deletion. Measurements
were taken on 2026-11-04 between 21:45 and 22:17 UTC. All live checks were read-only.
No pre-existing server files were removed, services restarted, credentials transferred,
storage purchased, volumes mounted, or live journals compacted.

## Measured scope

The previous 6.37 GB backup-plus-restore estimate covered only bridge storage. A
complete application recovery also needs the separate support media directory,
MariaDB, the actual configured Redis instance, and private configuration.

| Component | Measured bytes / facts |
| --- | ---: |
| Root filesystem capacity | 102,888,095,744 |
| Root free at 21:45 UTC | 4,036,890,624 |
| Root free at 21:56 UTC | 3,936,112,640 |
| Root free at 22:02 UTC | 3,944,304,640 |
| Root free after synthetic restore at 22:14 UTC | 3,914,686,464 |
| Root free at final live check, 22:17 UTC | 3,907,112,960 |
| Available inodes at 21:45 UTC | 11,252,179 |
| Linked-device tree, allocated including directories | 4,124,958,720 |
| Support-media tree, allocated including directories | 1,942,683,648 |
| Combined application trees, allocated | 6,067,642,368 |
| Linked-device regular files | 18,093 / 4,085,843,892 logical bytes |
| Support-media regular files | 57,082 / 1,648,243,557 logical bytes |
| Read-only tar stream of both trees, including metadata | 5,913,016,320 |
| Same stream, gzip level 6 | 3,702,290,031 |
| Application MariaDB data / indexes | 174,309,376 / 344,768,512 |
| Entire existing MariaDB directory allocation | 859,385,856 |
| Application SQL dump / gzip level 6 | 212,650,364 / 39,960,152 |
| Actual Redis memory / dataset memory | 23,899,664 / 21,280,064 |
| Actual Redis RDB file, logical | 11,139,749 |
| Existing release, including dependencies, allocated | 344,236,032 |
| Private application configuration | 3,009 logical bytes |

The tar size measurement streamed into byte counters and completed in 221.946
seconds with exit 0 and no warnings. The SQL measurement used
`mariadb-dump --single-transaction --skip-lock-tables --routines --events --triggers
--hex-blob --databases ...`, streamed into counters, and completed in 11.097
seconds with exit 0 and no stderr. Neither stream was saved or transferred.
These are sizing observations, not consistent cross-service backups. The trees
were measured at different instants while live writes continued.

There is no separate mounted persistent data volume. `/tmp`, live application
state, releases and `/var/backups` share the root filesystem. `/boot`, EFI and
tmpfs are not suitable backup destinations.

The application and shipping database configuration names are identical: count
one schema, not two. It contains 31 InnoDB tables. All 38 observed database client
sockets belonged to `shipment-bot.service`; there were no scheduled database
events and the event scheduler was off.

**Redis scope correction:** both the running API environment and its environment
file use `127.0.0.1:6379/0`, owned by `redis-server.service` (PID 1582909 at inspection).
`shipment-redis.service` is a different process (PID 1582393). Backing up the latter
does not protect the application's current Redis state. Four observed Redis
client sockets belonged to the API; the fifth was this read-only inspection.
No other clients were observed, which does not establish exclusive ownership
forever. Never stop, flush, or overwrite the whole Redis service as part of
ordinary bridge rollback. AOF is disabled; an older RDB alone is not a fresh
cutover snapshot.

## Practical capacity solution requiring approval

There are 95 release directories. The bridge/current symlink uses
`20261104-dev-v19-session-journal-2a6a225`, while the running API still has its
working directory and mappings in `20261103-dev-v19-quick-select-6163be9`.
**Retain both complete releases**, including their virtual environments and
Node dependencies. Keep the existing backups, all application state, media,
configuration, historical source, lockfiles and test/recovery evidence.

The concrete reclamation candidates are only these regenerable artifacts:

| Approval scope | Conservative reclaimable bytes |
| --- | ---: |
| `venv` and `whatsapp_bridge/node_modules` under the 93 historical releases dated 20261012 through 20261102 inclusive | 15,325,331,456 |
| `/root/.npm/_cacache` | 572,776,448 |
| `/root/.cache/pip` | 116,006,912 |
| `/var/cache/apt/pkgcache.bin` and `/var/cache/apt/srcpkgcache.bin` | 126,644,224 |
| `/root/.cache/puppeteer/chrome` unused full-browser download | 397,279,232 |
| Total proposed reclaim | **16,538,038,272** |

The dependency scope is 186 real directories. The release calculation counts a
regular-file or symlink inode only if every hard link is inside the candidate
set; links retained elsewhere count as zero. Directory inode space is excluded,
so it does not inflate the estimate. Simply summing release directory sizes
would overstate reclaimed space because historical releases share hard links.
No `.env`, `auth.json`, `auth.json.journal`, `state.json.journal` or `lines.json`
filenames were found inside these dependency trees. Process working directories,
executables and mappings, release symlinks, systemd/nginx configuration and
application deployment links were inspected for references. Temporary-directory
symlinks reference only the retained `2a6a225` dependencies. No running process
executable or memory mapping referenced the optional full-browser download.
Retain `/root/.cache/puppeteer/chrome-headless-shell`, which the tests use.
The filename check is screening evidence, not proof that arbitrary package
contents cannot contain private data. Keep any unexpected local state or
locally authored files found during the final dependency provenance review;
never broaden this selection to release source, application data or temporary
test directories.

This is not authorization to delete the paths. Before an approved cleanup:

1. Revalidate the exact 186-directory selection in the appendix, the three named
   cache trees and the two exact apt cache files against the private manifest;
   reject symlinked roots, paths outside the listed parents, mountpoints, or any
   changed dependency links. Recheck process references and active build/install
   jobs. Add any newly referenced release to the retained set and recalculate.
2. Verify current and API release file hashes and dependency verification, and
   retain their source, package lockfiles and complete runtime environments.
   Do not remove browsers needed for the final regression run.
3. Obtain explicit approval for that manifest. Remove only approved dependency
   and cache entries, without following symlinks. Do not use broad release,
   `/tmp`, cache-parent or backup-directory deletion patterns.
   Preserve apt archive/partial directories and locks.
   Unlink selected directory entries only: never truncate, overwrite, shred or
   otherwise mutate a shared hardlinked inode retained by another release.
4. Synchronize affected parent directories. Measure actual free bytes and inodes
   again; inode estimates are not proof that filesystem space has been reclaimed.
   Recheck all eight sessions and both active release hashes.
5. Admit deployment preparation only with at least **20,000,000,000 bytes free**
   and a fresh recalculation of the budget below. If growth or references prevent
   this floor, stop and select additional approved capacity instead.

Using the 22:17 free-space sample, the candidate set would produce approximately
20,445,151,232 bytes free before subsequent growth, only 445 MB above the
20 GB admission floor. This is a measured approval
option, not a claim that capacity has already been recovered. Incremental storage
purchase cost is **zero**. Historical releases whose dependencies are removed
will require dependency installation before they can run again; the two active
releases and the required `2a6a225` rollback remain immediately runnable.

The device/inode manifest is already frozen on DEV at
`/tmp/whatsapp-capacity-preflight-t1v2j75c/reclaim-manifest-v2.json`
(0700 parent, 0600 file), SHA-256
`5035bab549e21717eacf8d8a45df42737ec829e0ad27b1c508fe8a10b1527d9e`.
It records 186 dependency roots, three cache roots and two apt cache files.
Recheck identities and use a fresh process-reference audit before an approved
removal. Directory metadata necessarily changes if another operation installs
dependencies; such changes require review, not silently broadening the scope.

Additional measured but **unselected** options are 223,129,600 bytes of
regenerable apt index lists and 115,474,432 bytes of historical source `.pyc`
bytecode (4,891 files outside retained releases and dependency directories).
They would require their own exact approval manifest. The 3.81 GB snap download
cache is deliberately excluded: every cached file is hardlinked to retained
installed snaps, so unlinking cache names would reclaim zero measured bytes.

A separate persistent backup volume remains preferable for long-term capacity,
and an encrypted off-host copy additionally protects against host loss. No
approved destination, recipient/key or purchasable volume was identified in the
current environment. Those alternatives require separate approval, provider
selection and current pricing; no price or storage purchase is assumed here.

## Admission budget for this measured dataset

Use streaming compression/encryption and streaming restoration so there is no
second complete decrypted archive on disk. Retain the encrypted backup while
the entire restored copy is verified. Preserve previous backups as already
allocated space; this table is **additional** required free capacity.

| Simultaneous allocation / reserve | Bytes | Basis |
| --- | ---: | --- |
| Encrypted application, SQL, Redis and configuration archives | 4,294,967,296 | 4 GiB; measured compressed payload about 3.754 GB before small metadata/encryption overhead |
| Complete restored linked-device and support-media trees | 6,442,450,944 | 6 GiB; current allocated trees about 6.068 GB |
| Isolated MariaDB restore, including indexes and temporary files | 1,073,741,824 | 1 GiB planning allowance; existing directory is 859 MB, actual restored peak has not been measured |
| Isolated Redis restore files | 67,108,864 | 64 MiB; current RDB 11.14 MB |
| New release and its private dependencies | 536,870,912 | 512 MiB; current release 344 MB |
| Single checkpoint worker disk workspace | 805,306,368 | 768 MiB; 128 MiB snapshot cap plus concurrent journal suffix and margin |
| Additional live growth / cutover allowance | 536,870,912 | 512 MiB |
| Untouched 5% root-filesystem reserve | 5,144,404,788 | Greater than the default 2 GiB absolute warning reserve |
| Required measured-plan total | **18,901,721,908** | About 17.60 GiB |
| Operational admission floor | **20,000,000,000** | About 18.63 GiB; includes further margin |

The rounded caps are operational admission limits, not guarantees of future
compression, database index growth or journal traffic. Re-measure immediately
before the cutover. Abort preparation before any cap or reserve is violated;
never reclaim authoritative records to finish a backup. If verification needs a
second restored tree, a persisted plaintext tar, a second retained fresh backup,
extra SQL scratch space or a longer retention window, add that allocation first.
In particular, the live MariaDB directory size does not prove that a freshly
restored instance or its peak index-building scratch space fits 1 GiB. The real
restore must establish that before deployment; the SQL export byte measurement
and synthetic encryption test do not validate database restoration.

The critical 1%/512 MiB threshold is an emergency admission threshold, not an
acceptable backup safety reserve. Inode headroom currently exceeds 11 million;
monitor it during release extraction and restoration as well.

## Consistent encrypted backup and restore procedure after approval

1. Record eight-line status, pairing, all 17 store sequences, callback IDs,
   pending/uncertain-send identities, delivery evidence, database claims and
   idempotency records. Quiesce outgoing work, gracefully stop the API and bridge,
   and require successful drains with no unresolved authentication fences. Check
   that no other application writer appeared. Keep MariaDB and Redis running.
   Stop/start actions require the separate deployment approval.
2. Create a new private backup directory with `umask 077`, directory mode 0700
   and artifact mode 0600. Record versions, stable source manifests and hashes.
   Archive the complete linked-device and support-media trees, private
   configuration and required deployment metadata; omit no journals, snapshots,
   fences, quarantines or delivery evidence. Export the one InnoDB schema using
   the measured single-transaction dump flags.
3. Capture a fresh RDB from the **configured redis-server instance** while
   application writers are quiesced, using an approved persistence operation and
   checking its completion. The RDB may include other namespaces; protect it as
   private data and restore only into an isolated instance for verification.
   Do not change the application's Redis URL, issue FLUSH commands, stop a shared
   instance, or reuse the unrelated shipment-redis backup.
4. Stream each archive/export through compression and GPG AES-256 encryption.
   Use an operator-approved recipient with independently held recovery material,
   or an operator-entered strong passphrase through a secure prompt/file
   descriptor. Never place secrets in command arguments, shell history, logs,
   source or committed files. Key custody and the actual backup recipient remain
   approval gates; the synthetic test key is never a production recipient.
5. Check every pipeline exit code; hash ciphertext, fsync each completed file,
   rename its pending name, fsync the backup directory, and atomically finalize a
   manifest only after every component succeeds. Do not call a collection of
   separately captured live files a consistent backup.
6. Verify decryption/integrity to a sink first. Restore from the unchanged
   encrypted artifacts into a separate 0700 directory using pipelines that fail
   if *any* stage fails. A decryptor can emit plaintext before reporting a bad
   integrity code; therefore treat the restored tree as untrusted until all
   stages and hashes succeed. Avoid a second decrypted archive on disk. Compare
   every source/restored file hash, size and metadata, then validate all 17
   stores and materialized state with both new and deployed readers. No provider
   sockets or WhatsApp pairings may be started from copied credentials.
7. Restore SQL into an ephemeral MariaDB data directory/socket, with networking,
   binary logging and the event scheduler disabled. Check schema, indexes,
   routines/triggers, row counts and ordered data digests, especially message
   claims, delivery evidence and idempotency records. Restore Redis into an
   ephemeral Unix-socket-only instance; verify key types, values and absolute
   expiration semantics. Keys that expire while verifying must not be made
   permanent or resurrected. Never connect the application to these instances.
8. Retain the fresh encrypted backup and verification manifest through deployment
   and the observation window. Ordinary code rollback preserves the **current**
   bridge/database/Redis state. Do not restore the older backup, flush Redis or
   bypass unresolved authentication fences to force reconnection. Any data
   restoration is a separate coordinated recovery decision.

## Verification actually completed

GnuPG 2.4.4 is installed; no package installation was needed. A synthetic-only
RSA-3072 recipient and 1 MiB random fixture were created in the private DEV
directory `/tmp/whatsapp-backup-crypto-probe-x7ieot2c`. AES-256 encryption, full tar
restoration and SHA-256 equality succeeded. Deliberate ciphertext modification
caused decryption failure. The directory was 0700 and encrypted artifact 0600.
No live session, database row, customer media or private configuration was used
in this test. The test artifacts were retained; nothing was deleted.

A separate synthetic-only MariaDB and Redis encrypted export/restoration passed
at 22:14:23 UTC in 7.607 seconds. The harness is
`/tmp/bridge_synthetic_backup_restore.py`; its private retained evidence is
`/tmp/whatsapp-synthetic-backup-restore-p52ktiz4/report.json` on DEV.

| Isolated check | Result |
| --- | --- |
| MariaDB restored rows | 256 messages and 256 receipts; three tables passed `CHECK TABLE` |
| MariaDB content/schema verification | Ordered row/BLOB/JSON, receipt, audit, index, constraint, trigger and stored-routine digests matched |
| MariaDB transaction coverage | Trigger audit and a rolled-back transaction were preserved through restoration |
| Redis restored values | 69 surviving keys from 70 exported; string, hash, list, set, sorted-set and stream types matched |
| Redis expiration coverage | Absolute expiry timestamps matched; the one expired key was not resurrected |
| Encryption/integrity | Both AES-256 export artifacts restored exactly and rejected deliberately modified ciphertext |
| Permissions | Test root 0700; encrypted SQL and Redis artifacts 0600 |
| Peak / retained allocated test disk | 103,038,976 / 90,476,544 bytes, conservatively including directory accounting |
| Isolation | Private Unix sockets only; no live configuration imports, live database connections, customer data or network listeners |
| Cleanup of processes | Both temporary MariaDB instances, both temporary Redis instances and the test GPG agent stopped; artifacts retained |

MariaDB used 32 MiB buffer pools and 16 MiB redo files, with the source stopped
before starting the restored instance. The harness had a 280 MiB disk guard,
which did not trigger. This verifies the restoration procedure with small
synthetic fixtures. It does not establish the real 31-table restored database
size, peak index-building space, or fresh production-data recovery correctness.

The fresh real-data backup, full MariaDB/Redis restore, operator recovery key
exercise and approved reclamation have **not** been executed. Until those gates
are satisfied, deployment remains **NO-GO on capacity/backup readiness**, even
if the startup/shutdown code correction and isolated regressions pass.

## Final read-only cleanup safety audit — 2026-11-05 Beirut time

The frozen manifest and all 191 root identities matched during the audit. The
186 dependency paths matched the appendix hash. A no-follow scan inspected
1,231,002 entries; hardlink accounting and separate cache directory accounting
confirmed 16,538,038,272 reclaimable allocated bytes. This is a prospective
calculation, not proof of free capacity after deletion. No files were removed.

No active target references were found among 185 processes, 11,705 mapped paths,
2,618 open descriptors, 25 process mount namespaces, or 25,456 external symlinks.
Retained release entrypoints and inspected deployment scripts did not reference
the targets. Nineteen failed, inactive historical test units reference eight
selected virtual environments; they have no observed automatic restart or
trigger relationship and would need dependencies restored before reuse.

All 93 releases retain source, requirements and Node lockfiles. Installed Node
versions match those locks, and all 558 checked original Baileys source hashes
match. Verification of 466,755 hashed Python package RECORD entries found no
content mismatches. No authoritative state was identified in dependency trees.
However, retained Python requirements omit exact versions for six to nine
installed distributions per environment, and artifact availability has not been
verified. Complete historical reconstruction therefore requires preserving
additional installed-version and artifact provenance. Opaque pip HTTP cache
bodies also prevent a categorical claim that all cached content is redundant.

**Capacity cleanup remains NO-GO as presently specified.** The manifest freezes
only root path/device/inode/mode/mtime, not descendants or ancestor identities.
Some ancestors are writable by non-root owners, and apt timers can start cache
writers after inspection. There is no reviewed cleanup executor enforcing the
prose prechecks, confinement or automatic aborts. An earlier path/process/mount
check followed by recursive pathname deletion does not resolve these races.

Before cleanup approval, preserve the missing recovery provenance and review a
complete descendant/ancestor inventory plus a bounded deletion executor. It must
exclude concurrent deployment/install/cache writers, use descriptor-relative
no-follow traversal without crossing mounts, unlink entries without modifying
shared hardlinked contents, and abort on every mismatch, unexpected entry or
operation failure. Validate these controls against isolated path replacement,
symlink, mount, hardlink and failure scenarios. Measure free bytes/inodes again
immediately before approved cleanup and after directory synchronization. A
changed selection requires renewed review; never silently broaden its scope.

A later read-only deployment precheck at 01:30 Beirut found only
3,804,790,784 bytes free and no separate persistent backup volume. The manifest
file hash remained unchanged, but both apt cache files had changed modification
times following scheduled apt activity; only 189 of 191 root identities still
matched. The frozen cleanup must therefore abort pending a new reviewed
inventory. No fresh complete backup with full-scope restore verification was
identified. Commit/PR readiness does not remove these deployment blockers.

## Frozen historical dependency selection

For each basename below, the only candidate children are `venv` and
`whatsapp_bridge/node_modules` under `/opt/whatsapp-support/releases/`.
This appendix does not authorize deleting the release directory itself.
Sort basenames as shown, emit the absolute `venv` path followed by the absolute
`whatsapp_bridge/node_modules` path for each, and separate all 186 paths with LF,
including a final LF. The SHA-256 of that exact selection is
`1bb7e5273691d253c7cd329855bc214f29c35f425871818bab66fdd6f19c5fd9`.
An unexpected path, symlink, process reference, inode change or differing
selection hash requires re-evaluation before any approved removal.

```text
20261012-dev-v19
20261012-dev-v19-auth-loading
20261012-dev-v19-edit-history
20261012-dev-v19-refresh-navigation
20261012-dev-v19-reload-network
20261012-dev-v19-repeat-edit-ui
20261012-dev-v19-shared-history-app-sync
20261012-dev-v19-shared-history-visible
20261012-dev-v19-ticket-history
20261012-dev-v19-ticket-history-number-check
20261014-dev-v19-number-alias
20261016-dev-v19-copy-supervisor
20261016-dev-v19-supervisor-selection
20261016-dev-v19-supervisor-selection-v2
20261017-dev-v19-claim-to-mine
20261017-dev-v19-fast-open
20261017-dev-v19-fast-open-2
20261017-dev-v19-staff-delete
20261017-dev-v19-status-menu
20261017-dev-v19-ui-redesign
20261018-dev-v19-active-assigned
20261018-dev-v19-chat-delete
20261018-dev-v19-utc-times
20261019-dev-v19-quick-replies
20261019-dev-v19-video-media
20261020-dev-v19-active-managers
20261020-dev-v19-contacts-numbers
20261020-dev-v19-empty-auto-messages
20261020-dev-v19-idle-autoresolve
20261020-dev-v19-media-host-fallback
20261020-dev-v19-no-handoff-message
20261020-dev-v19-phone-app-replies
20261020-dev-v19-queue-scroll
20261020-dev-v19-rename-registered
20261020-dev-v19-ticks-resend
20261021-dev-v19-delivery-lifecycle
20261021-dev-v19-history-customer-search
20261021-dev-v19-linked-sync
20261021-dev-v19-migration-audit
20261021-dev-v19-no-incoming-toast
20261021-dev-v19-reopen-owner
20261021-dev-v19-reopen-ticket
20261021-dev-v19-shared-contacts
20261021-dev-v19-support-hardening
20261022-dev-v19-interrupted-delivery
20261022-dev-v19-local-database
20261022-dev-v19-reaction-recovery-status
20261022-dev-v19-sound-toggle
20261022-dev-v19-uncertain-actions
20261023-dev-v19-agent-statistics
20261023-dev-v19-composer-pdfs
20261023-dev-v19-delivery-read-sync
20261023-dev-v19-meta-removal
20261023-dev-v19-mobile-voice-read
20261023-dev-v19-played-indicator
20261023-dev-v19-staff-names
20261023-dev-v19-staff-pdf
20261023-dev-v19-statistics-alignment
20261023-dev-v19-transfer-stats
20261024-dev-v19-number-lookup
20261024-dev-v19-quick-reply-dedup
20261024-dev-v19-quick-reply-locations
20261024-dev-v19-quick-reply-share
20261025-dev-v19-chat-list-state
20261025-dev-v19-investigation
20261025-dev-v19-manager-review
20261025-dev-v19-mik-support-title
20261026-dev-v19-pdf-sync
20261026-dev-v19-pdf-sync-recovery
20261027-dev-v19-multi-items
20261027-dev-v19-reply-navigation
20261027-dev-v19-reply-navigation-final
20261027-dev-v19-reply-navigation-review
20261028-dev-v19-bridge-recovery
20261029-dev-v19-active-directories
20261029-dev-v19-bridge-performance
20261029-dev-v19-dashboard-queues
20261029-dev-v19-opus-voice
20261029-dev-v19-remove-resume-send
20261029-dev-v19-session-timers
20261029-dev-v19-voice-recording
20261030-dev-v19-album-reactions
20261030-dev-v19-delayed-inbound
20261030-dev-v19-sse-recovery
20261030-dev-v19-voice-zero-start
20261031-dev-v19-line-outage
20261031-dev-v19-relink-restore
20261031-dev-v19-unread-filter
20261031-dev-v19-whatsapp-all-staff
20261101-dev-v19-voice-player-62aec3d
20261102-dev-v19-chat-fixes-2443d5d
20261102-dev-v19-forward-contact-47f9b64
20261102-dev-v19-history-actions-692fcd7
```
