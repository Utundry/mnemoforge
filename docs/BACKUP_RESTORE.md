# Backup and Restore (SloplessCode Data)

This runbook covers backup and restore of local SloplessCode system data.

SloplessCode now separates two data classes:

- System data root: SQLite/JSON source-of-truth stores such as project memory,
  tasks, laws, checkpoints, aliases, and lifecycle state.
- Qdrant storage: semantic indexing data. Qdrant is treated as rebuildable from
  SQLite-backed stores where possible.

The canonical system data root is selected in this order:

1. `SLOPLESSCODE_DATA_DIR`
2. `MNEMOFORGE_DATA_DIR` for old deployments
3. existing `system_data`
4. legacy `qdrant_data`

Older installations used `qdrant_data` for both system data and Qdrant files.
The old scripts remain, but their default behavior now backs up the selected
system data root.

- SQLite stores (`*.db`)
- JSON registries and local operational files
- docs cache and generated project state packets

## 1. Create backup

Stop write-heavy workloads first (recommended): API server, background workers, and Qdrant writes.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup_qdrant_data.ps1
```

Optional parameters:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup_qdrant_data.ps1 -Root qdrant_data -OutDir backups
```

Result: `backups/system_data-backup-<timestamp>.zip`

## 2. Restore backup

Before restore, stop the API server and Qdrant writers.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/restore_qdrant_data.ps1 -ArchivePath backups/system_data-backup-20260409-120000.zip
```

By default restore creates a pre-restore copy:

- `<Root>.pre-restore-<timestamp>`

Disable pre-restore copy (not recommended):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/restore_qdrant_data.ps1 -ArchivePath <zip> -NoBackup
```

## 3. Verify after restore

1. Start services.
2. Check health endpoint:
   - `GET /api/v1/health`
3. Run storage trust check:
   - `GET /api/v1/admin/storage-trust`
4. Spot-check critical paths:
   - memory search
   - project context enrichment
   - laws list / docs status

## 4. Rollback strategy

If restored state is bad:

1. Stop services.
2. Move restored system data root aside.
3. Move `<Root>.pre-restore-<timestamp>` back to the selected system data root.
4. Start services and repeat verification.
