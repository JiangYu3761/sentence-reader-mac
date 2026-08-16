package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Test

class ClickNativeStoreNullableMetadataTest {
    @Test
    fun missingReadingProfileFallsBackToUnknown() {
        assertEquals("UNKNOWN", ClickNativeStore.normalizedReadingProfile(null))
        assertEquals("UNKNOWN", ClickNativeStore.normalizedReadingProfile("  "))
    }

    @Test
    fun existingReadingProfileIsPreserved() {
        assertEquals(
            "IMAGE_COMIC",
            ClickNativeStore.normalizedReadingProfile(" IMAGE_COMIC "),
        )
    }

    @Test
    fun literalJsonNullIsNotRenderedAsAnnotationNote() {
        val globalRow = ClickNativeStore.GlobalAnnotationRow(
            "annotation-1",
            "book-1",
            "Book",
            "Author",
            "highlight",
            "Source",
            "null",
            "chapter-1",
            "Chapter",
            "2026-07-29T00:00:00Z",
        )
        val annotationRow = ClickNativeStore.AnnotationRow(
            "annotation-1",
            "highlight",
            "Source",
            "null",
            "chapter-1",
            "{}",
            "2026-07-29T00:00:00Z",
            "{}",
            1L,
        )

        assertEquals("", globalRow.noteText)
        assertEquals("", annotationRow.noteText)
    }

    @Test
    fun realAnnotationNoteTextIsPreservedAndTrimmed() {
        val row = ClickNativeStore.GlobalAnnotationRow(
            "annotation-1",
            "book-1",
            "Book",
            "Author",
            "note",
            "Source",
            "  My note  ",
            "chapter-1",
            "Chapter",
            "2026-07-29T00:00:00Z",
        )

        assertEquals("My note", row.noteText)
    }
}
