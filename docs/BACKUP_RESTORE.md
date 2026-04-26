# Backup and Restore (SuperMemory Data)

This runbook covers backup and restore of local `qdrant_data` state, including:

- Qdrant collections and segments
- SQLite stores under `qdrant_data` (`*.db`)
- Docs cache and other local operational files in that tree

## 1. Create backup

Stop write-heavy workloads first (recommended): API server, background workers, and Qdrant writes.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup_qdrant_data.ps1
```

Optional parameters:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup_qdrant_data.ps1 -Root qdrant_data -OutDir backups
```

Result: `backups/qdrant_data-backup-<timestamp>.zip`

## 2. Restore backup

Before restore, stop the API server and Qdrant writers.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/restore_qdrant_data.ps1 -ArchivePath backups/qdrant_data-backup-20260409-120000.zip
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
2. Move restored `qdrant_data` aside.
3. Move `<Root>.pre-restore-<timestamp>` back to `qdrant_data`.
4. Start services and repeat verification.
