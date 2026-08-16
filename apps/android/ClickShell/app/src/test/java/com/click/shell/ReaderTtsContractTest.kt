package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderTtsContractTest {
    @Test
    fun spokenGlossUsesChinesePartOfSpeechNames() {
        val spoken = ReaderTtsTextNormalizer.normalize("prayer n. 祷告; anoint v. 膏抹; divine adj. 神圣的")
        assertTrue("名词" in spoken)
        assertTrue("动词" in spoken)
        assertTrue("形容词" in spoken)
        assertFalse("n." in spoken)
        assertFalse("v." in spoken)
    }

    @Test
    fun longTextIsSplitIntoPlayableSentences() {
        assertEquals(3, ReaderTtsTextNormalizer.segment("第一句。第二句！Third sentence.").size)
    }

    @Test
    fun tableOfContentsLinesRemainSeparateSpokenSegments() {
        val segments = ReaderTtsTextNormalizer.segment(
            "第一篇、前言\n第二篇、生命与建造的引言（一）\n第三篇、生命与建造的引言（二）",
        )
        assertEquals(3, segments.size)
        assertEquals("第一篇、前言", segments.first())
    }

    @Test
    fun stateMachineRejectsImpossibleResumeFromIdle() {
        assertFalse(ReaderTtsTransitionPolicy.allows(ReaderTtsState.IDLE, ReaderTtsState.PLAYING))
        assertTrue(ReaderTtsTransitionPolicy.allows(ReaderTtsState.IDLE, ReaderTtsState.PREPARING))
        assertTrue(ReaderTtsTransitionPolicy.allows(ReaderTtsState.PREPARING, ReaderTtsState.PLAYING))
        assertTrue(ReaderTtsTransitionPolicy.allows(ReaderTtsState.PLAYING, ReaderTtsState.PAUSED))
        assertTrue(ReaderTtsTransitionPolicy.allows(ReaderTtsState.PAUSED, ReaderTtsState.PLAYING))
    }

    @Test
    fun everyLocalModeFallsBackToAnInstalledAndroidOfflineVoice() {
        assertTrue(
            ReaderTtsFallbackPolicy.useAndroidOfflineVoice(
                ReaderTtsMode.LOCAL_PRIVATE,
                hasPackagedVoice = false,
            ),
        )
        assertTrue(
            ReaderTtsFallbackPolicy.useAndroidOfflineVoice(
                ReaderTtsMode.SMART,
                hasPackagedVoice = false,
            ),
        )
        assertFalse(
            ReaderTtsFallbackPolicy.useAndroidOfflineVoice(
                ReaderTtsMode.LOCAL_PRIVATE,
                hasPackagedVoice = true,
            ),
        )
    }

    @Test
    fun explicitMicrosoftModeNeverSilentlyChangesProvider() {
        assertFalse(
            ReaderTtsFallbackPolicy.useAndroidOfflineVoice(
                ReaderTtsMode.MICROSOFT_ONLINE,
                hasPackagedVoice = false,
            ),
        )
    }

    @Test
    fun firstTapWaitsForSystemInitializationInsteadOfFailing() {
        assertEquals(
            ReaderTtsSystemAction.WAIT,
            ReaderTtsSystemPolicy.action(ReaderTtsSystemState.INITIALIZING),
        )
        assertEquals(
            ReaderTtsSystemAction.SPEAK,
            ReaderTtsSystemPolicy.action(ReaderTtsSystemState.READY),
        )
        assertEquals(
            ReaderTtsSystemAction.ERROR,
            ReaderTtsSystemPolicy.action(ReaderTtsSystemState.UNAVAILABLE),
        )
        assertTrue(
            ReaderTtsSystemPolicy.shouldResumePending(
                pendingToken = 42,
                sessionToken = 42,
                state = ReaderTtsSystemState.READY,
            ),
        )
        assertFalse(
            ReaderTtsSystemPolicy.shouldResumePending(
                pendingToken = 41,
                sessionToken = 42,
                state = ReaderTtsSystemState.READY,
            ),
        )
        assertFalse(
            ReaderTtsSystemPolicy.shouldResumePending(
                pendingToken = 42,
                sessionToken = 42,
                state = ReaderTtsSystemState.UNAVAILABLE,
            ),
        )
    }

    @Test
    fun chineseTextNeverFallsThroughToAnEnglishOfflineVoice() {
        assertEquals(
            "zh",
            ReaderTtsSystemPolicy.chooseLanguage(listOf("en", "zh"), "这是中文正文"),
        )
        assertEquals(
            null,
            ReaderTtsSystemPolicy.chooseLanguage(listOf("en"), "这是中文正文"),
        )
        assertEquals(
            "en",
            ReaderTtsSystemPolicy.chooseLanguage(listOf("en"), "English text"),
        )
    }
}
