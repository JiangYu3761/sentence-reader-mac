package com.click.shell;

import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.time.Instant;

final class ClickNativeStore extends SQLiteOpenHelper {
    static final String DATABASE_NAME = "click_native_reader.db";
    static final String ASSET_EPUB = "epub";
    static final String ASSET_COVER = "cover";
    static final String ASSET_DISPLAY_VARIANTS = "display_variants";
    static final String ASSET_SOURCE = "source";
    static final String IMPORT_PENDING = "pending";
    static final String IMPORT_TERMINAL = "terminal";
    private static final int DATABASE_VERSION = 10;
    private static final long ASSET_RETRY_BASE_MILLIS = 60_000L;
    private static final long ASSET_RETRY_MAX_MILLIS = 6L * 60L * 60L * 1000L;
    private static final int MAX_LOCAL_FILE_CLEANUPS_PER_DRAIN = 8;
    private final File appFilesDirectory;

    ClickNativeStore(Context context) {
        super(context, DATABASE_NAME, null, DATABASE_VERSION);
        appFilesDirectory = context.getApplicationContext().getFilesDir();
    }

    static SyncEvidence readSyncEvidence(
            Context context,
            String resourceType,
            String resourceId
    ) {
        File databaseFile = context.getApplicationContext().getDatabasePath(DATABASE_NAME);
        if (!databaseFile.isFile()) {
            return SyncEvidence.missing();
        }
        SQLiteDatabase database = null;
        try {
            database = SQLiteDatabase.openDatabase(
                    databaseFile.getAbsolutePath(),
                    null,
                    SQLiteDatabase.OPEN_READONLY
            );
            if (!database.isReadOnly()) {
                return SyncEvidence.missing();
            }
            long sequenceCursor = 0L;
            try (Cursor cursor = database.rawQuery(
                    "SELECT value FROM sync_state WHERE key=?",
                    new String[]{"sequence_cursor"}
            )) {
                if (cursor.moveToFirst()) {
                    sequenceCursor = nonNegativeLong(cursor.getString(0));
                }
            }

            String table;
            String identityColumn;
            if ("book".equals(resourceType)) {
                table = "books";
                identityColumn = "id";
            } else if ("position".equals(resourceType)) {
                table = "positions";
                identityColumn = "book_id";
            } else if ("annotation".equals(resourceType)) {
                table = "annotations";
                identityColumn = "id";
            } else {
                return new SyncEvidence(sequenceCursor, false, 0L);
            }
            try (Cursor cursor = database.rawQuery(
                    "SELECT server_version FROM "
                            + table
                            + " WHERE "
                            + identityColumn
                            + "=? LIMIT 1",
                    new String[]{resourceId}
            )) {
                if (!cursor.moveToFirst()) {
                    return new SyncEvidence(sequenceCursor, false, 0L);
                }
                return new SyncEvidence(
                        sequenceCursor,
                        true,
                        Math.max(0L, cursor.getLong(0))
                );
            }
        } catch (Throwable ignored) {
            return SyncEvidence.missing();
        } finally {
            if (database != null) {
                database.close();
            }
        }
    }

    private static long nonNegativeLong(String value) {
        try {
            return Math.max(0L, Long.parseLong(value == null ? "" : value.trim()));
        } catch (NumberFormatException ignored) {
            return 0L;
        }
    }

    static final class SyncEvidence {
        final long sequenceCursor;
        final boolean present;
        final long serverVersion;

        SyncEvidence(long sequenceCursor, boolean present, long serverVersion) {
            this.sequenceCursor = Math.max(0L, sequenceCursor);
            this.present = present;
            this.serverVersion = Math.max(0L, serverVersion);
        }

        static SyncEvidence missing() {
            return new SyncEvidence(0L, false, 0L);
        }
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, title TEXT, author TEXT, epub_url TEXT, epub_local_path TEXT, cover_url TEXT, cover_local_path TEXT, publication_json TEXT, position_json TEXT, updated_at TEXT, reading_profile TEXT, compatibility_json TEXT, display_variants_url TEXT, display_variants_local_path TEXT, analysis_state TEXT, analysis_updated_at TEXT, favorite INTEGER DEFAULT 0, custom_category TEXT DEFAULT '', tags_json TEXT DEFAULT '[]', last_opened_at TEXT DEFAULT '', server_version INTEGER DEFAULT 1, epub_hash TEXT DEFAULT '', epub_byte_size INTEGER DEFAULT -1, source_kind TEXT DEFAULT 'epub', source_local_path TEXT DEFAULT '', source_hash TEXT DEFAULT '', source_byte_size INTEGER DEFAULT -1, import_state TEXT DEFAULT 'remote', canonical_id TEXT DEFAULT '')");
        db.execSQL("CREATE TABLE IF NOT EXISTS chapters (book_id TEXT, chapter_index INTEGER, locator TEXT, title TEXT, html TEXT, PRIMARY KEY(book_id, chapter_index))");
        db.execSQL("CREATE TABLE IF NOT EXISTS annotations (id TEXT PRIMARY KEY, book_id TEXT, kind TEXT, source_text TEXT, note_text TEXT, color TEXT, chapter_locator TEXT, range_locator_json TEXT, metadata_json TEXT, updated_at TEXT, server_version INTEGER DEFAULT 1)");
        db.execSQL("CREATE TABLE IF NOT EXISTS positions (book_id TEXT PRIMARY KEY, chapter_locator TEXT, page_index INTEGER, total_pages INTEGER, locator_json TEXT, updated_at TEXT, server_version INTEGER DEFAULT 1)");
        db.execSQL("CREATE TABLE IF NOT EXISTS lookup_cache (book_id TEXT, word TEXT, payload_json TEXT, updated_at TEXT, PRIMARY KEY(book_id, word))");
        db.execSQL("CREATE TABLE IF NOT EXISTS operation_queue (operation_id TEXT PRIMARY KEY, operation_type TEXT, book_id TEXT, payload_json TEXT, sync_status TEXT, created_at TEXT, updated_at TEXT, retry_count INTEGER DEFAULT 0, last_error TEXT, base_server_version INTEGER DEFAULT 0, receipt_json TEXT DEFAULT '{}')");
        createAssetRefreshQueue(db);
        createMetadataRepairQueue(db);
        createBookImportQueue(db);
        createLocalFileCleanupQueue(db);
        db.execSQL("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        if (oldVersion < 8) {
            // v7 already owns asset_refresh_queue. Add the indexed column before onCreate()
            // asks SQLite to create the v8 state-leading index.
            addColumn(
                    db,
                    "ALTER TABLE asset_refresh_queue ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'"
            );
        }
        onCreate(db);
        if (oldVersion < 2) {
            try {
                db.execSQL("ALTER TABLE books ADD COLUMN epub_local_path TEXT DEFAULT ''");
            } catch (Throwable ignored) {
                // The column may already exist after CREATE TABLE IF NOT EXISTS.
            }
        }
        if (oldVersion < 3) {
            try {
                db.execSQL("ALTER TABLE books ADD COLUMN cover_url TEXT DEFAULT ''");
            } catch (Throwable ignored) {
                // The column may already exist after CREATE TABLE IF NOT EXISTS.
            }
            try {
                db.execSQL("ALTER TABLE books ADD COLUMN cover_local_path TEXT DEFAULT ''");
            } catch (Throwable ignored) {
                // The column may already exist after CREATE TABLE IF NOT EXISTS.
            }
        }
        if (oldVersion < 4) {
            addColumn(db, "ALTER TABLE books ADD COLUMN reading_profile TEXT DEFAULT 'UNKNOWN'");
            addColumn(db, "ALTER TABLE books ADD COLUMN compatibility_json TEXT DEFAULT '{}'");
            addColumn(db, "ALTER TABLE books ADD COLUMN display_variants_url TEXT DEFAULT ''");
            addColumn(db, "ALTER TABLE books ADD COLUMN display_variants_local_path TEXT DEFAULT ''");
            addColumn(db, "ALTER TABLE books ADD COLUMN analysis_state TEXT DEFAULT 'not_requested'");
            addColumn(db, "ALTER TABLE books ADD COLUMN analysis_updated_at TEXT DEFAULT ''");
        }
        if (oldVersion < 5) {
            addColumn(db, "ALTER TABLE books ADD COLUMN favorite INTEGER DEFAULT 0");
            addColumn(db, "ALTER TABLE books ADD COLUMN custom_category TEXT DEFAULT ''");
            addColumn(db, "ALTER TABLE books ADD COLUMN tags_json TEXT DEFAULT '[]'");
            addColumn(db, "ALTER TABLE books ADD COLUMN last_opened_at TEXT DEFAULT ''");
        }
        if (oldVersion < 6) {
            addColumn(db, "ALTER TABLE books ADD COLUMN server_version INTEGER DEFAULT 1");
            addColumn(db, "ALTER TABLE books ADD COLUMN epub_hash TEXT DEFAULT ''");
            addColumn(db, "ALTER TABLE books ADD COLUMN epub_byte_size INTEGER DEFAULT -1");
            addColumn(db, "ALTER TABLE annotations ADD COLUMN server_version INTEGER DEFAULT 1");
            addColumn(db, "ALTER TABLE positions ADD COLUMN server_version INTEGER DEFAULT 1");
            addColumn(db, "ALTER TABLE operation_queue ADD COLUMN base_server_version INTEGER DEFAULT 0");
        }
        if (oldVersion < 7) {
            db.execSQL("UPDATE operation_queue SET sync_status='retryable' WHERE sync_status='failed'");
            createAssetRefreshQueue(db);
        }
        if (oldVersion < 8) {
            addColumn(
                    db,
                    "ALTER TABLE operation_queue ADD COLUMN receipt_json TEXT DEFAULT '{}'"
            );
            createMetadataRepairQueue(db);
        }
        if (oldVersion < 9) {
            addColumn(db, "ALTER TABLE books ADD COLUMN source_kind TEXT DEFAULT 'epub'");
            addColumn(db, "ALTER TABLE books ADD COLUMN source_local_path TEXT DEFAULT ''");
            addColumn(db, "ALTER TABLE books ADD COLUMN source_hash TEXT DEFAULT ''");
            addColumn(db, "ALTER TABLE books ADD COLUMN source_byte_size INTEGER DEFAULT -1");
            addColumn(db, "ALTER TABLE books ADD COLUMN import_state TEXT DEFAULT 'remote'");
            addColumn(db, "ALTER TABLE books ADD COLUMN canonical_id TEXT DEFAULT ''");
            db.execSQL(
                    "UPDATE books SET source_kind=COALESCE(NULLIF(source_kind, ''), 'epub'), "
                            + "canonical_id=COALESCE(NULLIF(canonical_id, ''), id)"
            );
            migrateAssetRefreshQueueToV9(db);
            createBookImportQueue(db);
        }
        if (oldVersion < 10) {
            createLocalFileCleanupQueue(db);
        }
    }

    private void createAssetRefreshQueue(SQLiteDatabase db) {
        db.execSQL(
                "CREATE TABLE IF NOT EXISTS asset_refresh_queue ("
                        + "book_id TEXT NOT NULL, "
                        + "asset_type TEXT NOT NULL CHECK(asset_type IN ('epub','cover','display_variants','source')), "
                        + "asset_url TEXT NOT NULL DEFAULT '', "
                        + "expected_hash TEXT NOT NULL DEFAULT '', "
                        + "expected_byte_size INTEGER NOT NULL DEFAULT -1, "
                        + "contract_marker TEXT NOT NULL DEFAULT '', "
                        + "state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','terminal')), "
                        + "retry_count INTEGER NOT NULL DEFAULT 0, "
                        + "last_error TEXT NOT NULL DEFAULT '', "
                        + "next_attempt_at INTEGER NOT NULL DEFAULT 0, "
                        + "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                        + "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
                        + "PRIMARY KEY(book_id, asset_type))"
        );
        db.execSQL(
                "CREATE INDEX IF NOT EXISTS asset_refresh_due_idx "
                        + "ON asset_refresh_queue(next_attempt_at, retry_count, updated_at)"
        );
        db.execSQL(
                "CREATE INDEX IF NOT EXISTS asset_refresh_due_idx_v8 "
                        + "ON asset_refresh_queue(state, next_attempt_at, retry_count, updated_at)"
        );
    }

    private void migrateAssetRefreshQueueToV9(SQLiteDatabase db) {
        db.execSQL("DROP INDEX IF EXISTS asset_refresh_due_idx");
        db.execSQL("DROP INDEX IF EXISTS asset_refresh_due_idx_v8");
        db.execSQL("ALTER TABLE asset_refresh_queue RENAME TO asset_refresh_queue_v8");
        createAssetRefreshQueue(db);
        db.execSQL(
                "INSERT INTO asset_refresh_queue ("
                        + "book_id, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at, "
                        + "created_at, updated_at"
                        + ") SELECT book_id, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at, "
                        + "created_at, updated_at FROM asset_refresh_queue_v8"
        );
        db.execSQL("DROP TABLE asset_refresh_queue_v8");
    }

    private void createMetadataRepairQueue(SQLiteDatabase db) {
        db.execSQL(
                "CREATE TABLE IF NOT EXISTS metadata_repair_queue ("
                        + "book_id TEXT PRIMARY KEY, "
                        + "reason TEXT NOT NULL DEFAULT '', "
                        + "contract_marker TEXT NOT NULL DEFAULT '', "
                        + "state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','terminal')), "
                        + "retry_count INTEGER NOT NULL DEFAULT 0, "
                        + "last_error TEXT NOT NULL DEFAULT '', "
                        + "next_attempt_at INTEGER NOT NULL DEFAULT 0, "
                        + "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                        + "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        );
        db.execSQL(
                "CREATE INDEX IF NOT EXISTS metadata_repair_due_idx "
                        + "ON metadata_repair_queue(state, next_attempt_at, retry_count, updated_at)"
        );
    }

    private void createBookImportQueue(SQLiteDatabase db) {
        db.execSQL(
                "CREATE TABLE IF NOT EXISTS book_import_queue ("
                        + "local_book_id TEXT PRIMARY KEY, "
                        + "source_kind TEXT NOT NULL CHECK(source_kind IN ('epub','pdf')), "
                        + "source_local_path TEXT NOT NULL, "
                        + "source_hash TEXT NOT NULL, "
                        + "source_byte_size INTEGER NOT NULL, "
                        + "filename TEXT NOT NULL, "
                        + "state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','terminal')), "
                        + "last_error TEXT NOT NULL DEFAULT '', "
                        + "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                        + "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        );
        db.execSQL(
                "CREATE INDEX IF NOT EXISTS book_import_pending_idx_v9 "
                        + "ON book_import_queue(state, updated_at, local_book_id)"
        );
        db.execSQL(
                "CREATE INDEX IF NOT EXISTS book_import_hash_idx_v9 "
                        + "ON book_import_queue(source_hash)"
        );
    }

    private void createLocalFileCleanupQueue(SQLiteDatabase db) {
        db.execSQL(
                "CREATE TABLE IF NOT EXISTS local_file_cleanup_queue ("
                        + "canonical_path TEXT PRIMARY KEY, "
                        + "book_id TEXT NOT NULL DEFAULT '', "
                        + "attempt_count INTEGER NOT NULL DEFAULT 0, "
                        + "last_error TEXT NOT NULL DEFAULT '', "
                        + "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                        + "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        );
        db.execSQL(
                "CREATE INDEX IF NOT EXISTS local_file_cleanup_created_idx_v10 "
                        + "ON local_file_cleanup_queue(created_at, canonical_path)"
        );
    }

    private void addColumn(SQLiteDatabase db, String statement) {
        try {
            db.execSQL(statement);
        } catch (Throwable ignored) {
            // Incremental installs may already contain the column after a development build.
        }
    }

    void saveFullSync(JSONObject payload) throws Exception {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            saveFullSyncLocked(db, payload);
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void saveFullBaseline(JSONObject payload) throws Exception {
        if (payload == null || !payload.has("watermark_sequence")) {
            throw new IllegalArgumentException("full baseline requires watermark_sequence");
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            saveFullSyncLocked(db, payload);
            registerMetadataRepairsLocked(db, payload);
            setSyncValueLocked(db, "full_sync_completed", "1");
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private void saveFullSyncLocked(SQLiteDatabase db, JSONObject payload) throws Exception {
        deleteRemovedBooksLocked(db, payload.optJSONArray("removed_book_ids"));
        JSONArray books = payload.optJSONArray("books");
        if (books != null) {
            for (int i = 0; i < books.length(); i++) {
                JSONObject item = books.getJSONObject(i);
                JSONObject book = item.optJSONObject("book");
                if (book == null) {
                    continue;
                }
                JSONObject epub = item.optJSONObject("epub");
                JSONObject source = item.optJSONObject("source");
                JSONObject cover = item.optJSONObject("cover");
                JSONObject compatibility = item.optJSONObject("compatibility");
                JSONObject displayVariants = item.optJSONObject("display_variants");
                JSONObject analysis = item.optJSONObject("living_book_analysis");
                JSONObject organization = item.optJSONObject("organization");
                String bookId = book.optString("id");
                db.execSQL(
                        "DELETE FROM metadata_repair_queue WHERE book_id=?",
                        new Object[]{bookId}
                );
                BookAssetState previousAssets = bookAssetStateLocked(db, bookId);
                db.execSQL(
                            "INSERT OR REPLACE INTO books (id, title, author, epub_url, epub_local_path, cover_url, cover_local_path, publication_json, position_json, updated_at, reading_profile, compatibility_json, display_variants_url, display_variants_local_path, analysis_state, analysis_updated_at, favorite, custom_category, tags_json, last_opened_at) VALUES (?, ?, ?, ?, COALESCE((SELECT epub_local_path FROM books WHERE id=?), ''), ?, COALESCE((SELECT cover_local_path FROM books WHERE id=?), ''), ?, ?, ?, ?, ?, ?, COALESCE((SELECT display_variants_local_path FROM books WHERE id=?), ''), ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), (SELECT last_opened_at FROM books WHERE id=?), ''))",
                            new Object[]{
                                    bookId,
                                    book.optString("title"),
                                    book.optString("author"),
                                    epub == null ? "" : epub.optString("url"),
                                    bookId,
                                    cover == null ? "" : cover.optString("url"),
                                    bookId,
                                    item.optJSONObject("publication") == null ? "{}" : item.optJSONObject("publication").toString(),
                                    item.optJSONObject("position") == null ? "{}" : item.optJSONObject("position").toString(),
                                    book.optString("updated_at"),
                                    book.optString("reading_profile", compatibility == null ? "UNKNOWN" : compatibility.optString("reading_profile", "UNKNOWN")),
                                    compatibility == null ? "{}" : compatibility.toString(),
                                    displayVariants == null ? "" : displayVariants.optString("url"),
                                    bookId,
                                    analysis == null ? book.optString("analysis_state", "not_requested") : analysis.optString("state", "not_requested"),
                                    analysis == null ? book.optString("analysis_updated_at") : analysis.optString("updated_at"),
                                    organization != null && organization.optBoolean("favorite") ? 1 : 0,
                                    organization == null ? "" : organization.optString("custom_category"),
                                    organization == null || organization.optJSONArray("tags") == null ? "[]" : organization.optJSONArray("tags").toString(),
                                    book.optString("last_opened_at"),
                                    bookId
                            }
                );
                db.execSQL(
                            "UPDATE books SET server_version=?, epub_hash=?, epub_byte_size=? WHERE id=?",
                            new Object[]{
                                    Math.max(1, book.optLong("server_version", 1L)),
                                    epub == null ? "" : epub.optString("file_hash"),
                                    epub == null ? -1L : epub.optLong("byte_size", -1L),
                                    bookId
                            }
                );
                applySourceContractLocked(db, bookId, book, source, previousAssets);
                registerAssetRefreshLocked(
                            db,
                            bookId,
                            ASSET_EPUB,
                            epub == null ? "" : epub.optString("url"),
                            epub == null ? "" : epub.optString("file_hash"),
                            epub == null ? -1L : epub.optLong("byte_size", -1L),
                            (epub == null ? "" : epub.optString("file_hash"))
                                    + ":"
                                    + (epub == null ? -1L : epub.optLong("byte_size", -1L)),
                            previousAssets == null
                                    || !knownFileAvailable(previousAssets.epubLocalPath)
                                    || !same(previousAssets.epubUrl, epub == null ? "" : epub.optString("url"))
                                    || !same(previousAssets.epubHash, epub == null ? "" : epub.optString("file_hash"))
                                    || previousAssets.epubByteSize != (epub == null ? -1L : epub.optLong("byte_size", -1L))
                );
                registerAssetRefreshLocked(
                            db,
                            bookId,
                            ASSET_SOURCE,
                            source == null ? "" : source.optString("url"),
                            source == null ? "" : source.optString("file_hash"),
                            source == null ? -1L : source.optLong("byte_size", -1L),
                            (source == null ? "" : source.optString("file_hash"))
                                    + ":"
                                    + (source == null ? -1L : source.optLong("byte_size", -1L)),
                            previousAssets == null
                                    || !knownFileAvailable(previousAssets.sourceLocalPath)
                                    || !same(previousAssets.sourceHash, source == null ? "" : source.optString("file_hash"))
                                    || previousAssets.sourceByteSize != (source == null ? -1L : source.optLong("byte_size", -1L))
                );
                registerAssetRefreshLocked(
                            db,
                            bookId,
                            ASSET_COVER,
                            cover == null ? "" : cover.optString("url"),
                            "",
                            -1L,
                            book.optString("updated_at"),
                            previousAssets == null
                                    || !knownFileAvailable(previousAssets.coverLocalPath)
                                    || !same(previousAssets.coverUrl, cover == null ? "" : cover.optString("url"))
                                    || !same(previousAssets.updatedAt, book.optString("updated_at"))
                );
                boolean displayVariantsAvailable = displayVariants != null
                        && displayVariants.optBoolean("available")
                        && !displayVariants.optString("url").isEmpty();
                registerAssetRefreshLocked(
                            db,
                            bookId,
                            ASSET_DISPLAY_VARIANTS,
                            displayVariantsAvailable ? displayVariants.optString("url") : "",
                            "",
                            -1L,
                            book.optString("updated_at"),
                            displayVariantsAvailable && (
                                    previousAssets == null
                                            || !knownFileAvailable(previousAssets.displayVariantsLocalPath)
                                            || !same(previousAssets.displayVariantsUrl, displayVariants.optString("url"))
                                            || !same(previousAssets.updatedAt, book.optString("updated_at"))
                            )
                );
                JSONArray chapters = item.optJSONArray("chapters");
                if (chapters != null) {
                    for (int c = 0; c < chapters.length(); c++) {
                        JSONObject chapter = chapters.getJSONObject(c);
                        db.execSQL(
                                    "INSERT OR REPLACE INTO chapters (book_id, chapter_index, locator, title, html) VALUES (?, ?, ?, ?, ?)",
                                    new Object[]{
                                            bookId,
                                            chapter.optInt("index", c),
                                            chapter.optString("locator"),
                                            chapter.optString("title"),
                                            chapter.optString("html")
                                    }
                        );
                    }
                }
                JSONArray annotations = item.optJSONArray("annotations");
                if (annotations != null) {
                    removeMissingServerAnnotationsLocked(db, bookId, annotations);
                    for (int a = 0; a < annotations.length(); a++) {
                        saveAnnotationLocked(db, annotations.getJSONObject(a));
                    }
                }
                JSONObject position = item.optJSONObject("position");
                if (position != null) {
                    savePositionLocked(db, position);
                }
            }
        }
        if (payload.has("cursor")) {
            setSyncValueLocked(db, "cursor", payload.optString("cursor"));
        }
        if (payload.has("watermark_sequence")) {
            setSyncValueLocked(db, "sequence_cursor", String.valueOf(payload.optLong("watermark_sequence", 0L)));
        }
    }

    private void registerMetadataRepairsLocked(SQLiteDatabase db, JSONObject payload) {
        JSONArray errors = payload.optJSONArray("errors");
        if (errors != null) {
            for (int index = 0; index < errors.length(); index++) {
                JSONObject error = errors.optJSONObject(index);
                if (error == null) {
                    continue;
                }
                registerMetadataRepairLocked(
                        db,
                        error.optString("book_id"),
                        error.optString("reason", error.optString("error")),
                        error.optString("contract_marker", error.optString("updated_at"))
                );
            }
        }
        JSONArray skipped = payload.optJSONArray("skipped_books");
        if (skipped == null) {
            return;
        }
        for (int index = 0; index < skipped.length(); index++) {
            JSONObject item = skipped.optJSONObject(index);
            if (item == null
                    || (!"missing_epub_source".equals(item.optString("reason"))
                    && !"missing_source".equals(item.optString("reason")))) {
                continue;
            }
            registerMetadataRepairLocked(
                    db,
                    item.optString("book_id"),
                    item.optString("reason"),
                    item.optString("contract_marker", item.optString("updated_at"))
            );
        }
    }

    void saveChanges(JSONObject payload) throws Exception {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            deleteRemovedBooksLocked(db, payload.optJSONArray("removed_book_ids"));
            JSONArray books = payload.optJSONArray("books");
            if (books != null) {
                for (int i = 0; i < books.length(); i++) {
                    JSONObject book = books.getJSONObject(i);
                    JSONObject organization = book.optJSONObject("organization");
                    BookAssetState previousAssets = bookAssetStateLocked(db, book.optString("id"));
                    db.execSQL(
                            "INSERT OR REPLACE INTO books (id, title, author, epub_url, epub_local_path, cover_url, cover_local_path, publication_json, position_json, updated_at, reading_profile, compatibility_json, display_variants_url, display_variants_local_path, analysis_state, analysis_updated_at, favorite, custom_category, tags_json, last_opened_at, server_version, epub_hash, epub_byte_size) VALUES (?, ?, ?, COALESCE((SELECT epub_url FROM books WHERE id=?), ''), COALESCE((SELECT epub_local_path FROM books WHERE id=?), ''), COALESCE((SELECT cover_url FROM books WHERE id=?), ''), COALESCE((SELECT cover_local_path FROM books WHERE id=?), ''), COALESCE((SELECT publication_json FROM books WHERE id=?), '{}'), COALESCE((SELECT position_json FROM books WHERE id=?), '{}'), ?, ?, COALESCE((SELECT compatibility_json FROM books WHERE id=?), '{}'), COALESCE((SELECT display_variants_url FROM books WHERE id=?), ''), COALESCE((SELECT display_variants_local_path FROM books WHERE id=?), ''), ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), (SELECT last_opened_at FROM books WHERE id=?), ''), ?, COALESCE((SELECT epub_hash FROM books WHERE id=?), ''), COALESCE((SELECT epub_byte_size FROM books WHERE id=?), -1))",
                            new Object[]{
                                    book.optString("id"), book.optString("title"), book.optString("author"),
                                    book.optString("id"), book.optString("id"), book.optString("id"), book.optString("id"),
                                    book.optString("id"), book.optString("id"), book.optString("updated_at"),
                                    book.optString("reading_profile", "UNKNOWN"), book.optString("id"), book.optString("id"), book.optString("id"),
                                    book.optString("analysis_state", "not_requested"), book.optString("analysis_updated_at"),
                                    organization != null && organization.optBoolean("favorite") ? 1 : 0,
                                    organization == null ? "" : organization.optString("custom_category"),
                                    organization == null || organization.optJSONArray("tags") == null ? "[]" : organization.optJSONArray("tags").toString(),
                                    book.optString("last_opened_at"), book.optString("id"),
                                    Math.max(1, book.optLong("server_version", 1L)),
                                    book.optString("id"), book.optString("id")
                            }
                    );
                    applySourceContractLocked(db, book.optString("id"), book, null, previousAssets);
                    registerAssetRefreshLocked(
                            db,
                            book.optString("id"),
                            ASSET_SOURCE,
                            book.optString("source_url"),
                            book.optString("source_hash"),
                            book.optLong("source_byte_size", -1L),
                            book.optString("source_hash")
                                    + ":"
                                    + book.optLong("source_byte_size", -1L),
                            previousAssets == null
                                    || !knownFileAvailable(previousAssets.sourceLocalPath)
                                    || !same(previousAssets.sourceHash, book.optString("source_hash"))
                                    || previousAssets.sourceByteSize
                                    != book.optLong("source_byte_size", -1L)
                    );
                }
            }
            JSONArray booksToRefresh = payload.optJSONArray("books_to_refresh");
            if (booksToRefresh != null) {
                String contractMarker = "sequence:" + payload.optLong("next_sequence", 0L);
                for (int index = 0; index < booksToRefresh.length(); index++) {
                    registerMetadataRepairLocked(
                            db,
                            booksToRefresh.optString(index),
                            "远端书籍元数据已变化",
                            contractMarker
                    );
                }
            }
            JSONArray contracts = payload.optJSONArray("book_contracts");
            if (contracts != null) {
                for (int i = 0; i < contracts.length(); i++) {
                    saveBookContractLocked(db, contracts.getJSONObject(i));
                }
            }
            JSONArray positions = payload.optJSONArray("positions");
            if (positions != null) {
                for (int i = 0; i < positions.length(); i++) {
                    savePositionLocked(db, positions.getJSONObject(i));
                }
            }
            JSONArray annotations = payload.optJSONArray("annotations");
            if (annotations != null) {
                for (int i = 0; i < annotations.length(); i++) {
                    saveAnnotationLocked(db, annotations.getJSONObject(i));
                }
            }
            deleteRemovedAnnotationIdsLocked(db, payload.optJSONArray("removed_annotation_ids"));
            deleteRemovedPositionIdsLocked(db, payload.optJSONArray("removed_position_book_ids"));
            if (payload.has("next_sequence")) {
                setSyncValueLocked(db, "sequence_cursor", String.valueOf(payload.optLong("next_sequence", 0L)));
            } else if (payload.has("cursor")) {
                setSyncValueLocked(db, "cursor", payload.optString("cursor"));
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void saveBookAnnotations(JSONArray rows) throws Exception {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            for (int index = 0; index < rows.length(); index++) {
                saveAnnotationLocked(db, rows.getJSONObject(index));
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void commitVerifiedEpubRefresh(JSONObject publication, String localPath) throws Exception {
        if (publication == null || !knownFileAvailable(localPath)) {
            throw new IllegalArgumentException("verified EPUB refresh requires a local file");
        }
        JSONObject book = publication.optJSONObject("book");
        JSONArray chapters = publication.optJSONArray("chapters");
        String bookId = book == null ? "" : clean(book.optString("id"));
        if (bookId.isEmpty() || chapters == null) {
            throw new IllegalArgumentException("verified EPUB refresh requires book and chapters");
        }

        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.execSQL("DELETE FROM chapters WHERE book_id=?", new Object[]{bookId});
            saveFullSyncLocked(
                    db,
                    new JSONObject().put("books", new JSONArray().put(publication))
            );
            db.execSQL(
                    "UPDATE books SET epub_local_path=? WHERE id=?",
                    new Object[]{localPath, bookId}
            );
            db.execSQL(
                    "DELETE FROM asset_refresh_queue WHERE book_id=? AND asset_type=?",
                    new Object[]{bookId, ASSET_EPUB}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private BookAssetState bookAssetStateLocked(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT updated_at, epub_url, epub_local_path, cover_url, cover_local_path, "
                        + "display_variants_url, display_variants_local_path, epub_hash, epub_byte_size, "
                        + "source_kind, source_local_path, source_hash, source_byte_size, "
                        + "import_state, canonical_id "
                        + "FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            if (!cursor.moveToFirst()) {
                return null;
            }
            return new BookAssetState(
                    cursor.getString(0),
                    cursor.getString(1),
                    cursor.getString(2),
                    cursor.getString(3),
                    cursor.getString(4),
                    cursor.getString(5),
                    cursor.getString(6),
                    cursor.getString(7),
                    cursor.getLong(8),
                    cursor.getString(9),
                    cursor.getString(10),
                    cursor.getString(11),
                    cursor.getLong(12),
                    cursor.getString(13),
                    cursor.getString(14)
            );
        }
    }

    private void applySourceContractLocked(
            SQLiteDatabase db,
            String bookId,
            JSONObject book,
            JSONObject source,
            BookAssetState previous
    ) {
        String projectedKind = clean(
                source == null
                        ? book.optString("source_kind")
                        : source.optString("kind", book.optString("source_kind"))
        ).toLowerCase(Locale.ROOT);
        if (!"epub".equals(projectedKind) && !"pdf".equals(projectedKind)) {
            projectedKind = previous == null || previous.sourceKind.isEmpty()
                    ? "epub"
                    : previous.sourceKind;
        }
        String projectedHash = clean(
                source == null
                        ? book.optString("source_hash", book.optString("book_hash"))
                        : source.optString("file_hash", book.optString("book_hash"))
        );
        long projectedBytes = source == null
                ? book.optLong("source_byte_size", book.optLong("byte_size", -1L))
                : source.optLong("byte_size", book.optLong("byte_size", -1L));
        String localPath = previous == null ? "" : previous.sourceLocalPath;
        String importState = previous == null || previous.importState.isEmpty()
                ? "remote"
                : previous.importState;
        String canonicalId = previous == null || previous.canonicalId.isEmpty()
                ? clean(bookId)
                : previous.canonicalId;
        db.execSQL(
                "UPDATE books SET source_kind=?, source_local_path=?, source_hash=?, "
                        + "source_byte_size=?, import_state=?, canonical_id=? WHERE id=?",
                new Object[]{
                        projectedKind,
                        localPath,
                        projectedHash,
                        projectedBytes,
                        importState,
                        canonicalId,
                        bookId
                }
        );
    }

    private void registerAssetRefreshLocked(
            SQLiteDatabase db,
            String bookId,
            String assetType,
            String assetUrl,
            String expectedHash,
            long expectedByteSize,
            String contractMarker,
            boolean needsRefresh
    ) {
        if (bookId == null || bookId.trim().isEmpty() || !isAssetType(assetType)) {
            return;
        }
        String cleanUrl = clean(assetUrl);
        if (cleanUrl.isEmpty()) {
            if (ASSET_DISPLAY_VARIANTS.equals(assetType)) {
                db.execSQL(
                        "UPDATE books SET display_variants_local_path='' WHERE id=?",
                        new Object[]{bookId}
                );
            }
            db.execSQL(
                    "DELETE FROM asset_refresh_queue WHERE book_id=? AND asset_type=?",
                    new Object[]{bookId, assetType}
            );
            return;
        }
        AssetRefreshRow existing = assetRefreshLocked(db, bookId, assetType);
        String cleanHash = clean(expectedHash);
        String cleanMarker = clean(contractMarker);
        if (existing == null) {
            if (!needsRefresh) {
                return;
            }
            db.execSQL(
                    "INSERT INTO asset_refresh_queue ("
                            + "book_id, asset_type, asset_url, expected_hash, expected_byte_size, contract_marker, "
                            + "state, retry_count, last_error, next_attempt_at, created_at, updated_at"
                            + ") VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '', 0, datetime('now'), datetime('now'))",
                    new Object[]{bookId, assetType, cleanUrl, cleanHash, expectedByteSize, cleanMarker}
            );
            return;
        }
        boolean contractChanged = !same(existing.assetUrl, cleanUrl)
                || !same(existing.expectedHash, cleanHash)
                || existing.expectedByteSize != expectedByteSize
                || !same(existing.contractMarker, cleanMarker);
        if (contractChanged) {
            db.execSQL(
                    "UPDATE asset_refresh_queue SET asset_url=?, expected_hash=?, expected_byte_size=?, "
                            + "contract_marker=?, state='pending', retry_count=0, last_error='', next_attempt_at=0, "
                            + "updated_at=datetime('now') WHERE book_id=? AND asset_type=?",
                    new Object[]{
                            cleanUrl,
                            cleanHash,
                            expectedByteSize,
                            cleanMarker,
                            bookId,
                            assetType
                    }
            );
        }
    }

    private void registerMetadataRepairLocked(
            SQLiteDatabase db,
            String bookId,
            String reason,
            String contractMarker
    ) {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            return;
        }
        String cleanReason = clean(reason);
        String cleanMarker = clean(contractMarker);
        MetadataRepairRow existing = metadataRepairLocked(db, cleanBookId);
        if (existing == null) {
            db.execSQL(
                    "INSERT INTO metadata_repair_queue ("
                            + "book_id, reason, contract_marker, state, retry_count, last_error, "
                            + "next_attempt_at, created_at, updated_at"
                            + ") VALUES (?, ?, ?, 'pending', 0, '', 0, datetime('now'), datetime('now'))",
                    new Object[]{cleanBookId, cleanReason, cleanMarker}
            );
            return;
        }
        if (!same(existing.contractMarker, cleanMarker)) {
            db.execSQL(
                    "UPDATE metadata_repair_queue SET reason=?, contract_marker=?, state='pending', "
                            + "retry_count=0, last_error='', next_attempt_at=0, updated_at=datetime('now') "
                            + "WHERE book_id=?",
                    new Object[]{cleanReason, cleanMarker, cleanBookId}
            );
        } else if ("pending".equals(existing.state) && !same(existing.reason, cleanReason)) {
            db.execSQL(
                    "UPDATE metadata_repair_queue SET reason=?, updated_at=datetime('now') WHERE book_id=?",
                    new Object[]{cleanReason, cleanBookId}
            );
        }
    }

    private static boolean same(String left, String right) {
        return clean(left).equals(clean(right));
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    private static String cleanOptionalText(String value) {
        String normalized = clean(value);
        return "null".equals(normalized) ? "" : normalized;
    }

    private static boolean knownFileAvailable(String localPath) {
        String path = clean(localPath);
        return !path.isEmpty() && new File(path).isFile();
    }

    private static boolean isAssetType(String assetType) {
        return ASSET_EPUB.equals(assetType)
                || ASSET_COVER.equals(assetType)
                || ASSET_DISPLAY_VARIANTS.equals(assetType)
                || ASSET_SOURCE.equals(assetType);
    }

    private void saveBookContractLocked(SQLiteDatabase db, JSONObject contract) {
        db.execSQL(
                "UPDATE books SET reading_profile=COALESCE(NULLIF(?, ''), reading_profile), analysis_state=COALESCE(NULLIF(?, ''), analysis_state), analysis_updated_at=? WHERE id=?",
                new Object[]{
                        contract.optString("reading_profile"),
                        contract.optString("analysis_state"),
                        contract.optString("analysis_updated_at"),
                        contract.optString("book_id")
                }
        );
    }

    private void deleteRemovedBooksLocked(SQLiteDatabase db, JSONArray removedBookIds) throws Exception {
        if (removedBookIds == null) {
            return;
        }
        for (int i = 0; i < removedBookIds.length(); i++) {
            String bookId = removedBookIds.optString(i, "").trim();
            if (bookId.isEmpty()) {
                continue;
            }
            deleteBookLocked(db, bookId);
        }
    }

    private void removeMissingServerAnnotationsLocked(SQLiteDatabase db, String bookId, JSONArray serverRows) {
        Set<String> serverIds = new HashSet<>();
        List<String> removedIds = new ArrayList<>();
        for (int index = 0; index < serverRows.length(); index++) {
            JSONObject row = serverRows.optJSONObject(index);
            if (row != null && !row.optString("id").isEmpty()) {
                serverIds.add(row.optString("id"));
            }
        }
        try (Cursor cursor = db.rawQuery("SELECT id FROM annotations WHERE book_id=?", new String[]{bookId})) {
            while (cursor.moveToNext()) {
                String annotationId = cursor.getString(0);
                if (annotationId.startsWith("local-")
                        || serverIds.contains(annotationId)
                        || hasUnresolvedAnnotationOperationLocked(db, annotationId)) {
                    continue;
                }
                removedIds.add(annotationId);
            }
        }
        for (String annotationId : removedIds) {
            db.execSQL("DELETE FROM annotations WHERE id=?", new Object[]{annotationId});
        }
    }

    private void deleteRemovedAnnotationIdsLocked(SQLiteDatabase db, JSONArray removedIds) {
        if (removedIds == null) {
            return;
        }
        for (int index = 0; index < removedIds.length(); index++) {
            String annotationId = removedIds.optString(index, "").trim();
            if (!annotationId.isEmpty() && !hasUnresolvedAnnotationOperationLocked(db, annotationId)) {
                db.execSQL("DELETE FROM annotations WHERE id=?", new Object[]{annotationId});
            }
        }
    }

    private void deleteRemovedPositionIdsLocked(SQLiteDatabase db, JSONArray removedBookIds) {
        if (removedBookIds == null) {
            return;
        }
        for (int index = 0; index < removedBookIds.length(); index++) {
            String bookId = removedBookIds.optString(index, "").trim();
            if (bookId.isEmpty()) {
                continue;
            }
            try (Cursor cursor = db.rawQuery(
                    "SELECT 1 FROM operation_queue WHERE book_id=? AND operation_type='reading_position_updated' AND sync_status IN ('pending','retryable','conflict','permanent_failed','local_only') LIMIT 1",
                    new String[]{bookId}
            )) {
                if (!cursor.moveToFirst()) {
                    db.execSQL("DELETE FROM positions WHERE book_id=?", new Object[]{bookId});
                }
            }
        }
    }

    private void deleteBookLocked(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT 1 FROM book_import_queue WHERE local_book_id=? AND state='pending' LIMIT 1",
                new String[]{bookId}
        )) {
            if (cursor.moveToFirst()) {
                preserveDeferredRemoteBookDeletionLocked(
                        db,
                        bookId,
                        "远端已删除；手机原书仍在等待归档"
                );
                return;
            }
        }
        if (hasUnresolvedBookOperationLocked(db, bookId)) {
            preserveDeferredRemoteBookDeletionLocked(
                    db,
                    bookId,
                    "远端已删除；手机仍有未同步的阅读证据"
            );
            return;
        }
        Object[] args = new Object[]{bookId};
        db.execSQL("DELETE FROM chapters WHERE book_id=?", args);
        db.execSQL("DELETE FROM annotations WHERE book_id=?", args);
        db.execSQL("DELETE FROM positions WHERE book_id=?", args);
        db.execSQL("DELETE FROM lookup_cache WHERE book_id=?", args);
        db.execSQL("DELETE FROM operation_queue WHERE book_id=?", args);
        db.execSQL("DELETE FROM asset_refresh_queue WHERE book_id=?", args);
        db.execSQL("DELETE FROM metadata_repair_queue WHERE book_id=?", args);
        db.execSQL("DELETE FROM book_import_queue WHERE local_book_id=?", args);
        db.execSQL("DELETE FROM books WHERE id=?", args);
    }

    private boolean hasUnresolvedBookOperationLocked(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT 1 FROM operation_queue WHERE book_id=? "
                        + "AND sync_status IN ('pending','retryable','conflict',"
                        + "'permanent_failed','local_only') LIMIT 1",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst();
        }
    }

    private void preserveDeferredRemoteBookDeletionLocked(
            SQLiteDatabase db,
            String bookId,
            String reason
    ) {
        registerMetadataRepairLocked(
                db,
                bookId,
                reason,
                "remote-delete:" + Instant.now()
        );
    }

    private void saveAnnotationLocked(SQLiteDatabase db, JSONObject row) {
        String annotationId = row.optString("id");
        if (!annotationId.isEmpty() && hasUnresolvedAnnotationOperationLocked(db, annotationId)) {
            return;
        }
        JSONObject metadata = row.optJSONObject("metadata") == null
                ? new JSONObject()
                : row.optJSONObject("metadata");
        metadata = preserveExistingLocalAudioLocked(db, annotationId, metadata);
        db.execSQL(
                "INSERT OR REPLACE INTO annotations (id, book_id, kind, source_text, note_text, color, chapter_locator, range_locator_json, metadata_json, updated_at, server_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                new Object[]{
                        row.optString("id"),
                        row.optString("book_id"),
                        row.optString("kind"),
                        row.optString("source_text"),
                        cleanOptionalText(row.optString("note_text")),
                        row.optString("color"),
                        row.optString("chapter_locator"),
                        row.optJSONObject("range_locator") == null ? "{}" : row.optJSONObject("range_locator").toString(),
                        metadata.toString(),
                        row.optString("updated_at"),
                        Math.max(0L, row.optLong("server_version", annotationId.startsWith("local-") ? 0L : 1L))
                }
        );
    }

    private JSONObject preserveExistingLocalAudioLocked(
            SQLiteDatabase db,
            String annotationId,
            JSONObject incomingMetadata
    ) {
        if (clean(annotationId).isEmpty()) {
            return incomingMetadata;
        }
        try (Cursor cursor = db.rawQuery(
                "SELECT metadata_json FROM annotations WHERE id=?",
                new String[]{annotationId}
        )) {
            if (!cursor.moveToFirst()) {
                return incomingMetadata;
            }
            JSONObject existing = objectOrEmpty(cursor.getString(0));
            JSONObject localAudio = existing.optJSONObject(
                    ReaderAudioNoteRepository.METADATA_KEY
            );
            if (localAudio == null) {
                return incomingMetadata;
            }
            JSONObject merged = new JSONObject(incomingMetadata.toString());
            merged.put(
                    ReaderAudioNoteRepository.METADATA_KEY,
                    new JSONObject(localAudio.toString())
            );
            return merged;
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot retain local reader audio metadata", error);
        }
    }

    private boolean hasUnresolvedAnnotationOperationLocked(SQLiteDatabase db, String annotationId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT payload_json FROM operation_queue WHERE operation_type IN ('annotation_updated','annotation_deleted') AND sync_status IN ('pending','retryable','conflict','permanent_failed','local_only')",
                null
        )) {
            while (cursor.moveToNext()) {
                try {
                    if (annotationId.equals(new JSONObject(cursor.getString(0)).optString("annotation_id"))) {
                        return true;
                    }
                } catch (Throwable ignored) {
                    // A malformed pending operation is reported by sync; it must not block other rows.
                }
            }
        }
        return false;
    }

    private void savePositionLocked(SQLiteDatabase db, JSONObject row) {
        savePositionLocked(db, row, true);
    }

    private void savePositionLocked(SQLiteDatabase db, JSONObject row, boolean preservePending) {
        String bookId = row.optString("book_id");
        if (preservePending && !bookId.isEmpty() && hasUnresolvedPositionOperationLocked(db, bookId)) {
            return;
        }
        db.execSQL(
                "INSERT OR REPLACE INTO positions (book_id, chapter_locator, page_index, total_pages, locator_json, updated_at, server_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                new Object[]{
                        bookId,
                        row.optString("chapter_locator"),
                        row.optInt("page_index"),
                        row.optInt("total_pages", 1),
                        row.optJSONObject("locator") == null ? "{}" : row.optJSONObject("locator").toString(),
                        row.optString("updated_at"),
                        Math.max(1L, row.optLong("server_version", 1L))
                }
        );
    }

    private boolean hasUnresolvedPositionOperationLocked(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT 1 FROM operation_queue WHERE book_id=? AND operation_type='reading_position_updated' AND sync_status IN ('pending','retryable','conflict','permanent_failed','local_only') LIMIT 1",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst();
        }
    }

    void enqueueOperation(String operationId, String operationType, String bookId, JSONObject payload) {
        enqueueOperation(operationId, operationType, bookId, payload, 0L);
    }

    void enqueueOperation(String operationId, String operationType, String bookId, JSONObject payload, long baseServerVersion) {
        insertPendingOperationLocked(
                getWritableDatabase(),
                operationId,
                operationType,
                bookId,
                payload,
                baseServerVersion,
                true
        );
    }

    private void insertPendingOperationLocked(
            SQLiteDatabase db,
            String operationId,
            String operationType,
            String bookId,
            JSONObject payload,
            long baseServerVersion,
            boolean replaceExisting
    ) {
        String verb = replaceExisting ? "INSERT OR REPLACE" : "INSERT";
        db.execSQL(
                verb + " INTO operation_queue (operation_id, operation_type, book_id, payload_json, "
                        + "sync_status, created_at, updated_at, retry_count, last_error, "
                        + "base_server_version, receipt_json) "
                        + "VALUES (?, ?, ?, ?, 'pending', datetime('now'), datetime('now'), 0, '', ?, '{}')",
                new Object[]{
                        clean(operationId),
                        clean(operationType),
                        clean(bookId),
                        payload == null ? "{}" : payload.toString(),
                        Math.max(0L, baseServerVersion)
                }
        );
    }

    void enqueueLatestPositionOperation(String operationId, String bookId, JSONObject payload) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            enqueueLatestPositionOperationLocked(db, operationId, bookId, payload);
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private void enqueueLatestPositionOperationLocked(
            SQLiteDatabase db,
            String operationId,
            String bookId,
            JSONObject payload
    ) {
        db.execSQL(
                "DELETE FROM operation_queue WHERE operation_type='reading_position_updated' "
                        + "AND book_id=? AND sync_status <> 'synced'",
                new Object[]{bookId}
        );
        db.execSQL(
                "INSERT INTO operation_queue (operation_id, operation_type, book_id, "
                        + "payload_json, sync_status, created_at, updated_at, retry_count, "
                        + "last_error, receipt_json) VALUES (?, 'reading_position_updated', ?, ?, "
                        + "'pending', datetime('now'), datetime('now'), 0, '', '{}')",
                new Object[]{operationId, bookId, payload.toString()}
        );
    }

    void saveLocalAnnotation(String operationId, JSONObject payload) {
        saveLocalAnnotationLocked(getWritableDatabase(), operationId, payload);
    }

    void saveLocalAnnotationAndEnqueue(
            String operationId,
            String operationType,
            String bookId,
            JSONObject payload
    ) {
        saveLocalAnnotationAndEnqueue(
                operationId,
                operationType,
                bookId,
                payload,
                payload
        );
    }

    void saveLocalAnnotationAndEnqueue(
            String operationId,
            String operationType,
            String bookId,
            JSONObject localPayload,
            JSONObject operationPayload
    ) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            saveLocalAnnotationLocked(db, operationId, localPayload);
            insertPendingOperationLocked(
                    db,
                    operationId,
                    operationType,
                    bookId,
                    operationPayload,
                    0L,
                    true
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private void saveLocalAnnotationLocked(
            SQLiteDatabase db,
            String operationId,
            JSONObject payload
    ) {
        JSONObject row = new JSONObject();
        try {
            JSONObject metadata = payload.optJSONObject("metadata") == null
                    ? new JSONObject()
                    : new JSONObject(payload.optJSONObject("metadata").toString());
            metadata.put("local_operation_id", operationId);
            metadata.put("sync_status", "pending");
            row.put("id", "local-" + operationId);
            row.put("book_id", payload.optString("book_id"));
            row.put("kind", payload.optString("kind"));
            row.put("source_text", payload.optString("source_text"));
            row.put("note_text", payload.optString("note_text"));
            row.put("color", payload.optString("color"));
            row.put("chapter_locator", payload.optString("chapter_locator"));
            row.put("range_locator", payload.optJSONObject("range_locator"));
            row.put("metadata", metadata);
            row.put("updated_at", Instant.now().toString());
            saveAnnotationLocked(db, row);
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot save local annotation", error);
        }
    }

    boolean deleteAnnotationForSync(String annotationId) {
        if (annotationId == null || annotationId.trim().isEmpty()) {
            return false;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            boolean requiresRemoteDelete = !annotationId.startsWith("local-");
            if (!requiresRemoteDelete) {
                String operationId = annotationId.substring("local-".length());
                db.execSQL("DELETE FROM operation_queue WHERE operation_id=?", new Object[]{operationId});
            }
            db.execSQL("DELETE FROM annotations WHERE id=?", new Object[]{annotationId});
            db.setTransactionSuccessful();
            return requiresRemoteDelete;
        } finally {
            db.endTransaction();
        }
    }

    void deleteAnnotationAndEnqueue(
            String operationId,
            String annotationId,
            String bookId,
            long baseServerVersion
    ) {
        String cleanAnnotationId = clean(annotationId);
        if (cleanAnnotationId.isEmpty()) {
            throw new IllegalArgumentException("annotation_id is required");
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            if (cleanAnnotationId.startsWith("local-")) {
                String createOperationId = cleanAnnotationId.substring("local-".length());
                db.execSQL(
                        "DELETE FROM operation_queue WHERE operation_id=?",
                        new Object[]{createOperationId}
                );
            } else {
                JSONObject payload = new JSONObject();
                try {
                    payload.put("book_id", clean(bookId));
                    payload.put("annotation_id", cleanAnnotationId);
                } catch (Throwable error) {
                    throw new IllegalStateException("Cannot build annotation deletion", error);
                }
                insertPendingOperationLocked(
                        db,
                        operationId,
                        "annotation_deleted",
                        bookId,
                        payload,
                        baseServerVersion,
                        false
                );
            }
            db.execSQL(
                    "DELETE FROM annotations WHERE id=?",
                    new Object[]{cleanAnnotationId}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    boolean updateAnnotationNoteForSync(String annotationId, String noteText) {
        if (annotationId == null || annotationId.trim().isEmpty()) {
            return false;
        }
        String value = noteText == null ? "" : noteText.trim();
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            boolean requiresRemoteUpdate = !annotationId.startsWith("local-");
            if (!requiresRemoteUpdate) {
                String operationId = annotationId.substring("local-".length());
                try (Cursor cursor = db.rawQuery(
                        "SELECT payload_json FROM operation_queue WHERE operation_id=?",
                        new String[]{operationId}
                )) {
                    if (cursor.moveToFirst()) {
                        JSONObject payload = new JSONObject(cursor.getString(0));
                        payload.put("note_text", value);
                        db.execSQL(
                                "UPDATE operation_queue SET payload_json=?, updated_at=datetime('now') WHERE operation_id=?",
                                new Object[]{payload.toString(), operationId}
                        );
                    }
                } catch (Throwable error) {
                    throw new IllegalStateException("Cannot update pending annotation", error);
                }
            }
            db.execSQL(
                    "UPDATE annotations SET note_text=?, updated_at=? WHERE id=?",
                    new Object[]{value, Instant.now().toString(), annotationId}
            );
            db.setTransactionSuccessful();
            return requiresRemoteUpdate;
        } finally {
            db.endTransaction();
        }
    }

    void updateAnnotationNoteAndEnqueue(
            String operationId,
            String annotationId,
            String bookId,
            String noteText,
            long baseServerVersion
    ) {
        String cleanAnnotationId = clean(annotationId);
        if (cleanAnnotationId.isEmpty()) {
            throw new IllegalArgumentException("annotation_id is required");
        }
        String value = noteText == null ? "" : noteText.trim();
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            if (cleanAnnotationId.startsWith("local-")) {
                String createOperationId = cleanAnnotationId.substring("local-".length());
                try (Cursor cursor = db.rawQuery(
                        "SELECT payload_json FROM operation_queue WHERE operation_id=?",
                        new String[]{createOperationId}
                )) {
                    if (!cursor.moveToFirst()) {
                        throw new IllegalStateException("pending annotation operation is missing");
                    }
                    JSONObject payload = new JSONObject(cursor.getString(0));
                    payload.put("note_text", value);
                    db.execSQL(
                            "UPDATE operation_queue SET payload_json=?, updated_at=datetime('now') "
                                    + "WHERE operation_id=?",
                            new Object[]{payload.toString(), createOperationId}
                    );
                } catch (Throwable error) {
                    throw new IllegalStateException("Cannot update pending annotation", error);
                }
            } else {
                JSONObject payload = new JSONObject();
                try {
                    payload.put("book_id", clean(bookId));
                    payload.put("annotation_id", cleanAnnotationId);
                    payload.put("note_text", value);
                } catch (Throwable error) {
                    throw new IllegalStateException("Cannot build annotation update", error);
                }
                insertPendingOperationLocked(
                        db,
                        operationId,
                        "annotation_updated",
                        bookId,
                        payload,
                        baseServerVersion,
                        false
                );
            }
            db.execSQL(
                    "UPDATE annotations SET note_text=?, updated_at=? WHERE id=?",
                    new Object[]{value, Instant.now().toString(), cleanAnnotationId}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void saveLocalPosition(JSONObject payload) {
        try {
            savePositionLocked(getWritableDatabase(), localPositionRow(payload), false);
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot save local position", error);
        }
    }

    void saveLocalPositionAndEnqueueLatest(
            String operationId,
            String bookId,
            JSONObject payload
    ) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            savePositionLocked(db, localPositionRow(payload), false);
            enqueueLatestPositionOperationLocked(db, operationId, bookId, payload);
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private JSONObject localPositionRow(JSONObject payload) {
        JSONObject row = new JSONObject();
        try {
            row.put("book_id", payload.optString("book_id"));
            row.put("chapter_locator", payload.optString("chapter_locator"));
            row.put("page_index", payload.optInt("page_index"));
            row.put("total_pages", payload.optInt("total_pages", 1));
            row.put("locator", payload.optJSONObject("locator"));
            row.put("updated_at", Instant.now().toString());
            return row;
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot build local position", error);
        }
    }

    void saveLookupCache(String bookId, String word, JSONObject payload) {
        String normalized = word == null ? "" : word.trim().toLowerCase(java.util.Locale.ROOT);
        if (bookId == null || bookId.isEmpty() || normalized.isEmpty() || payload == null) {
            return;
        }
        getWritableDatabase().execSQL(
                "INSERT OR REPLACE INTO lookup_cache (book_id, word, payload_json, updated_at) VALUES (?, ?, ?, ?)",
                new Object[]{bookId, normalized, payload.toString(), Instant.now().toString()}
        );
    }

    String lookupCache(String bookId, String word) {
        String normalized = word == null ? "" : word.trim().toLowerCase(java.util.Locale.ROOT);
        if (bookId == null || bookId.isEmpty() || normalized.isEmpty()) {
            return "";
        }
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT payload_json FROM lookup_cache WHERE book_id=? AND word=?",
                new String[]{bookId, normalized}
        )) {
            return cursor.moveToFirst() ? cursor.getString(0) : "";
        }
    }

    JSONArray pendingOperations(String deviceId) throws Exception {
        JSONArray operations = new JSONArray();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT q.operation_id, q.operation_type, q.book_id, q.payload_json, "
                        + "q.created_at, q.updated_at, q.base_server_version "
                        + "FROM operation_queue q "
                        + "WHERE q.sync_status IN ('pending','retryable') "
                        + "AND NOT EXISTS(SELECT 1 FROM book_import_queue i "
                        + "WHERE i.local_book_id=q.book_id) "
                        + "ORDER BY q.created_at ASC LIMIT 100",
                null
        )) {
            while (cursor.moveToNext()) {
                JSONObject item = new JSONObject();
                item.put("operation_id", cursor.getString(0));
                item.put("device_id", deviceId);
                item.put("operation_type", cursor.getString(1));
                item.put("book_id", cursor.getString(2));
                item.put("payload", new JSONObject(cursor.getString(3)));
                item.put("created_at", cursor.getString(4));
                item.put("updated_at", cursor.getString(5));
                long baseServerVersion = cursor.getLong(6);
                if (baseServerVersion > 0L) {
                    item.put("base_server_version", String.valueOf(baseServerVersion));
                }
                operations.put(item);
            }
        }
        return operations;
    }

    void markOperationResult(String operationId, boolean ok, String error) {
        SQLiteDatabase db = getWritableDatabase();
        if (ok) {
            db.execSQL(
                    "DELETE FROM operation_queue WHERE operation_id=? AND operation_type='reading_position_updated'",
                    new Object[]{operationId}
            );
        }
        db.execSQL(
                "UPDATE operation_queue SET sync_status=?, updated_at=datetime('now'), last_error=?, retry_count=retry_count+? WHERE operation_id=?",
                new Object[]{ok ? "synced" : "retryable", error == null ? "" : error, ok ? 0 : 1, operationId}
        );
    }

    void markOperationResult(String operationId, boolean ok, String error, JSONObject result) {
        markOperationResult(operationId, ok, error);
        if (!ok || result == null) {
            return;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            JSONObject analysis = result.optJSONObject("analysis");
            if (analysis != null && !result.optString("book_id").isEmpty()) {
                db.execSQL(
                        "UPDATE books SET analysis_state=?, analysis_updated_at=? WHERE id=?",
                        new Object[]{analysis.optString("state"), analysis.optString("updated_at"), result.optString("book_id")}
                );
            }
            if (!result.optString("kind").isEmpty()) {
                JSONObject reconciled = preserveLocalAudioMetadataLocked(
                        db,
                        "local-" + operationId,
                        result
                );
                db.execSQL("DELETE FROM annotations WHERE id=?", new Object[]{"local-" + operationId});
                saveAnnotationLocked(db, reconciled);
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void markOperationResult(JSONObject item) {
        if (item == null) {
            return;
        }
        String operationId = item.optString("operation_id").trim();
        if (operationId.isEmpty()) {
            return;
        }
        boolean ok = item.optBoolean("ok");
        String receiptStatus = item.optString("receipt_status").trim();
        boolean retryable = item.optBoolean("retryable");
        String status;
        if (ok) {
            status = "synced";
        } else if ("conflict".equals(receiptStatus) || item.optBoolean("conflict")) {
            status = "conflict";
        } else if (retryable) {
            status = "retryable";
        } else {
            status = "permanent_failed";
        }

        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            if (ok && "reading_position_updated".equals(item.optString("operation_type"))) {
                db.execSQL(
                        "DELETE FROM operation_queue WHERE operation_id=?",
                        new Object[]{operationId}
                );
            } else {
                db.execSQL(
                        "UPDATE operation_queue SET sync_status=?, updated_at=datetime('now'), last_error=?, "
                                + "retry_count=retry_count+?, receipt_json=? WHERE operation_id=?",
                        new Object[]{
                                status,
                                item.optString("error"),
                                ok || "conflict".equals(status) ? 0 : 1,
                                item.toString(),
                                operationId
                        }
                );
            }

            if (ok) {
                reconcileOperationResultLocked(db, operationId, item.optJSONObject("result"));
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private void reconcileOperationResultLocked(SQLiteDatabase db, String operationId, JSONObject result) {
        if (result == null) {
            return;
        }
        JSONObject analysis = result.optJSONObject("analysis");
        if (analysis != null && !result.optString("book_id").isEmpty()) {
            db.execSQL(
                    "UPDATE books SET analysis_state=?, analysis_updated_at=? WHERE id=?",
                    new Object[]{analysis.optString("state"), analysis.optString("updated_at"), result.optString("book_id")}
            );
        }
        JSONObject organization = result.optJSONObject("organization");
        if (organization != null && !result.optString("book_id").isEmpty()) {
            db.execSQL(
                    "UPDATE books SET favorite=?, custom_category=?, tags_json=? WHERE id=?",
                    new Object[]{
                            organization.optBoolean("favorite") ? 1 : 0,
                            organization.optString("custom_category"),
                            organization.optJSONArray("tags") == null ? "[]" : organization.optJSONArray("tags").toString(),
                            result.optString("book_id")
                    }
            );
        }
        if (result.optBoolean("deleted") && !result.optString("id").isEmpty()) {
            db.execSQL("DELETE FROM annotations WHERE id=?", new Object[]{result.optString("id")});
        } else if (!result.optString("kind").isEmpty()) {
            JSONObject reconciled = preserveLocalAudioMetadataLocked(
                    db,
                    "local-" + operationId,
                    result
            );
            db.execSQL("DELETE FROM annotations WHERE id=?", new Object[]{"local-" + operationId});
            saveAnnotationLocked(db, reconciled);
        } else if (!result.optString("chapter_locator").isEmpty() && !result.optString("book_id").isEmpty()) {
            savePositionLocked(db, result, false);
        }
    }

    private JSONObject preserveLocalAudioMetadataLocked(
            SQLiteDatabase db,
            String localAnnotationId,
            JSONObject serverResult
    ) {
        JSONObject localAudio = null;
        try (Cursor cursor = db.rawQuery(
                "SELECT metadata_json FROM annotations WHERE id=?",
                new String[]{localAnnotationId}
        )) {
            if (cursor.moveToFirst()) {
                JSONObject localMetadata = objectOrEmpty(cursor.getString(0));
                JSONObject candidate = localMetadata.optJSONObject(
                        ReaderAudioNoteRepository.METADATA_KEY
                );
                if (candidate != null) {
                    localAudio = new JSONObject(candidate.toString());
                }
            }
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot preserve local reader audio", error);
        }
        try {
            JSONObject reconciled = new JSONObject(serverResult.toString());
            JSONObject metadata = reconciled.optJSONObject("metadata") == null
                    ? new JSONObject()
                    : new JSONObject(reconciled.optJSONObject("metadata").toString());
            if (localAudio != null) {
                metadata.put(ReaderAudioNoteRepository.METADATA_KEY, localAudio);
            }
            JSONObject audioNote = reconciled.optJSONObject("audio_note");
            if (audioNote != null && !clean(audioNote.optString("id")).isEmpty()) {
                JSONObject voiceNote = metadata.optJSONObject("voice_note") == null
                        ? new JSONObject()
                        : new JSONObject(metadata.optJSONObject("voice_note").toString());
                voiceNote.put("audio_note_id", clean(audioNote.optString("id")));
                metadata.put("voice_note", voiceNote);
            }
            reconciled.put("metadata", metadata);
            return reconciled;
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot merge local reader audio", error);
        }
    }

    void setBookEpubLocalPath(String bookId, String localPath) {
        getWritableDatabase().execSQL(
                "UPDATE books SET epub_local_path=? WHERE id=?",
                new Object[]{localPath == null ? "" : localPath, bookId}
        );
    }

    void setBookCoverLocalPath(String bookId, String localPath) {
        getWritableDatabase().execSQL(
                "UPDATE books SET cover_local_path=? WHERE id=?",
                new Object[]{localPath == null ? "" : localPath, bookId}
        );
    }

    void setBookDisplayVariantsLocalPath(String bookId, String localPath) {
        getWritableDatabase().execSQL(
                "UPDATE books SET display_variants_local_path=? WHERE id=?",
                new Object[]{localPath == null ? "" : localPath, bookId}
        );
    }

    boolean shouldAttemptInitialAssetRefresh(String bookId, String assetType) {
        if (!isAssetType(assetType)) {
            return false;
        }
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT 1 FROM asset_refresh_queue "
                        + "WHERE book_id=? AND asset_type=? AND state='pending' AND retry_count=0 LIMIT 1",
                new String[]{bookId, assetType}
        )) {
            return cursor.moveToFirst();
        }
    }

    AssetRefreshRow assetRefresh(String bookId, String assetType) {
        if (!isAssetType(assetType)) {
            return null;
        }
        return assetRefreshLocked(getReadableDatabase(), bookId, assetType);
    }

    List<AssetRefreshRow> dueAssetRefreshes(int requestedLimit, long nowMillis) {
        int limit = Math.max(1, Math.min(12, requestedLimit));
        List<AssetRefreshRow> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT book_id, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at "
                        + "FROM asset_refresh_queue "
                        + "WHERE state='pending' AND next_attempt_at<=? "
                        + "ORDER BY next_attempt_at ASC, retry_count ASC, updated_at ASC LIMIT " + limit,
                new String[]{String.valueOf(Math.max(0L, nowMillis))}
        )) {
            while (cursor.moveToNext()) {
                rows.add(assetRefreshRow(cursor));
            }
        }
        return rows;
    }

    AssetRefreshRow dueBookSourceRefresh(String bookId, long nowMillis) {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            return null;
        }
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT book_id, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at "
                        + "FROM asset_refresh_queue "
                        + "WHERE book_id=? AND asset_type=? AND state='pending' "
                        + "AND next_attempt_at<=? LIMIT 1",
                new String[]{
                        cleanBookId,
                        ASSET_SOURCE,
                        String.valueOf(Math.max(0L, nowMillis))
                }
        )) {
            return cursor.moveToFirst() ? assetRefreshRow(cursor) : null;
        }
    }

    void markAssetRefreshSucceeded(String bookId, String assetType, String localPath) {
        if (!isAssetType(assetType) || clean(bookId).isEmpty() || clean(localPath).isEmpty()) {
            return;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            String column = ASSET_EPUB.equals(assetType)
                    ? "epub_local_path"
                    : ASSET_COVER.equals(assetType)
                    ? "cover_local_path"
                    : ASSET_SOURCE.equals(assetType)
                    ? "source_local_path"
                    : "display_variants_local_path";
            db.execSQL(
                    "UPDATE books SET " + column + "=? WHERE id=?",
                    new Object[]{localPath, bookId}
            );
            db.execSQL(
                    "DELETE FROM asset_refresh_queue WHERE book_id=? AND asset_type=?",
                    new Object[]{bookId, assetType}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void markAssetRefreshFailed(
            String bookId,
            String assetType,
            String humanError,
            long failedAtMillis
    ) {
        if (!isAssetType(assetType)) {
            return;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            AssetRefreshRow current = assetRefreshLocked(db, bookId, assetType);
            if (current == null) {
                return;
            }
            int retryCount = Math.min(30, current.retryCount + 1);
            long nextAttemptAt = Math.max(0L, failedAtMillis) + assetRetryDelayMillis(retryCount);
            String error = clean(humanError);
            if (error.length() > 240) {
                error = error.substring(0, 240);
            }
            db.execSQL(
                    "UPDATE asset_refresh_queue SET state='pending', retry_count=?, last_error=?, "
                            + "next_attempt_at=?, updated_at=datetime('now') "
                            + "WHERE book_id=? AND asset_type=? AND state='pending'",
                    new Object[]{retryCount, error, nextAttemptAt, bookId, assetType}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void markAssetRefreshTerminal(
            String bookId,
            String assetType,
            String humanError
    ) {
        if (!isAssetType(assetType) || clean(bookId).isEmpty()) {
            return;
        }
        String error = clean(humanError);
        if (error.length() > 240) {
            error = error.substring(0, 240);
        }
        getWritableDatabase().execSQL(
                "UPDATE asset_refresh_queue SET state='terminal', last_error=?, next_attempt_at=0, "
                        + "updated_at=datetime('now') WHERE book_id=? AND asset_type=?",
                new Object[]{error, bookId, assetType}
        );
    }

    static long assetRetryDelayMillis(int retryCount) {
        int exponent = Math.max(0, Math.min(20, retryCount - 1));
        long delay = ASSET_RETRY_BASE_MILLIS << exponent;
        return Math.min(ASSET_RETRY_MAX_MILLIS, delay);
    }

    private AssetRefreshRow assetRefreshLocked(SQLiteDatabase db, String bookId, String assetType) {
        try (Cursor cursor = db.rawQuery(
                "SELECT book_id, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at "
                        + "FROM asset_refresh_queue WHERE book_id=? AND asset_type=?",
                new String[]{bookId, assetType}
        )) {
            return cursor.moveToFirst() ? assetRefreshRow(cursor) : null;
        }
    }

    private AssetRefreshRow assetRefreshRow(Cursor cursor) {
        return new AssetRefreshRow(
                cursor.getString(0),
                cursor.getString(1),
                cursor.getString(2),
                cursor.getString(3),
                cursor.getLong(4),
                cursor.getString(5),
                cursor.getString(6),
                cursor.getInt(7),
                cursor.getString(8),
                cursor.getLong(9)
        );
    }

    long earliestAssetAttemptAt() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT MIN(next_attempt_at) FROM asset_refresh_queue WHERE state='pending'",
                null
        )) {
            return cursor.moveToFirst() && !cursor.isNull(0) ? Math.max(0L, cursor.getLong(0)) : -1L;
        }
    }

    String assetIssueSummary(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT asset_type, last_error FROM asset_refresh_queue "
                        + "WHERE book_id=? AND state='terminal' ORDER BY updated_at DESC LIMIT 1",
                new String[]{bookId}
        )) {
            if (!cursor.moveToFirst()) {
                return "";
            }
            return formatAssetIssueSummary(cursor.getString(0), cursor.getString(1));
        }
    }

    private static String formatAssetIssueSummary(String assetType, String lastError) {
        String cleanType = clean(assetType);
        if (cleanType.isEmpty()) {
            return "";
        }
        String label = ASSET_EPUB.equals(cleanType)
                ? "EPUB"
                : ASSET_DISPLAY_VARIANTS.equals(cleanType)
                ? "简繁显示文件"
                : ASSET_SOURCE.equals(cleanType) ? "原书" : "封面";
        String error = clean(lastError);
        return error.isEmpty() ? label + "缓存异常" : error;
    }

    int terminalAssetIssueCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT count(*) FROM asset_refresh_queue WHERE state='terminal'",
                null
        )) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    private MetadataRepairRow metadataRepairLocked(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT book_id, reason, contract_marker, state, retry_count, last_error, "
                        + "next_attempt_at FROM metadata_repair_queue WHERE book_id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? metadataRepairRow(cursor) : null;
        }
    }

    private MetadataRepairRow metadataRepairRow(Cursor cursor) {
        return new MetadataRepairRow(
                cursor.getString(0),
                cursor.getString(1),
                cursor.getString(2),
                cursor.getString(3),
                cursor.getInt(4),
                cursor.getString(5),
                cursor.getLong(6)
        );
    }

    List<MetadataRepairRow> dueMetadataRepairs(int requestedLimit, long nowMillis) {
        int limit = Math.max(1, Math.min(12, requestedLimit));
        List<MetadataRepairRow> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT book_id, reason, contract_marker, state, retry_count, last_error, "
                        + "next_attempt_at FROM metadata_repair_queue "
                        + "WHERE state='pending' AND next_attempt_at<=? "
                        + "ORDER BY next_attempt_at ASC, retry_count ASC, updated_at ASC LIMIT " + limit,
                new String[]{String.valueOf(Math.max(0L, nowMillis))}
        )) {
            while (cursor.moveToNext()) {
                rows.add(metadataRepairRow(cursor));
            }
        }
        return rows;
    }

    void markMetadataRepairSucceeded(String bookId) {
        getWritableDatabase().execSQL(
                "DELETE FROM metadata_repair_queue WHERE book_id=?",
                new Object[]{bookId}
        );
    }

    void markMetadataRepairFailed(String bookId, String humanError, long failedAtMillis) {
        SQLiteDatabase db = getWritableDatabase();
        MetadataRepairRow current = metadataRepairLocked(db, bookId);
        if (current == null || !"pending".equals(current.state)) {
            return;
        }
        int retryCount = Math.min(30, current.retryCount + 1);
        long nextAttemptAt = Math.max(0L, failedAtMillis) + assetRetryDelayMillis(retryCount);
        String error = shortError(humanError);
        db.execSQL(
                "UPDATE metadata_repair_queue SET retry_count=?, last_error=?, next_attempt_at=?, "
                        + "updated_at=datetime('now') WHERE book_id=? AND state='pending'",
                new Object[]{retryCount, error, nextAttemptAt, bookId}
        );
    }

    void markMetadataRepairTerminal(String bookId, String humanError) {
        getWritableDatabase().execSQL(
                "UPDATE metadata_repair_queue SET state='terminal', last_error=?, next_attempt_at=0, "
                        + "updated_at=datetime('now') WHERE book_id=?",
                new Object[]{shortError(humanError), bookId}
        );
    }

    long earliestMetadataRepairAttemptAt() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT MIN(next_attempt_at) FROM metadata_repair_queue WHERE state='pending'",
                null
        )) {
            return cursor.moveToFirst() && !cursor.isNull(0) ? Math.max(0L, cursor.getLong(0)) : -1L;
        }
    }

    boolean hasMetadataRepairWork() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT EXISTS(SELECT 1 FROM metadata_repair_queue WHERE state='pending')",
                null
        )) {
            return cursor.moveToFirst() && cursor.getInt(0) == 1;
        }
    }

    int terminalMetadataRepairIssueCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT count(*) FROM metadata_repair_queue WHERE state='terminal'",
                null
        )) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    BookRetryResult retryTerminalBookWork(String bookId) {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            return BookRetryResult.EMPTY;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        int assetRows;
        int metadataRows;
        try {
            assetRows = terminalRowCountLocked(
                    db,
                    "asset_refresh_queue",
                    cleanBookId
            );
            metadataRows = terminalRowCountLocked(
                    db,
                    "metadata_repair_queue",
                    cleanBookId
            );
            db.execSQL(
                    "UPDATE asset_refresh_queue SET state='pending', retry_count=0, "
                            + "last_error='', next_attempt_at=0, updated_at=datetime('now') "
                            + "WHERE book_id=? AND state='terminal'",
                    new Object[]{cleanBookId}
            );
            db.execSQL(
                    "UPDATE metadata_repair_queue SET state='pending', retry_count=0, "
                            + "last_error='', next_attempt_at=0, updated_at=datetime('now') "
                            + "WHERE book_id=? AND state='terminal'",
                    new Object[]{cleanBookId}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
        return new BookRetryResult(assetRows, metadataRows);
    }

    private int terminalRowCountLocked(
            SQLiteDatabase db,
            String table,
            String bookId
    ) {
        try (Cursor cursor = db.rawQuery(
                "SELECT count(*) FROM " + table + " WHERE book_id=? AND state='terminal'",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    private static String shortError(String value) {
        String error = clean(value);
        return error.length() > 240 ? error.substring(0, 240) : error;
    }

    void setBookAnalysisState(String bookId, String state) {
        getWritableDatabase().execSQL(
                "UPDATE books SET analysis_state=?, analysis_updated_at=? WHERE id=?",
                new Object[]{state == null ? "not_requested" : state, Instant.now().toString(), bookId}
        );
    }

    void setBookAnalysisStateAndEnqueue(
            String operationId,
            String bookId,
            String state,
            JSONObject operationPayload
    ) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.execSQL(
                    "UPDATE books SET analysis_state=?, analysis_updated_at=? WHERE id=?",
                    new Object[]{
                            state == null ? "not_requested" : state,
                            Instant.now().toString(),
                            clean(bookId)
                    }
            );
            insertPendingOperationLocked(
                    db,
                    operationId,
                    "living_book_analysis_requested",
                    bookId,
                    operationPayload,
                    0L,
                    true
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void setBookOrganization(String bookId, boolean favorite, String customCategory, JSONArray tags) {
        getWritableDatabase().execSQL(
                "UPDATE books SET favorite=?, custom_category=?, tags_json=? WHERE id=?",
                new Object[]{favorite ? 1 : 0, customCategory == null ? "" : customCategory.trim(), tags == null ? "[]" : tags.toString(), bookId}
        );
    }

    void setBookOrganizationAndEnqueue(
            String operationId,
            String bookId,
            boolean favorite,
            String customCategory,
            JSONArray tags
    ) {
        String category = customCategory == null ? "" : customCategory.trim();
        JSONArray safeTags = tags == null ? new JSONArray() : tags;
        JSONObject payload = new JSONObject();
        try {
            payload.put("book_id", clean(bookId));
            payload.put("favorite", favorite);
            payload.put("custom_category", category);
            payload.put("tags", safeTags);
        } catch (Throwable error) {
            throw new IllegalStateException("Cannot build book organization operation", error);
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.execSQL(
                    "UPDATE books SET favorite=?, custom_category=?, tags_json=? WHERE id=?",
                    new Object[]{favorite ? 1 : 0, category, safeTags.toString(), clean(bookId)}
            );
            insertPendingOperationLocked(
                    db,
                    operationId,
                    "book_organization_updated",
                    bookId,
                    payload,
                    0L,
                    false
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void markBookOpened(String bookId) {
        getWritableDatabase().execSQL(
                "UPDATE books SET last_opened_at=? WHERE id=?",
                new Object[]{Instant.now().toString(), bookId}
        );
    }

    void clearBookLocalFiles(String bookId) {
        getWritableDatabase().execSQL(
                "UPDATE books SET epub_local_path='', cover_local_path='', display_variants_local_path='' WHERE id=?",
                new Object[]{bookId}
        );
    }

    LocalCopyRemovalResult removeBookLocalCopy(String bookId) throws Exception {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            throw new IllegalArgumentException("缺少要删除的书籍");
        }
        SQLiteDatabase db = getWritableDatabase();
        LocalCopySnapshot snapshot = localCopySnapshot(db, cleanBookId);
        requireRemovableLocalCopy(db, cleanBookId, snapshot.importState);
        List<File> controlledFiles = new ArrayList<>();
        Set<String> uniquePaths = new HashSet<>();
        for (String rawPath : snapshot.paths) {
            File target = controlledLocalCopy(rawPath);
            if (target == null || !uniquePaths.add(target.getPath()) || !target.exists()) {
                continue;
            }
            if (!target.isFile()) {
                throw new IllegalStateException("本地副本不是普通文件，已停止删除");
            }
            controlledFiles.add(target);
        }

        db.beginTransaction();
        try {
            LocalCopySnapshot current = localCopySnapshot(db, cleanBookId);
            requireRemovableLocalCopy(db, cleanBookId, current.importState);
            if (!snapshot.samePaths(current)) {
                throw new IllegalStateException("本地副本刚刚发生变化，请刷新书架后重试");
            }

            for (File target : controlledFiles) {
                db.execSQL(
                        "INSERT OR IGNORE INTO local_file_cleanup_queue ("
                                + "canonical_path, book_id, attempt_count, last_error, "
                                + "created_at, updated_at"
                                + ") VALUES (?, ?, 0, '', datetime('now'), datetime('now'))",
                        new Object[]{target.getCanonicalPath(), cleanBookId}
                );
            }
            db.execSQL(
                    "UPDATE books SET epub_local_path='', cover_local_path='', "
                            + "display_variants_local_path='', source_local_path='' WHERE id=?",
                    new Object[]{cleanBookId}
            );
            db.execSQL(
                    "DELETE FROM asset_refresh_queue WHERE book_id=? AND asset_type IN (?,?,?,?)",
                    new Object[]{
                            cleanBookId,
                            ASSET_EPUB,
                            ASSET_COVER,
                            ASSET_DISPLAY_VARIANTS,
                            ASSET_SOURCE
                    }
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }

        LocalFileCleanupResult cleanup = drainLocalFileCleanupQueue();
        return new LocalCopyRemovalResult(
                cleanup.removedFileCount,
                pendingLocalFileCleanupCount()
        );
    }

    LocalFileCleanupResult drainLocalFileCleanupQueue() {
        List<String> pendingPaths = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT canonical_path FROM local_file_cleanup_queue "
                        + "ORDER BY created_at ASC, canonical_path ASC LIMIT "
                        + MAX_LOCAL_FILE_CLEANUPS_PER_DRAIN,
                null
        )) {
            while (cursor.moveToNext()) {
                pendingPaths.add(clean(cursor.getString(0)));
            }
        }

        int removed = 0;
        for (String queuedPath : pendingPaths) {
            if (queuedPath.isEmpty()) {
                continue;
            }
            try {
                File target = controlledLocalCopy(queuedPath);
                if (target == null) {
                    throw new IllegalStateException("清理路径为空");
                }
                boolean existed = target.exists();
                if (existed && !target.isFile()) {
                    throw new IllegalStateException("待清理路径不是普通文件");
                }
                if (existed && !target.delete()) {
                    throw new IllegalStateException("文件删除失败");
                }
                getWritableDatabase().execSQL(
                        "DELETE FROM local_file_cleanup_queue WHERE canonical_path=?",
                        new Object[]{queuedPath}
                );
                if (existed) {
                    removed += 1;
                }
            } catch (Throwable error) {
                getWritableDatabase().execSQL(
                        "UPDATE local_file_cleanup_queue SET attempt_count=attempt_count+1, "
                                + "last_error=?, updated_at=datetime('now') WHERE canonical_path=?",
                        new Object[]{shortCleanupError(error), queuedPath}
                );
            }
        }
        return new LocalFileCleanupResult(removed, pendingLocalFileCleanupCount());
    }

    int pendingLocalFileCleanupCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT count(*) FROM local_file_cleanup_queue",
                null
        )) {
            return cursor.moveToFirst() ? Math.max(0, cursor.getInt(0)) : 0;
        }
    }

    private static String shortCleanupError(Throwable error) {
        String type = error == null ? "cleanup_error" : error.getClass().getSimpleName();
        String message = error == null ? "" : clean(error.getMessage());
        String detail = message.isEmpty() ? type : type + ": " + message;
        return detail.length() <= 240 ? detail : detail.substring(0, 240);
    }

    private LocalCopySnapshot localCopySnapshot(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT epub_local_path, cover_local_path, display_variants_local_path, "
                        + "source_local_path, import_state FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            if (!cursor.moveToFirst()) {
                throw new IllegalStateException("这本书已经不在手机书架");
            }
            return new LocalCopySnapshot(
                    new String[]{
                            clean(cursor.getString(0)),
                            clean(cursor.getString(1)),
                            clean(cursor.getString(2)),
                            clean(cursor.getString(3))
                    },
                    clean(cursor.getString(4))
            );
        }
    }

    private void requireRemovableLocalCopy(
            SQLiteDatabase db,
            String bookId,
            String importState
    ) {
        if (!"remote".equals(importState) && !"imported".equals(importState)) {
            throw new IllegalStateException(
                    "这份原书还没有安全归入 Mac 主库，手机目前是唯一可靠副本，不能删除"
            );
        }
        try (Cursor cursor = db.rawQuery(
                "SELECT 1 FROM book_import_queue WHERE local_book_id=? LIMIT 1",
                new String[]{bookId}
        )) {
            if (cursor.moveToFirst()) {
                throw new IllegalStateException(
                        "这份原书仍在等待归档，不能删除手机上的唯一副本"
                );
            }
        }
    }

    boolean requestBookSourceDownload(String bookId) {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            return false;
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            String sourceKind;
            String sourceHash;
            long sourceByteSize;
            String importState;
            try (Cursor cursor = db.rawQuery(
                    "SELECT source_kind, source_hash, source_byte_size, import_state "
                            + "FROM books WHERE id=?",
                    new String[]{cleanBookId}
            )) {
                if (!cursor.moveToFirst()) {
                    return false;
                }
                sourceKind = clean(cursor.getString(0)).toLowerCase(Locale.ROOT);
                sourceHash = clean(cursor.getString(1)).toLowerCase(Locale.ROOT);
                sourceByteSize = cursor.getLong(2);
                importState = clean(cursor.getString(3));
            }
            if (!"remote".equals(importState) && !"imported".equals(importState)) {
                return false;
            }
            if ((!"epub".equals(sourceKind) && !"pdf".equals(sourceKind))
                    || !sourceHash.matches("[0-9a-f]{64}")
                    || sourceByteSize <= 0L) {
                return false;
            }
            String sourceUrl = "/v1/android/books/"
                    + android.net.Uri.encode(cleanBookId)
                    + "/source";
            registerAssetRefreshLocked(
                    db,
                    cleanBookId,
                    ASSET_SOURCE,
                    sourceUrl,
                    sourceHash,
                    sourceByteSize,
                    sourceHash + ":" + sourceByteSize,
                    true
            );
            db.execSQL(
                    "UPDATE asset_refresh_queue SET state='pending', retry_count=0, "
                            + "last_error='', next_attempt_at=0, updated_at=datetime('now') "
                            + "WHERE book_id=? AND asset_type=?",
                    new Object[]{cleanBookId, ASSET_SOURCE}
            );
            db.setTransactionSuccessful();
            return true;
        } finally {
            db.endTransaction();
        }
    }

    private File controlledLocalCopy(String rawPath) throws Exception {
        String path = clean(rawPath);
        if (path.isEmpty()) {
            return null;
        }
        File root = appFilesDirectory.getCanonicalFile();
        File target = new File(path).getCanonicalFile();
        String rootPrefix = root.getPath() + File.separator;
        if (!target.getPath().startsWith(rootPrefix)) {
            throw new IllegalStateException("本地副本不在 Click 私有目录，已停止删除");
        }
        String relative = target.getPath().substring(rootPrefix.length());
        boolean controlled = relative.startsWith("click-epub-cache" + File.separator)
                || relative.startsWith("click-source-library" + File.separator)
                || relative.startsWith("click-cover-cache" + File.separator)
                || relative.startsWith("click-display-variant-cache" + File.separator)
                || relative.startsWith(BookImportIngress.SOURCE_ROOT_NAME + File.separator);
        if (!controlled) {
            throw new IllegalStateException("本地副本不属于 Click 受控缓存，已停止删除");
        }
        return target;
    }

    void registerLocalBookImport(
            String localBookId,
            String title,
            String author,
            String sourceKind,
            String sourceLocalPath,
            String sourceHash,
            long sourceByteSize,
            String filename,
            List<EpubOfflineIndexer.Chapter> offlineChapters
    ) throws Exception {
        String cleanBookId = clean(localBookId);
        String cleanKind = clean(sourceKind).toLowerCase(Locale.ROOT);
        String cleanHash = clean(sourceHash).toLowerCase(Locale.ROOT);
        String cleanFilename = clean(filename);
        File source = appPrivateOriginal(sourceLocalPath);
        if (cleanBookId.isEmpty()
                || (!"epub".equals(cleanKind) && !"pdf".equals(cleanKind))
                || !cleanHash.matches("[0-9a-f]{64}")
                || sourceByteSize <= 0L
                || source.length() != sourceByteSize
                || cleanFilename.isEmpty()
                || !cleanFilename.equals(new File(cleanFilename).getName())
                || !cleanFilename.toLowerCase(Locale.ROOT).endsWith("." + cleanKind)) {
            throw new IllegalArgumentException("invalid local book import contract");
        }

        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.execSQL(
                    "INSERT OR IGNORE INTO books ("
                            + "id, title, author, updated_at, source_kind, source_local_path, "
                            + "source_hash, source_byte_size, import_state, canonical_id"
                            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '')",
                    new Object[]{
                            cleanBookId,
                            clean(title),
                            clean(author),
                            Instant.now().toString(),
                            cleanKind,
                            source.getAbsolutePath(),
                            cleanHash,
                            sourceByteSize
                    }
            );
            db.execSQL(
                    "UPDATE books SET title=COALESCE(NULLIF(title, ''), ?), "
                            + "author=COALESCE(NULLIF(author, ''), ?), source_kind=?, "
                            + "source_local_path=?, source_hash=?, source_byte_size=?, "
                            + "import_state='pending', canonical_id='' WHERE id=?",
                    new Object[]{
                            clean(title),
                            clean(author),
                            cleanKind,
                            source.getAbsolutePath(),
                            cleanHash,
                            sourceByteSize,
                            cleanBookId
                    }
            );
            db.execSQL(
                    "INSERT OR REPLACE INTO book_import_queue ("
                            + "local_book_id, source_kind, source_local_path, source_hash, "
                            + "source_byte_size, filename, state, last_error, created_at, updated_at"
                            + ") VALUES (?, ?, ?, ?, ?, ?, 'pending', '', "
                            + "COALESCE((SELECT created_at FROM book_import_queue WHERE local_book_id=?), "
                            + "datetime('now')), datetime('now'))",
                    new Object[]{
                            cleanBookId,
                            cleanKind,
                            source.getAbsolutePath(),
                            cleanHash,
                            sourceByteSize,
                            cleanFilename,
                            cleanBookId
                    }
            );
            if ("epub".equals(cleanKind)) {
                if (offlineChapters == null || offlineChapters.isEmpty()) {
                    throw new IllegalArgumentException(
                            "local EPUB import requires an offline chapter index"
                    );
                }
                db.execSQL("DELETE FROM chapters WHERE book_id=?", new Object[]{cleanBookId});
                for (EpubOfflineIndexer.Chapter chapter : offlineChapters) {
                    db.execSQL(
                            "INSERT INTO chapters (book_id, chapter_index, locator, title, html) "
                                    + "VALUES (?, ?, ?, ?, ?)",
                            new Object[]{
                                    cleanBookId,
                                    chapter.index,
                                    chapter.locator,
                                    chapter.title,
                                    chapter.html
                            }
                    );
                }
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    BookImportRow nextPendingBookImport() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT local_book_id, source_kind, source_local_path, source_hash, "
                        + "source_byte_size, filename, state, last_error "
                        + "FROM book_import_queue WHERE state='pending' "
                        + "ORDER BY updated_at ASC, local_book_id ASC LIMIT 1",
                null
        )) {
            return cursor.moveToFirst() ? bookImportRow(cursor) : null;
        }
    }

    int pendingBookImportCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT count(*) FROM book_import_queue WHERE state='pending'",
                null
        )) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    void markBookImportFailed(String localBookId, String humanError, boolean terminal) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            String state = terminal ? IMPORT_TERMINAL : IMPORT_PENDING;
            String bookId = clean(localBookId);
            db.execSQL(
                    "UPDATE book_import_queue SET state=?, last_error=?, updated_at=datetime('now') "
                            + "WHERE local_book_id=?",
                    new Object[]{state, shortError(humanError), bookId}
            );
            db.execSQL(
                    "UPDATE books SET import_state=? WHERE id=?",
                    new Object[]{state, bookId}
            );
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void completeBookImport(
            String localBookId,
            String expectedSourceHash,
            String canonicalBookId
    ) throws Exception {
        String localId = clean(localBookId);
        String expectedHash = clean(expectedSourceHash).toLowerCase(Locale.ROOT);
        String canonicalId = clean(canonicalBookId);
        if (localId.isEmpty()
                || canonicalId.isEmpty()
                || !expectedHash.matches("[0-9a-f]{64}")) {
            throw new IllegalArgumentException("invalid canonical import receipt");
        }

        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            BookImportRow queued = bookImportLocked(db, localId);
            if (queued == null) {
                if (expectedHash.equals(bookSourceHashLocked(db, canonicalId))) {
                    db.setTransactionSuccessful();
                    return;
                }
                throw new IllegalStateException("book import queue row is missing");
            }
            if (!expectedHash.equals(queued.sourceHash)) {
                throw new IllegalStateException("book import hash changed before merge");
            }
            if (!expectedHash.equals(bookSourceHashLocked(db, localId))) {
                throw new IllegalStateException("local book row is missing or changed before merge");
            }
            File original = appPrivateOriginal(queued.sourceLocalPath);
            if (original.length() != queued.sourceByteSize
                    || !queued.sourceHash.equals(VerifiedAssetFile.sha256(original))) {
                throw new IllegalStateException("book import source changed before merge");
            }
            if (localId.equals(canonicalId)) {
                db.execSQL(
                        "UPDATE books SET import_state='imported', canonical_id=? WHERE id=?",
                        new Object[]{canonicalId, localId}
                );
                db.execSQL(
                        "DELETE FROM book_import_queue WHERE local_book_id=?",
                        new Object[]{localId}
                );
                db.setTransactionSuccessful();
                return;
            }

            db.execSQL(
                    "INSERT OR IGNORE INTO books ("
                            + "id, title, author, epub_url, epub_local_path, cover_url, "
                            + "cover_local_path, publication_json, position_json, updated_at, "
                            + "reading_profile, compatibility_json, display_variants_url, "
                            + "display_variants_local_path, analysis_state, analysis_updated_at, "
                            + "favorite, custom_category, tags_json, last_opened_at, server_version, "
                            + "epub_hash, epub_byte_size, source_kind, source_local_path, source_hash, "
                            + "source_byte_size, import_state, canonical_id"
                            + ") SELECT ?, title, author, epub_url, epub_local_path, cover_url, "
                            + "cover_local_path, publication_json, position_json, updated_at, "
                            + "reading_profile, compatibility_json, display_variants_url, "
                            + "display_variants_local_path, analysis_state, analysis_updated_at, "
                            + "favorite, custom_category, tags_json, last_opened_at, server_version, "
                            + "epub_hash, epub_byte_size, source_kind, source_local_path, source_hash, "
                            + "source_byte_size, 'imported', ? FROM books WHERE id=?",
                    new Object[]{canonicalId, canonicalId, localId}
            );
            db.execSQL(
                    "UPDATE books SET source_kind=?, source_local_path=?, source_hash=?, "
                            + "source_byte_size=?, import_state='imported', canonical_id=? WHERE id=?",
                    new Object[]{
                            queued.sourceKind,
                            original.getAbsolutePath(),
                            queued.sourceHash,
                            queued.sourceByteSize,
                            canonicalId,
                            canonicalId
                    }
            );

            mergeBookReferencesLocked(db, localId, canonicalId);
            db.execSQL(
                    "DELETE FROM book_import_queue WHERE local_book_id=?",
                    new Object[]{localId}
            );
            db.execSQL("DELETE FROM books WHERE id=?", new Object[]{localId});
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private void mergeBookReferencesLocked(
            SQLiteDatabase db,
            String localBookId,
            String canonicalBookId
    ) throws Exception {
        db.execSQL(
                "INSERT OR IGNORE INTO chapters (book_id, chapter_index, locator, title, html) "
                        + "SELECT ?, chapter_index, locator, title, html FROM chapters WHERE book_id=?",
                new Object[]{canonicalBookId, localBookId}
        );
        db.execSQL("DELETE FROM chapters WHERE book_id=?", new Object[]{localBookId});

        db.execSQL(
                "INSERT OR REPLACE INTO positions (book_id, chapter_locator, page_index, "
                        + "total_pages, locator_json, updated_at, server_version) "
                        + "SELECT ?, chapter_locator, page_index, total_pages, locator_json, "
                        + "updated_at, server_version FROM positions WHERE book_id=? AND ("
                        + "NOT EXISTS(SELECT 1 FROM positions WHERE book_id=?) OR "
                        + "COALESCE(updated_at, '') >= COALESCE(("
                        + "SELECT updated_at FROM positions WHERE book_id=?), ''))",
                new Object[]{
                        canonicalBookId,
                        localBookId,
                        canonicalBookId,
                        canonicalBookId
                }
        );
        db.execSQL("DELETE FROM positions WHERE book_id=?", new Object[]{localBookId});
        db.execSQL(
                "UPDATE annotations SET book_id=? WHERE book_id=?",
                new Object[]{canonicalBookId, localBookId}
        );
        db.execSQL(
                "INSERT OR IGNORE INTO lookup_cache (book_id, word, payload_json, updated_at) "
                        + "SELECT ?, word, payload_json, updated_at FROM lookup_cache WHERE book_id=?",
                new Object[]{canonicalBookId, localBookId}
        );
        db.execSQL("DELETE FROM lookup_cache WHERE book_id=?", new Object[]{localBookId});

        try (Cursor cursor = db.rawQuery(
                "SELECT operation_id, payload_json, receipt_json "
                        + "FROM operation_queue WHERE book_id=?",
                new String[]{localBookId}
        )) {
            while (cursor.moveToNext()) {
                JSONObject payload = objectOrEmpty(cursor.getString(1));
                JSONObject receipt = objectOrEmpty(cursor.getString(2));
                replaceBookIdInJson(payload, localBookId, canonicalBookId);
                replaceBookIdInJson(receipt, localBookId, canonicalBookId);
                db.execSQL(
                        "UPDATE operation_queue SET book_id=?, payload_json=?, receipt_json=? "
                                + "WHERE operation_id=?",
                        new Object[]{
                                canonicalBookId,
                                payload.toString(),
                                receipt.toString(),
                                cursor.getString(0)
                        }
                );
            }
        }
        db.execSQL(
                "INSERT OR IGNORE INTO asset_refresh_queue ("
                        + "book_id, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at, "
                        + "created_at, updated_at"
                        + ") SELECT ?, asset_type, asset_url, expected_hash, expected_byte_size, "
                        + "contract_marker, state, retry_count, last_error, next_attempt_at, "
                        + "created_at, updated_at FROM asset_refresh_queue WHERE book_id=?",
                new Object[]{canonicalBookId, localBookId}
        );
        db.execSQL(
                "DELETE FROM asset_refresh_queue WHERE book_id=?",
                new Object[]{localBookId}
        );
        db.execSQL(
                "INSERT OR IGNORE INTO metadata_repair_queue ("
                        + "book_id, reason, contract_marker, state, retry_count, last_error, "
                        + "next_attempt_at, created_at, updated_at"
                        + ") SELECT ?, reason, contract_marker, state, retry_count, last_error, "
                        + "next_attempt_at, created_at, updated_at "
                        + "FROM metadata_repair_queue WHERE book_id=?",
                new Object[]{canonicalBookId, localBookId}
        );
        db.execSQL(
                "DELETE FROM metadata_repair_queue WHERE book_id=?",
                new Object[]{localBookId}
        );
    }

    private BookImportRow bookImportLocked(SQLiteDatabase db, String localBookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT local_book_id, source_kind, source_local_path, source_hash, "
                        + "source_byte_size, filename, state, last_error "
                        + "FROM book_import_queue WHERE local_book_id=?",
                new String[]{localBookId}
        )) {
            return cursor.moveToFirst() ? bookImportRow(cursor) : null;
        }
    }

    private BookImportRow bookImportRow(Cursor cursor) {
        return new BookImportRow(
                cursor.getString(0),
                cursor.getString(1),
                cursor.getString(2),
                cursor.getString(3),
                cursor.getLong(4),
                cursor.getString(5),
                cursor.getString(6),
                cursor.getString(7)
        );
    }

    private String bookSourceHashLocked(SQLiteDatabase db, String bookId) {
        try (Cursor cursor = db.rawQuery(
                "SELECT source_hash FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? clean(cursor.getString(0)).toLowerCase(Locale.ROOT) : "";
        }
    }

    private void replaceBookIdInJson(
            Object value,
            String localBookId,
            String canonicalBookId
    ) throws Exception {
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            java.util.Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                Object child = object.opt(key);
                if ("book_id".equals(key)
                        && child instanceof String
                        && localBookId.equals(child)) {
                    object.put(key, canonicalBookId);
                } else if (child instanceof JSONObject || child instanceof JSONArray) {
                    replaceBookIdInJson(child, localBookId, canonicalBookId);
                }
            }
        } else if (value instanceof JSONArray) {
            JSONArray array = (JSONArray) value;
            for (int index = 0; index < array.length(); index++) {
                Object child = array.opt(index);
                if (child instanceof JSONObject || child instanceof JSONArray) {
                    replaceBookIdInJson(child, localBookId, canonicalBookId);
                }
            }
        }
    }

    private File appPrivateOriginal(String rawPath) throws Exception {
        String path = clean(rawPath);
        if (path.isEmpty()) {
            throw new IllegalArgumentException("book import source path is required");
        }
        File root = appFilesDirectory.getCanonicalFile();
        File source = new File(path).getCanonicalFile();
        if (!source.getPath().startsWith(root.getPath() + File.separator)
                || !source.isFile()
                || source.length() <= 0L) {
            throw new IllegalArgumentException("book import source must be an app-private file");
        }
        return source;
    }

    List<BookRow> books() {
        List<BookRow> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT b.id, b.title, b.author, b.updated_at, "
                        + "EXISTS(SELECT 1 FROM positions p WHERE p.book_id=b.id), "
                        + "b.reading_profile, b.analysis_state, b.analysis_updated_at, "
                        + "b.compatibility_json, b.cover_local_path, b.favorite, "
                        + "b.custom_category, b.tags_json, b.last_opened_at, b.epub_local_path, "
                        + "b.source_kind, b.source_local_path, b.source_hash, "
                        + "b.source_byte_size, b.import_state, "
                        + "COALESCE((SELECT q.asset_type FROM asset_refresh_queue q "
                        + "WHERE q.book_id=b.id AND q.state='terminal' "
                        + "ORDER BY q.updated_at DESC, q.asset_type ASC LIMIT 1), ''), "
                        + "COALESCE((SELECT q.last_error FROM asset_refresh_queue q "
                        + "WHERE q.book_id=b.id AND q.state='terminal' "
                        + "ORDER BY q.updated_at DESC, q.asset_type ASC LIMIT 1), '') "
                        + "FROM books b ORDER BY COALESCE(NULLIF(b.last_opened_at, ''), "
                        + "b.updated_at) DESC, b.title ASC",
                null
        )) {
            while (cursor.moveToNext()) {
                rows.add(new BookRow(
                        cursor.getString(0), cursor.getString(1), cursor.getString(2), cursor.getString(3),
                        cursor.getInt(4) != 0, cursor.getString(5), cursor.getString(6), cursor.getString(7),
                        cursor.getString(8), cursor.getString(9), cursor.getInt(10) != 0, cursor.getString(11),
                        cursor.getString(12), cursor.getString(13), cursor.getString(14), cursor.getString(15),
                        cursor.getString(16), cursor.getString(17), cursor.getLong(18), cursor.getString(19),
                        cursor.getString(20), cursor.getString(21)
                ));
            }
        }
        return rows;
    }

    List<AnnotationRow> annotations(String bookId, String kind) {
        List<AnnotationRow> rows = new ArrayList<>();
        String selection = kind == null || kind.isEmpty() ? "book_id=?" : "book_id=? AND kind=?";
        String[] args = kind == null || kind.isEmpty() ? new String[]{bookId} : new String[]{bookId, kind};
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT id, kind, source_text, note_text, chapter_locator, range_locator_json, updated_at, metadata_json, server_version FROM annotations WHERE " + selection + " ORDER BY updated_at DESC",
                args
        )) {
            while (cursor.moveToNext()) {
                rows.add(new AnnotationRow(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getString(3),
                        cursor.getString(4),
                        cursor.getString(5),
                        cursor.getString(6),
                        cursor.getString(7),
                        cursor.getLong(8)
                ));
            }
        }
        return rows;
    }

    List<GlobalAnnotationRow> globalAnnotations(String kind, int requestedLimit) {
        List<GlobalAnnotationRow> rows = new ArrayList<>();
        String normalizedKind = clean(kind);
        if (normalizedKind.isEmpty()) {
            return rows;
        }
        int limit = Math.max(1, Math.min(100, requestedLimit));
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT a.id, a.book_id, b.title, b.author, a.kind, a.source_text, a.note_text, "
                        + "CASE WHEN TRIM(COALESCE(a.range_locator_json, '')) NOT IN ('', '{}') "
                        + "THEN a.range_locator_json ELSE a.chapter_locator END, COALESCE(("
                        + "SELECT c.title FROM chapters c "
                        + "WHERE c.book_id=a.book_id AND c.locator=a.chapter_locator LIMIT 1"
                        + "), ''), a.updated_at "
                        + "FROM annotations a JOIN books b ON b.id=a.book_id "
                        + "WHERE a.kind=? "
                        + "ORDER BY a.updated_at DESC, a.id DESC LIMIT " + limit,
                new String[]{normalizedKind}
        )) {
            while (cursor.moveToNext()) {
                rows.add(new GlobalAnnotationRow(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getString(3),
                        cursor.getString(4),
                        cursor.getString(5),
                        cursor.getString(6),
                        cursor.getString(7),
                        cursor.getString(8),
                        cursor.getString(9)
                ));
            }
        }
        return rows;
    }

    List<OfflineSearchRow> searchOffline(String query, int requestedLimit) {
        List<OfflineSearchRow> rows = new ArrayList<>();
        String normalizedQuery = clean(query);
        if (normalizedQuery.isEmpty()) {
            return rows;
        }
        int limit = Math.max(1, Math.min(100, requestedLimit));
        String like = "%" + escapeLikeQuery(normalizedQuery) + "%";
        String sql = "SELECT result_type, book_id, book_title, author, target_locator, chapter_title, raw_text "
                + "FROM ("
                + "SELECT 'book' AS result_type, b.id AS book_id, b.title AS book_title, b.author AS author, "
                + "'' AS target_locator, '' AS chapter_title, "
                + "b.title || CASE WHEN COALESCE(b.author, '')='' THEN '' ELSE ' · ' || b.author END AS raw_text, "
                + "0 AS result_rank, COALESCE(NULLIF(b.last_opened_at, ''), b.updated_at) AS sort_time "
                + "FROM books b WHERE b.title LIKE ? ESCAPE '\\' OR b.author LIKE ? ESCAPE '\\' "
                + "UNION ALL "
                + "SELECT 'annotation', a.book_id, b.title, b.author, "
                + "CASE WHEN TRIM(COALESCE(a.range_locator_json, '')) NOT IN ('', '{}') "
                + "THEN a.range_locator_json ELSE a.chapter_locator END, "
                + "COALESCE((SELECT c.title FROM chapters c "
                        + "WHERE c.book_id=a.book_id AND c.locator=a.chapter_locator LIMIT 1), ''), "
                + "CASE WHEN LOWER(TRIM(COALESCE(a.note_text, ''))) IN ('', 'null') "
                + "THEN COALESCE(a.source_text, '') "
                + "ELSE a.note_text || CASE WHEN COALESCE(a.source_text, '')='' THEN '' ELSE ' — ' || a.source_text END END, "
                + "1, a.updated_at "
                + "FROM annotations a JOIN books b ON b.id=a.book_id "
                + "WHERE a.source_text LIKE ? ESCAPE '\\' OR a.note_text LIKE ? ESCAPE '\\' "
                + "UNION ALL "
                + "SELECT 'chapter', c.book_id, b.title, b.author, c.locator, c.title, "
                + "c.title || ' ' || substr(COALESCE(c.html, ''), "
                + "CASE WHEN instr(lower(COALESCE(c.html, '')), lower(?))>240 "
                + "THEN instr(lower(COALESCE(c.html, '')), lower(?))-240 ELSE 1 END, 1200), "
                + "2, b.updated_at "
                + "FROM chapters c JOIN books b ON b.id=c.book_id "
                + "WHERE c.title LIKE ? ESCAPE '\\' OR c.html LIKE ? ESCAPE '\\'"
                + ") ORDER BY result_rank ASC, sort_time DESC LIMIT " + limit;
        String[] args = new String[]{
                like,
                like,
                like,
                like,
                normalizedQuery,
                normalizedQuery,
                like,
                like
        };
        try (Cursor cursor = getReadableDatabase().rawQuery(sql, args)) {
            while (cursor.moveToNext()) {
                rows.add(new OfflineSearchRow(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getString(3),
                        cursor.getString(4),
                        cursor.getString(5),
                        searchSnippet(cursor.getString(6), normalizedQuery)
                ));
            }
        }
        return rows;
    }

    static String escapeLikeQuery(String query) {
        return clean(query)
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_");
    }

    static String searchSnippet(String rawText, String query) {
        String text = clean(rawText)
                .replaceAll("(?is)<script[^>]*>.*?</script>", " ")
                .replaceAll("(?is)<style[^>]*>.*?</style>", " ")
                .replaceAll("(?is)<[^>]+>", " ")
                .replace("&nbsp;", " ")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", "\"")
                .replace("&#39;", "'")
                .replace("&amp;", "&")
                .replaceAll("\\s+", " ")
                .trim();
        final int maxLength = 140;
        if (text.length() <= maxLength) {
            return text;
        }
        String needle = clean(query).toLowerCase(Locale.ROOT);
        int matchIndex = needle.isEmpty() ? -1 : text.toLowerCase(Locale.ROOT).indexOf(needle);
        int start = matchIndex < 0 ? 0 : Math.max(0, matchIndex - 42);
        int end = Math.min(text.length(), start + maxLength);
        if (end == text.length()) {
            start = Math.max(0, end - maxLength);
        }
        return (start > 0 ? "…" : "")
                + text.substring(start, end).trim()
                + (end < text.length() ? "…" : "");
    }

    List<ChapterRow> chapters(String bookId) {
        List<ChapterRow> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT chapter_index, locator, title FROM chapters WHERE book_id=? ORDER BY chapter_index ASC",
                new String[]{bookId}
        )) {
            while (cursor.moveToNext()) {
                rows.add(new ChapterRow(cursor.getInt(0), cursor.getString(1), cursor.getString(2)));
            }
        }
        return rows;
    }

    String chapterHtml(String bookId, int chapterIndex) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT html FROM chapters WHERE book_id=? AND chapter_index=?",
                new String[]{bookId, String.valueOf(chapterIndex)}
        )) {
            return cursor.moveToFirst() ? cursor.getString(0) : "";
        }
    }

    String bookEpubLocalPath(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT epub_local_path FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? cursor.getString(0) : "";
        }
    }

    String bookCoverLocalPath(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT cover_local_path FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? cursor.getString(0) : "";
        }
    }

    String bookDisplayVariantsLocalPath(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT display_variants_local_path FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? clean(cursor.getString(0)) : "";
        }
    }

    String bookSourceKind(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT source_kind FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? clean(cursor.getString(0)).toLowerCase(Locale.ROOT) : "";
        }
    }

    String bookSourceLocalPath(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT source_local_path FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? clean(cursor.getString(0)) : "";
        }
    }

    String bookReadingProfile(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT reading_profile FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? normalizedReadingProfile(cursor.getString(0)) : "UNKNOWN";
        }
    }

    static String normalizedReadingProfile(String value) {
        String profile = clean(value);
        return profile.isEmpty() ? "UNKNOWN" : profile;
    }

    String bookAnalysisState(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT analysis_state FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            return cursor.moveToFirst() ? cursor.getString(0) : "not_requested";
        }
    }

    String positionLocatorJson(String bookId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT locator_json FROM positions WHERE book_id=?",
                new String[]{bookId}
        )) {
            if (cursor.moveToFirst()) {
                String value = cursor.getString(0);
                return value == null ? "" : value;
            }
        }
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT position_json FROM books WHERE id=?",
                new String[]{bookId}
        )) {
            if (!cursor.moveToFirst()) {
                return "";
            }
            String value = cursor.getString(0);
            if (value == null || value.trim().isEmpty()) {
                return "";
            }
            try {
                JSONObject position = new JSONObject(value);
                JSONObject locator = position.optJSONObject("locator");
                return locator == null ? "" : locator.toString();
            } catch (Throwable ignored) {
                return "";
            }
        }
    }

    String syncCursor() {
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT value FROM sync_state WHERE key='cursor'", null)) {
            return cursor.moveToFirst() ? cursor.getString(0) : "";
        }
    }

    long syncSequence() {
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT value FROM sync_state WHERE key='sequence_cursor'", null)) {
            if (!cursor.moveToFirst()) {
                return 0L;
            }
            try {
                return Math.max(0L, Long.parseLong(cursor.getString(0)));
            } catch (Throwable ignored) {
                return 0L;
            }
        }
    }

    boolean hasFullBaseline() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT value FROM sync_state WHERE key='full_sync_completed'",
                null
        )) {
            return cursor.moveToFirst() && "1".equals(cursor.getString(0));
        }
    }

    boolean hasAssetRefreshWork() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT EXISTS(SELECT 1 FROM asset_refresh_queue WHERE state='pending' LIMIT 1)",
                null
        )) {
            return cursor.moveToFirst() && cursor.getInt(0) == 1;
        }
    }

    int pendingCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT count(*) FROM operation_queue WHERE sync_status IN ('pending','retryable')", null)) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    boolean hasPendingOperations() {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT EXISTS(SELECT 1 FROM operation_queue q "
                        + "WHERE q.sync_status IN ('pending','retryable') "
                        + "AND NOT EXISTS(SELECT 1 FROM book_import_queue i "
                        + "WHERE i.local_book_id=q.book_id))",
                null
        )) {
            return cursor.moveToFirst() && cursor.getInt(0) == 1;
        }
    }

    int syncIssueCount() {
        return syncIssueCount("");
    }

    int syncIssueCount(String bookId) {
        String cleanBookId = clean(bookId);
        String bookFilter = cleanBookId.isEmpty() ? "" : " AND book_id=?";
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT count(*) FROM operation_queue "
                        + "WHERE sync_status IN ('conflict','permanent_failed')"
                        + bookFilter,
                cleanBookId.isEmpty() ? null : new String[]{cleanBookId}
        )) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    List<SyncIssueRow> syncIssues(int requestedLimit) {
        return syncIssues("", true, requestedLimit);
    }

    List<SyncIssueRow> syncIssues(String bookId, boolean includeHistory, int requestedLimit) {
        int limit = Math.max(1, Math.min(100, requestedLimit));
        String cleanBookId = clean(bookId);
        String statuses = includeHistory
                ? "('conflict','permanent_failed','resolved_server','superseded','local_only')"
                : "('conflict','permanent_failed')";
        String bookFilter = cleanBookId.isEmpty() ? "" : " AND q.book_id=?";
        List<SyncIssueRow> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT q.operation_id, q.operation_type, q.book_id, q.sync_status, q.last_error, "
                        + "q.payload_json, q.base_server_version, COALESCE(b.title, ''), "
                        + "q.receipt_json, q.updated_at FROM operation_queue q "
                        + "LEFT JOIN books b ON b.id=q.book_id "
                        + "WHERE q.sync_status IN " + statuses
                        + bookFilter
                        + " ORDER BY CASE WHEN q.sync_status IN ('conflict','permanent_failed') "
                        + "THEN 0 ELSE 1 END, q.updated_at DESC LIMIT " + limit,
                cleanBookId.isEmpty() ? null : new String[]{cleanBookId}
        )) {
            while (cursor.moveToNext()) {
                rows.add(new SyncIssueRow(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getString(3),
                        cursor.getString(4),
                        cursor.getString(5),
                        cursor.getLong(6),
                        cursor.getString(7),
                        cursor.getString(8),
                        cursor.getString(9)
                ));
            }
        }
        return rows;
    }

    void resolveUseServer(String operationId) throws Exception {
        String cleanOperationId = clean(operationId);
        if (cleanOperationId.isEmpty()) {
            throw new IllegalArgumentException("operation_id is required");
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            OperationResolutionRow row = operationResolutionLocked(db, cleanOperationId);
            if (row == null
                    || (!"conflict".equals(row.status)
                    && !"permanent_failed".equals(row.status))) {
                throw new IllegalStateException("同步问题已经变化，请刷新后重试");
            }
            JSONObject payload = objectOrEmpty(row.payloadJson);
            if ("conflict".equals(row.status)) {
                if (!isAnnotationMutation(row.operationType)) {
                    throw new IllegalStateException("当前冲突类型不能安全自动采用 Mac 版本");
                }
                JSONObject receipt = objectOrEmpty(row.receiptJson);
                JSONObject serverRecord = receipt.optJSONObject("server_record");
                boolean serverDeleted = receipt.optBoolean("server_deleted") || serverRecord == null;
                String annotationId = clean(payload.optString("annotation_id"));
                markOperationResolvedLocked(db, row.operationId, "resolved_server");
                if (serverDeleted) {
                    if (!annotationId.isEmpty()) {
                        db.execSQL(
                                "DELETE FROM annotations WHERE id=?",
                                new Object[]{annotationId}
                        );
                    }
                } else {
                    JSONObject restored = new JSONObject(serverRecord.toString());
                    restored.put(
                            "server_version",
                            Math.max(1L, receipt.optLong("server_version", 1L))
                    );
                    saveAnnotationLocked(db, restored);
                }
            } else if (isNewLocalAnnotationOperation(row.operationType)) {
                markOperationResolvedLocked(db, row.operationId, "resolved_server");
                db.execSQL(
                        "DELETE FROM annotations WHERE id=?",
                        new Object[]{"local-" + row.operationId}
                );
            } else {
                if (row.bookId.isEmpty()) {
                    throw new IllegalStateException("这项操作缺少书籍信息，不能安全恢复 Mac 版本");
                }
                markOperationResolvedLocked(db, row.operationId, "resolved_server");
                registerMetadataRepairLocked(
                        db,
                        row.bookId,
                        "用户采用 Mac 版本，恢复远端状态",
                        "operation-resolution:" + row.operationId
                );
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void resolveKeepDevice(String operationId, String newOperationId) throws Exception {
        String cleanOperationId = clean(operationId);
        String cleanNewOperationId = clean(newOperationId);
        if (cleanOperationId.isEmpty()) {
            throw new IllegalArgumentException("operation_id is required");
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            OperationResolutionRow row = operationResolutionLocked(db, cleanOperationId);
            if (row == null || !"conflict".equals(row.status)) {
                throw new IllegalStateException("这项冲突已经变化，请刷新后重试");
            }
            if (!isAnnotationMutation(row.operationType)) {
                throw new IllegalStateException("当前冲突类型不能安全重新提交");
            }
            JSONObject payload = objectOrEmpty(row.payloadJson);
            JSONObject receipt = objectOrEmpty(row.receiptJson);
            JSONObject serverRecord = receipt.optJSONObject("server_record");
            boolean serverDeleted = receipt.optBoolean("server_deleted") || serverRecord == null;
            String annotationId = clean(payload.optString("annotation_id"));

            if (serverDeleted && "annotation_deleted".equals(row.operationType)) {
                markOperationResolvedLocked(db, row.operationId, "resolved_server");
                if (!annotationId.isEmpty()) {
                    db.execSQL(
                            "DELETE FROM annotations WHERE id=?",
                            new Object[]{annotationId}
                    );
                }
                db.setTransactionSuccessful();
                return;
            }
            if (cleanNewOperationId.isEmpty()
                    || cleanNewOperationId.equals(cleanOperationId)) {
                throw new IllegalArgumentException("重新提交必须使用新的 operation_id");
            }

            if (serverDeleted) {
                if (!"annotation_updated".equals(row.operationType)
                        || annotationId.isEmpty()) {
                    throw new IllegalStateException("Mac 已删除这条内容，当前手机操作不能安全恢复");
                }
                JSONObject localSnapshot = annotationSnapshotLocked(db, annotationId);
                if (localSnapshot == null) {
                    throw new IllegalStateException("手机本地内容不存在，无法恢复为新批注");
                }
                JSONObject localRow = new JSONObject(localSnapshot.toString());
                JSONObject localMetadata = localRow.optJSONObject("metadata") == null
                        ? new JSONObject()
                        : new JSONObject(localRow.optJSONObject("metadata").toString());
                localMetadata.put("local_operation_id", cleanNewOperationId);
                localMetadata.put("sync_status", "pending");
                localRow.put("id", "local-" + cleanNewOperationId);
                localRow.put("metadata", localMetadata);
                localRow.put("updated_at", Instant.now().toString());
                localRow.put("server_version", 0L);
                JSONObject createPayload = annotationCreatePayload(localRow);

                markOperationResolvedLocked(db, row.operationId, "superseded");
                db.execSQL(
                        "DELETE FROM annotations WHERE id=?",
                        new Object[]{annotationId}
                );
                saveAnnotationLocked(db, localRow);
                insertPendingOperationLocked(
                        db,
                        cleanNewOperationId,
                        "annotation_created",
                        row.bookId,
                        createPayload,
                        0L,
                        false
                );
            } else {
                long serverVersion = receipt.optLong("server_version", 0L);
                if (serverVersion <= 0L) {
                    throw new IllegalStateException("冲突回执缺少 Mac 版本号");
                }
                markOperationResolvedLocked(db, row.operationId, "superseded");
                insertPendingOperationLocked(
                        db,
                        cleanNewOperationId,
                        row.operationType,
                        row.bookId,
                        payload,
                        serverVersion,
                        false
                );
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    void keepPermanentLocalOnly(String operationId) {
        String cleanOperationId = clean(operationId);
        if (cleanOperationId.isEmpty()) {
            throw new IllegalArgumentException("operation_id is required");
        }
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            OperationResolutionRow row = operationResolutionLocked(db, cleanOperationId);
            if (row == null || !"permanent_failed".equals(row.status)) {
                throw new IllegalStateException("这项失败记录已经变化，请刷新后重试");
            }
            if (!supportsLocalOnly(row.operationType)) {
                throw new IllegalStateException("这类操作不能安全设为仅保留在手机");
            }
            markOperationResolvedLocked(db, row.operationId, "local_only");
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private OperationResolutionRow operationResolutionLocked(
            SQLiteDatabase db,
            String operationId
    ) {
        try (Cursor cursor = db.rawQuery(
                "SELECT operation_id, operation_type, book_id, payload_json, sync_status, "
                        + "receipt_json FROM operation_queue WHERE operation_id=?",
                new String[]{operationId}
        )) {
            if (!cursor.moveToFirst()) {
                return null;
            }
            return new OperationResolutionRow(
                    cursor.getString(0),
                    cursor.getString(1),
                    cursor.getString(2),
                    cursor.getString(3),
                    cursor.getString(4),
                    cursor.getString(5)
            );
        }
    }

    private void markOperationResolvedLocked(
            SQLiteDatabase db,
            String operationId,
            String status
    ) {
        db.execSQL(
                "UPDATE operation_queue SET sync_status=?, updated_at=datetime('now') "
                        + "WHERE operation_id=?",
                new Object[]{status, operationId}
        );
    }

    private JSONObject annotationSnapshotLocked(SQLiteDatabase db, String annotationId)
            throws Exception {
        try (Cursor cursor = db.rawQuery(
                "SELECT id, book_id, kind, source_text, note_text, color, chapter_locator, "
                        + "range_locator_json, metadata_json, updated_at, server_version "
                        + "FROM annotations WHERE id=?",
                new String[]{annotationId}
        )) {
            if (!cursor.moveToFirst()) {
                return null;
            }
            return new JSONObject()
                    .put("id", cursor.getString(0))
                    .put("book_id", cursor.getString(1))
                    .put("kind", cursor.getString(2))
                    .put("source_text", cursor.getString(3))
                    .put("note_text", cursor.getString(4))
                    .put("color", cursor.getString(5))
                    .put("chapter_locator", cursor.getString(6))
                    .put("range_locator", objectOrEmpty(cursor.getString(7)))
                    .put("metadata", objectOrEmpty(cursor.getString(8)))
                    .put("updated_at", cursor.getString(9))
                    .put("server_version", cursor.getLong(10));
        }
    }

    private JSONObject annotationCreatePayload(JSONObject snapshot) throws Exception {
        return new JSONObject()
                .put("book_id", snapshot.optString("book_id"))
                .put("kind", snapshot.optString("kind"))
                .put("source_text", snapshot.optString("source_text"))
                .put("note_text", snapshot.optString("note_text"))
                .put("color", snapshot.optString("color"))
                .put("chapter_locator", snapshot.optString("chapter_locator"))
                .put("range_locator", snapshot.optJSONObject("range_locator"))
                .put("metadata", snapshot.optJSONObject("metadata"));
    }

    private static JSONObject objectOrEmpty(String raw) throws Exception {
        String value = clean(raw);
        return value.isEmpty() ? new JSONObject() : new JSONObject(value);
    }

    private static boolean isAnnotationMutation(String operationType) {
        return "annotation_updated".equals(operationType)
                || "annotation_deleted".equals(operationType);
    }

    private static boolean isNewLocalAnnotationOperation(String operationType) {
        return "annotation_created".equals(operationType)
                || "red_highlight_created".equals(operationType)
                || "note_created".equals(operationType)
                || "audio_note_created".equals(operationType);
    }

    private static boolean supportsLocalOnly(String operationType) {
        return isNewLocalAnnotationOperation(operationType)
                || isAnnotationMutation(operationType)
                || "reading_position_updated".equals(operationType);
    }

    private void setSyncValueLocked(SQLiteDatabase db, String key, String value) {
        db.execSQL("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", new Object[]{key, value == null ? "" : value});
    }

    private static final class BookAssetState {
        final String updatedAt;
        final String epubUrl;
        final String epubLocalPath;
        final String coverUrl;
        final String coverLocalPath;
        final String displayVariantsUrl;
        final String displayVariantsLocalPath;
        final String epubHash;
        final long epubByteSize;
        final String sourceKind;
        final String sourceLocalPath;
        final String sourceHash;
        final long sourceByteSize;
        final String importState;
        final String canonicalId;

        BookAssetState(
                String updatedAt,
                String epubUrl,
                String epubLocalPath,
                String coverUrl,
                String coverLocalPath,
                String displayVariantsUrl,
                String displayVariantsLocalPath,
                String epubHash,
                long epubByteSize,
                String sourceKind,
                String sourceLocalPath,
                String sourceHash,
                long sourceByteSize,
                String importState,
                String canonicalId
        ) {
            this.updatedAt = clean(updatedAt);
            this.epubUrl = clean(epubUrl);
            this.epubLocalPath = clean(epubLocalPath);
            this.coverUrl = clean(coverUrl);
            this.coverLocalPath = clean(coverLocalPath);
            this.displayVariantsUrl = clean(displayVariantsUrl);
            this.displayVariantsLocalPath = clean(displayVariantsLocalPath);
            this.epubHash = clean(epubHash);
            this.epubByteSize = epubByteSize;
            this.sourceKind = clean(sourceKind).toLowerCase(Locale.ROOT);
            this.sourceLocalPath = clean(sourceLocalPath);
            this.sourceHash = clean(sourceHash).toLowerCase(Locale.ROOT);
            this.sourceByteSize = sourceByteSize;
            this.importState = clean(importState);
            this.canonicalId = clean(canonicalId);
        }
    }

    static final class BookImportRow {
        final String localBookId;
        final String sourceKind;
        final String sourceLocalPath;
        final String sourceHash;
        final long sourceByteSize;
        final String filename;
        final String state;
        final String lastError;

        BookImportRow(
                String localBookId,
                String sourceKind,
                String sourceLocalPath,
                String sourceHash,
                long sourceByteSize,
                String filename,
                String state,
                String lastError
        ) {
            this.localBookId = clean(localBookId);
            this.sourceKind = clean(sourceKind).toLowerCase(Locale.ROOT);
            this.sourceLocalPath = clean(sourceLocalPath);
            this.sourceHash = clean(sourceHash).toLowerCase(Locale.ROOT);
            this.sourceByteSize = sourceByteSize;
            this.filename = clean(filename);
            this.state = clean(state);
            this.lastError = clean(lastError);
        }
    }

    static final class AssetRefreshRow {
        final String bookId;
        final String assetType;
        final String assetUrl;
        final String expectedHash;
        final long expectedByteSize;
        final String contractMarker;
        final String state;
        final int retryCount;
        final String lastError;
        final long nextAttemptAt;

        AssetRefreshRow(
                String bookId,
                String assetType,
                String assetUrl,
                String expectedHash,
                long expectedByteSize,
                String contractMarker,
                String state,
                int retryCount,
                String lastError,
                long nextAttemptAt
        ) {
            this.bookId = clean(bookId);
            this.assetType = clean(assetType);
            this.assetUrl = clean(assetUrl);
            this.expectedHash = clean(expectedHash);
            this.expectedByteSize = expectedByteSize;
            this.contractMarker = clean(contractMarker);
            this.state = clean(state);
            this.retryCount = Math.max(0, retryCount);
            this.lastError = clean(lastError);
            this.nextAttemptAt = Math.max(0L, nextAttemptAt);
        }
    }

    static final class MetadataRepairRow {
        final String bookId;
        final String reason;
        final String contractMarker;
        final String state;
        final int retryCount;
        final String lastError;
        final long nextAttemptAt;

        MetadataRepairRow(
                String bookId,
                String reason,
                String contractMarker,
                String state,
                int retryCount,
                String lastError,
                long nextAttemptAt
        ) {
            this.bookId = clean(bookId);
            this.reason = clean(reason);
            this.contractMarker = clean(contractMarker);
            this.state = clean(state);
            this.retryCount = Math.max(0, retryCount);
            this.lastError = clean(lastError);
            this.nextAttemptAt = Math.max(0L, nextAttemptAt);
        }
    }

    static final class BookRetryResult {
        static final BookRetryResult EMPTY = new BookRetryResult(0, 0);

        final int assetRowsReset;
        final int metadataRowsReset;

        BookRetryResult(int assetRowsReset, int metadataRowsReset) {
            this.assetRowsReset = Math.max(0, assetRowsReset);
            this.metadataRowsReset = Math.max(0, metadataRowsReset);
        }

        boolean hasWork() {
            return assetRowsReset > 0 || metadataRowsReset > 0;
        }
    }

    static final class SyncIssueRow {
        final String operationId;
        final String operationType;
        final String bookId;
        final String status;
        final String lastError;
        final String payloadJson;
        final long baseServerVersion;
        final String bookTitle;
        final String receiptJson;
        final String updatedAt;

        SyncIssueRow(
                String operationId,
                String operationType,
                String bookId,
                String status,
                String lastError,
                String payloadJson,
                long baseServerVersion,
                String bookTitle,
                String receiptJson,
                String updatedAt
        ) {
            this.operationId = clean(operationId);
            this.operationType = clean(operationType);
            this.bookId = clean(bookId);
            this.status = clean(status);
            this.lastError = clean(lastError);
            this.payloadJson = clean(payloadJson);
            this.baseServerVersion = Math.max(0L, baseServerVersion);
            this.bookTitle = clean(bookTitle);
            this.receiptJson = clean(receiptJson);
            this.updatedAt = clean(updatedAt);
        }

        boolean isActive() {
            return "conflict".equals(status) || "permanent_failed".equals(status);
        }

        boolean isConflict() {
            return "conflict".equals(status);
        }

        boolean supportsLocalOnly() {
            return ClickNativeStore.supportsLocalOnly(operationType);
        }
    }

    private static final class OperationResolutionRow {
        final String operationId;
        final String operationType;
        final String bookId;
        final String payloadJson;
        final String status;
        final String receiptJson;

        OperationResolutionRow(
                String operationId,
                String operationType,
                String bookId,
                String payloadJson,
                String status,
                String receiptJson
        ) {
            this.operationId = clean(operationId);
            this.operationType = clean(operationType);
            this.bookId = clean(bookId);
            this.payloadJson = clean(payloadJson);
            this.status = clean(status);
            this.receiptJson = clean(receiptJson);
        }
    }

    static final class GlobalAnnotationRow {
        final String id;
        final String bookId;
        final String bookTitle;
        final String author;
        final String kind;
        final String sourceText;
        final String noteText;
        final String targetLocator;
        final String chapterTitle;
        final String updatedAt;

        GlobalAnnotationRow(
                String id,
                String bookId,
                String bookTitle,
                String author,
                String kind,
                String sourceText,
                String noteText,
                String targetLocator,
                String chapterTitle,
                String updatedAt
        ) {
            this.id = clean(id);
            this.bookId = clean(bookId);
            this.bookTitle = clean(bookTitle);
            this.author = clean(author);
            this.kind = clean(kind);
            this.sourceText = clean(sourceText);
            this.noteText = cleanOptionalText(noteText);
            this.targetLocator = clean(targetLocator);
            this.chapterTitle = clean(chapterTitle);
            this.updatedAt = clean(updatedAt);
        }
    }

    static final class OfflineSearchRow {
        final String resultType;
        final String bookId;
        final String bookTitle;
        final String author;
        final String targetLocator;
        final String chapterTitle;
        final String snippet;

        OfflineSearchRow(
                String resultType,
                String bookId,
                String bookTitle,
                String author,
                String targetLocator,
                String chapterTitle,
                String snippet
        ) {
            this.resultType = clean(resultType);
            this.bookId = clean(bookId);
            this.bookTitle = clean(bookTitle);
            this.author = clean(author);
            this.targetLocator = clean(targetLocator);
            this.chapterTitle = clean(chapterTitle);
            this.snippet = clean(snippet);
        }
    }

    static final class BookRow {
        final String id;
        final String title;
        final String author;
        final String updatedAt;
        final boolean hasPosition;
        final String readingProfile;
        final String analysisState;
        final String analysisUpdatedAt;
        final boolean correctionQueueRequired;
        final String coverLocalPath;
        final boolean favorite;
        final String customCategory;
        final String tagsJson;
        final String lastOpenedAt;
        final String epubLocalPath;
        final String sourceKind;
        final String sourceLocalPath;
        final String sourceHash;
        final long sourceByteSize;
        final String importState;
        final String assetIssueSummary;

        BookRow(String id, String title, String author, String updatedAt, boolean hasPosition, String readingProfile, String analysisState, String analysisUpdatedAt, String compatibilityJson, String coverLocalPath, boolean favorite, String customCategory, String tagsJson, String lastOpenedAt, String epubLocalPath, String sourceKind, String sourceLocalPath, String sourceHash, long sourceByteSize, String importState, String assetIssueType, String assetIssueError) {
            this.id = id == null ? "" : id;
            this.title = title == null || title.isEmpty() ? this.id : title;
            this.author = author == null ? "" : author;
            this.updatedAt = updatedAt == null ? "" : updatedAt;
            this.hasPosition = hasPosition;
            this.readingProfile = readingProfile == null || readingProfile.isEmpty() ? "UNKNOWN" : readingProfile;
            this.analysisState = analysisState == null || analysisState.isEmpty() ? "not_requested" : analysisState;
            this.analysisUpdatedAt = analysisUpdatedAt == null ? "" : analysisUpdatedAt;
            this.coverLocalPath = coverLocalPath == null ? "" : coverLocalPath;
            this.favorite = favorite;
            this.customCategory = customCategory == null ? "" : customCategory;
            this.tagsJson = tagsJson == null || tagsJson.isEmpty() ? "[]" : tagsJson;
            this.lastOpenedAt = lastOpenedAt == null ? "" : lastOpenedAt;
            this.epubLocalPath = epubLocalPath == null ? "" : epubLocalPath;
            this.sourceKind = sourceKind == null || sourceKind.isEmpty()
                    ? "epub"
                    : sourceKind.toLowerCase(Locale.ROOT);
            this.sourceLocalPath = sourceLocalPath == null ? "" : sourceLocalPath;
            this.sourceHash = sourceHash == null ? "" : sourceHash.toLowerCase(Locale.ROOT);
            this.sourceByteSize = sourceByteSize;
            this.importState = importState == null || importState.isEmpty()
                    ? "remote"
                    : importState;
            this.assetIssueSummary = formatAssetIssueSummary(assetIssueType, assetIssueError);
            boolean correctionRequired = false;
            try {
                correctionRequired = new JSONObject(compatibilityJson == null ? "{}" : compatibilityJson)
                        .optBoolean("correction_queue_required");
            } catch (Throwable ignored) {
                // Missing compatibility metadata means reading remains available in original mode.
            }
            this.correctionQueueRequired = correctionRequired;
        }
    }

    static final class LocalCopyRemovalResult {
        final int removedFileCount;
        final int cleanupPendingCount;

        LocalCopyRemovalResult(int removedFileCount, int cleanupPendingCount) {
            this.removedFileCount = Math.max(0, removedFileCount);
            this.cleanupPendingCount = Math.max(0, cleanupPendingCount);
        }
    }

    static final class LocalFileCleanupResult {
        final int removedFileCount;
        final int pendingFileCount;

        LocalFileCleanupResult(int removedFileCount, int pendingFileCount) {
            this.removedFileCount = Math.max(0, removedFileCount);
            this.pendingFileCount = Math.max(0, pendingFileCount);
        }
    }

    private static final class LocalCopySnapshot {
        final String[] paths;
        final String importState;

        LocalCopySnapshot(String[] paths, String importState) {
            this.paths = paths;
            this.importState = clean(importState);
        }

        boolean samePaths(LocalCopySnapshot other) {
            if (other == null || paths.length != other.paths.length) {
                return false;
            }
            for (int index = 0; index < paths.length; index++) {
                if (!same(paths[index], other.paths[index])) {
                    return false;
                }
            }
            return true;
        }
    }

    static final class AnnotationRow {
        final String id;
        final String kind;
        final String sourceText;
        final String noteText;
        final String chapterLocator;
        final String rangeLocatorJson;
        final String updatedAt;
        final String metadataJson;
        final long serverVersion;

        AnnotationRow(String id, String kind, String sourceText, String noteText, String chapterLocator, String rangeLocatorJson, String updatedAt, String metadataJson, long serverVersion) {
            this.id = id == null ? "" : id;
            this.kind = kind == null ? "" : kind;
            this.sourceText = sourceText == null ? "" : sourceText;
            this.noteText = cleanOptionalText(noteText);
            this.chapterLocator = chapterLocator == null ? "" : chapterLocator;
            this.rangeLocatorJson = rangeLocatorJson == null ? "{}" : rangeLocatorJson;
            this.updatedAt = updatedAt == null ? "" : updatedAt;
            this.metadataJson = metadataJson == null ? "{}" : metadataJson;
            this.serverVersion = Math.max(0L, serverVersion);
        }
    }

    static final class ChapterRow {
        final int index;
        final String locator;
        final String title;

        ChapterRow(int index, String locator, String title) {
            this.index = index;
            this.locator = locator == null ? "" : locator;
            this.title = title == null || title.isEmpty() ? "第 " + (index + 1) + " 章" : title;
        }
    }
}
