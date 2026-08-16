package com.click.shell

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class HermesBookQuestionContractTest {
    @Test
    fun currentBookRequestCarriesStableBookContextWithoutSentenceOrSelection() {
        val message = HermesBookQuestionContract.buildMessage(
            HermesBookQuestionContext(
                bookId = "book-42",
                bookTitle = "经济学原理",
                chapterTitle = "供给与需求",
                locatorHref = "text/chapter-3.xhtml",
            ),
            "作者为什么先讲稀缺性？",
        )

        assertTrue("调用范围是 current_book" in message)
        assertTrue("book_id：book-42" in message)
        assertTrue("书名：经济学原理" in message)
        assertTrue("当前章节：供给与需求" in message)
        assertTrue("用户关于本书的问题：作者为什么先讲稀缺性？" in message)
        assertTrue("Mac 服务端会核验书籍身份" in message)
        assertTrue("不等于穷尽检索全书" in message)
        assertFalse("当前朗读句：" in message)
        assertFalse("所选原文：" in message)
    }

    @Test
    fun questionInputIsBoundedAndControlCharactersAreRemoved() {
        val cleaned = HermesBookQuestionContract.cleanQuestion("  为什么？\u0000" + "问".repeat(3_000))
        assertFalse('\u0000' in cleaned)
        assertTrue(cleaned.length <= 2_000)
    }
}
