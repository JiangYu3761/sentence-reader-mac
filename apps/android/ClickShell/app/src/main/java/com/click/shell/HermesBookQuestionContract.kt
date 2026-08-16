package com.click.shell

data class HermesBookQuestionContext(
    val bookId: String,
    val bookTitle: String,
    val chapterTitle: String,
    val locatorHref: String,
)

object HermesBookQuestionContract {
    private const val MAX_QUESTION_CHARACTERS = 2_000

    fun cleanQuestion(value: String): String =
        value
            .replace(Regex("[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F]"), " ")
            .replace(Regex("[ \\t]+"), " ")
            .trim()
            .take(MAX_QUESTION_CHARACTERS)

    fun buildMessage(context: HermesBookQuestionContext, question: String): String {
        val cleanQuestion = cleanQuestion(question)
        require(cleanQuestion.isNotBlank()) { "question is empty" }
        return """
            你正在处理 Click 阅读器中用户明确指定的当前书籍。调用范围是 current_book，不依赖当前朗读句或文字选区，也不要沿用其他书籍的会话上下文。
            book_id：${oneLine(context.bookId, "unknown")}
            书名：${oneLine(context.bookTitle, "当前书籍")}
            当前章节：${oneLine(context.chapterTitle, "当前章节")}
            当前定位：${oneLine(context.locatorHref, "unknown")}

            用户关于本书的问题：$cleanQuestion

            Mac 服务端会核验书籍身份，并从已索引原文、批注和可用书籍资料中限量取证；这不等于穷尽检索全书。证据不足时请明确说明，不要把其他书或常识冒充本书原意。
        """.trimIndent()
    }

    private fun oneLine(value: String, fallback: String): String =
        value.replace(Regex("\\s+"), " ").trim().take(500).ifBlank { fallback }
}
