package com.click.shell

import androidx.work.NetworkType
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class ClickSyncPolicyTest {
    @Test
    fun configDefaultsToUnmeteredAssetsAndCanonicalOrigin() {
        assertFalse(ClickSyncConfig.DEFAULT_ALLOW_METERED_ASSETS)
        assertEquals(
            "http://reader.example:18180",
            ClickSyncConfig.normalizeBaseUrl("reader.example", ""),
        )
        assertEquals(
            "https://reader.example:443",
            ClickSyncConfig.normalizeBaseUrl("https://reader.example", "443"),
        )
        assertEquals(
            "https://reader.example:443",
            ClickSyncConfig.normalizeBaseUrl("https://reader.example", "18180"),
        )
        assertEquals("", ClickSyncConfig.normalizeBaseUrl("", "18180"))
        assertEquals("", ClickSyncConfig.normalizeBaseUrl("file:///tmp/reader", "18180"))
    }

    @Test
    fun bonjourEndpointAcceptsOnlyPrivateIpv4() {
        val privateLanHost = listOf("192", "168", "20", "15").joinToString(".")
        val carrierGradeNatHost = listOf("100", "64", "0", "1").joinToString(".")
        assertEquals(
            "http://$privateLanHost:18180",
            ClickLanEndpointPolicy.originFor(privateLanHost, 18180),
        )
        assertEquals(
            "http://10.4.5.6:18180",
            ClickLanEndpointPolicy.originFor("10.4.5.6", 18180),
        )
        assertEquals("", ClickLanEndpointPolicy.originFor("127.0.0.1", 18180))
        assertEquals("", ClickLanEndpointPolicy.originFor(carrierGradeNatHost, 18180))
        assertEquals("", ClickLanEndpointPolicy.originFor("8.8.8.8", 18180))
        assertEquals("", ClickLanEndpointPolicy.originFor(privateLanHost, 0))
        assertEquals("", ClickLanEndpointPolicy.originFor(privateLanHost, 65_536))
    }

    @Test
    fun schedulerSeparatesSmallAndAssetNetworkContracts() {
        assertEquals(NetworkType.UNMETERED, ClickSyncScheduler.assetNetworkType(false))
        assertEquals(NetworkType.CONNECTED, ClickSyncScheduler.assetNetworkType(true))
        assertFalse(
            ClickSyncScheduler.assetLaneRequiresCharging(
                ClickSyncScheduler.LANE_IMMEDIATE,
            ),
        )
        assertTrue(
            ClickSyncScheduler.assetLaneRequiresCharging(
                ClickSyncScheduler.LANE_CHARGING_BACKLOG,
            ),
        )
        assertEquals(0L, ClickSyncScheduler.delayUntil(1_000L, 900L))
        assertEquals(4_000L, ClickSyncScheduler.delayUntil(1_000L, 5_000L))
        assertEquals(1, ClickSyncWorker.MAX_ASSETS_PER_RUN)
        assertTrue(
            ClickSyncWorker.validModeLane(
                ClickSyncScheduler.MODE_METADATA,
                ClickSyncScheduler.LANE_IMMEDIATE,
            ),
        )
        assertTrue(
            ClickSyncWorker.validModeLane(
                ClickSyncScheduler.MODE_METADATA_REPAIR,
                ClickSyncScheduler.LANE_METADATA_REPAIR,
            ),
        )
        assertTrue(
            ClickSyncWorker.validModeLane(
                ClickSyncScheduler.MODE_ASSETS,
                ClickSyncScheduler.LANE_DELAYED_RETRY,
            ),
        )
        assertTrue(
            ClickSyncWorker.validModeLane(
                ClickSyncScheduler.MODE_ASSETS,
                ClickSyncScheduler.LANE_CHARGING_BACKLOG,
            ),
        )
        assertTrue(
            ClickSyncWorker.validModeLane(
                ClickSyncScheduler.MODE_ASSETS,
                ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE,
            ),
        )
        assertTrue(
            ClickSyncWorker.validBookScope(
                ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE,
                "book-A",
            ),
        )
        assertFalse(
            ClickSyncWorker.validBookScope(
                ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE,
                "",
            ),
        )
        assertFalse(
            ClickSyncWorker.validBookScope(
                ClickSyncScheduler.LANE_IMMEDIATE,
                "book-A",
            ),
        )
        assertFalse(
            ClickSyncWorker.validModeLane(
                ClickSyncScheduler.MODE_ASSETS,
                ClickSyncScheduler.LANE_METADATA_REPAIR,
            ),
        )
    }

    @Test
    fun explicitBookSourceRoutingCannotBroadenToAnotherAssetOrBook() {
        val source = ClickNativeStore.AssetRefreshRow(
            "book-A",
            ClickNativeStore.ASSET_SOURCE,
            "/v1/android/books/book-A/source",
            "a".repeat(64),
            1_048_576L,
            "contract",
            "pending",
            0,
            "",
            0L,
        )
        val cover = ClickNativeStore.AssetRefreshRow(
            "book-A",
            ClickNativeStore.ASSET_COVER,
            "/cover",
            "",
            -1L,
            "contract",
            "pending",
            0,
            "",
            0L,
        )

        assertTrue(ClickSyncEngine.isRequestedBookSource(source, "book-A"))
        assertFalse(ClickSyncEngine.isRequestedBookSource(source, "book-B"))
        assertFalse(ClickSyncEngine.isRequestedBookSource(cover, "book-A"))
        assertEquals(
            ClickSyncScheduler.bookSourceWorkName("book-A"),
            ClickSyncScheduler.bookSourceWorkName("book-A"),
        )
        assertFalse(
            ClickSyncScheduler.bookSourceWorkName("book-A") ==
                ClickSyncScheduler.bookSourceWorkName("book-B"),
        )
        assertEquals("1.0 MB", ClickNativeReaderActivity.formatEstimatedBytes(1_048_576L))
    }

    @Test
    fun engineClassifiesRetryAuthAndPermanentFailures() {
        assertEquals(
            ClickSyncEngine.Outcome.Status.RETRYABLE_FAILURE,
            ClickSyncEngine.classifyHttpStatus(429),
        )
        assertEquals(
            ClickSyncEngine.Outcome.Status.RETRYABLE_FAILURE,
            ClickSyncEngine.classifyHttpStatus(503),
        )
        assertEquals(
            ClickSyncEngine.Outcome.Status.AUTH_REQUIRED,
            ClickSyncEngine.classifyHttpStatus(401),
        )
        assertEquals(
            ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE,
            ClickSyncEngine.classifyHttpStatus(422),
        )
        assertEquals(
            ClickSyncEngine.Outcome.Status.RETRYABLE_FAILURE,
            ClickSyncEngine.classify(IOException("offline")),
        )
        assertEquals(
            ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE,
            ClickSyncEngine.classify(ClickSyncEngine.SyncContractException("bad cursor")),
        )
    }

    @Test
    fun baselineUsesAnExplicitMarkerNotSequenceZero() {
        assertTrue(ClickSyncEngine.shouldRunFullBaseline(false))
        assertFalse(ClickSyncEngine.shouldRunFullBaseline(true))
        assertEquals(
            "/v1/android/sync/full?include_chapters=false",
            ClickSyncEngine.FULL_BASELINE_PATH,
        )
    }

    @Test
    fun assetFileStemCannotEscapeThePrivateCache() {
        assertEquals(
            "book-safe_123",
            ClickSyncEngine.safeAssetFileStem("book-safe_123"),
        )
        val hostile = ClickSyncEngine.safeAssetFileStem("../../Library/secret")
        assertFalse(hostile.contains("/"))
        assertFalse(hostile == "." || hostile == "..")
    }

    @Test
    fun workerRetriesOnlyBusyOrTransientRunFailures() {
        assertEquals(
            ClickSyncWorker.Action.RETRY,
            ClickSyncWorker.actionForStatus(ClickSyncEngine.Outcome.Status.BUSY),
        )
        assertEquals(
            ClickSyncWorker.Action.RETRY,
            ClickSyncWorker.actionForStatus(
                ClickSyncEngine.Outcome.Status.RETRYABLE_FAILURE,
            ),
        )
        assertEquals(
            ClickSyncWorker.Action.FAILURE,
            ClickSyncWorker.actionForStatus(ClickSyncEngine.Outcome.Status.AUTH_REQUIRED),
        )
        assertEquals(
            ClickSyncWorker.Action.FAILURE,
            ClickSyncWorker.actionForStatus(
                ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE,
            ),
        )
        assertEquals(
            ClickSyncWorker.Action.SUCCESS,
            ClickSyncWorker.actionForStatus(ClickSyncEngine.Outcome.Status.NOT_CONFIGURED),
        )
    }

    @Test
    fun operationReceiptsMustMatchTheRequestIdsExactlyOnce() {
        val pending = operationIds("A", "B")
        assertTrue(
            ClickSyncEngine.operationReceiptIdsMatchExactly(
                pending,
                operationIds("B", "A"),
            ),
        )
        assertFalse(
            ClickSyncEngine.operationReceiptIdsMatchExactly(
                pending,
                operationIds("A"),
            ),
        )
        assertFalse(
            ClickSyncEngine.operationReceiptIdsMatchExactly(
                pending,
                operationIds("A", "A"),
            ),
        )
        assertFalse(
            ClickSyncEngine.operationReceiptIdsMatchExactly(
                pending,
                operationIds("A", "C"),
            ),
        )
        assertFalse(
            ClickSyncEngine.operationReceiptIdsMatchExactly(
                pending,
                operationIds("A", " "),
            ),
        )
    }

    private fun operationIds(vararg ids: String): JSONArray {
        return JSONArray().apply {
            ids.forEach { id ->
                put(JSONObject().put("operation_id", id))
            }
        }
    }
}
