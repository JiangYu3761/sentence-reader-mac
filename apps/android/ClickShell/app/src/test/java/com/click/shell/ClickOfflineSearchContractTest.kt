package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ClickOfflineSearchContractTest {
    @Test
    fun likeWildcardsAreEscapedAsLiteralCharacters() {
        assertEquals(
            "100\\%\\_\\\\",
            ClickNativeStore.escapeLikeQuery("100%_\\"),
        )
    }

    @Test
    fun snippetsRemoveMarkupAndStayBoundedAroundTheMatch() {
        val html = """
            <style>.hidden { color: red; }</style>
            <p>${"开头".repeat(80)}目标句子${"结尾".repeat(80)}</p>
            <script>secret()</script>
        """.trimIndent()

        val snippet = ClickNativeStore.searchSnippet(html, "目标句子")

        assertTrue(snippet.contains("目标句子"))
        assertFalse(snippet.contains("<"))
        assertFalse(snippet.contains("hidden"))
        assertFalse(snippet.contains("secret"))
        assertTrue(snippet.length <= 142)
    }
}
