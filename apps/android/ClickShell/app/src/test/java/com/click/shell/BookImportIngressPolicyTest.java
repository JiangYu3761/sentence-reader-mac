package com.click.shell;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

public final class BookImportIngressPolicyTest {
    @Test
    public void streamCopyIsBoundedHashedAndDeletesRejectedPart() throws Exception {
        File directory = Files.createTempDirectory("click-import-copy").toFile();
        File accepted = new File(directory, "accepted.part");
        byte[] payload = "%PDF-1.7\nsafe".getBytes(StandardCharsets.UTF_8);

        BookImportIngress.CopiedSource copied = BookImportIngress.copyAndHash(
                new ByteArrayInputStream(payload),
                accepted,
                payload.length
        );

        assertEquals(payload.length, copied.byteSize);
        assertEquals(64, copied.sha256.length());
        assertTrue(accepted.isFile());

        File rejected = new File(directory, "rejected.part");
        try {
            BookImportIngress.copyAndHash(
                    new ByteArrayInputStream(payload),
                    rejected,
                    payload.length - 1L
            );
            fail("oversized source was accepted");
        } catch (BookImportIngress.ImportException expected) {
            assertFalse(rejected.exists());
        }
    }

    @Test
    public void actualBytesDecidePdfAndEpubKind() throws Exception {
        File directory = Files.createTempDirectory("click-import-kind").toFile();
        File pdf = new File(directory, "wrong-name.epub");
        Files.write(pdf.toPath(), "%PDF-1.7\nbody".getBytes(StandardCharsets.UTF_8));
        assertEquals("pdf", BookImportIngress.detectSourceKind(pdf));

        File epub = new File(directory, "book.epub");
        writeMinimalEpub(epub, false);
        assertEquals("epub", BookImportIngress.detectSourceKind(epub));
    }

    @Test
    public void legacyCompressedMimetypeIsAcceptedWhenItsContentIsCorrect() throws Exception {
        File epub = File.createTempFile("click-import-compressed-mimetype-", ".epub");
        writeMinimalEpub(epub, true);

        assertEquals("epub", BookImportIngress.detectSourceKind(epub));
    }

    @Test
    public void fakeZipIsRejectedAsAnEpub() throws Exception {
        File fake = File.createTempFile("click-import-fake-", ".epub");
        try (ZipOutputStream output = new ZipOutputStream(new FileOutputStream(fake))) {
            output.putNextEntry(new ZipEntry("book.txt"));
            output.write("not an epub".getBytes(StandardCharsets.UTF_8));
            output.closeEntry();
        }

        try {
            BookImportIngress.detectSourceKind(fake);
            fail("ZIP without EPUB contracts was accepted");
        } catch (BookImportIngress.ImportException expected) {
            assertTrue(expected.getMessage().contains("EPUB"));
        }
    }

    @Test
    public void contentHashOwnsStableLocalIdentityAndSafeName() throws Exception {
        String hash = "ab".repeat(32);
        assertEquals(
                "android-local-" + hash,
                BookImportIngress.stableLocalBookId(hash)
        );
        assertEquals(
                "unsafe name.pdf",
                BookImportIngress.safeFilename("../unsafe\nname.epub", "pdf")
        );
        assertEquals(
                "plain.pdf",
                BookImportIngress.safeFilename("plain.pdf", "pdf")
        );
    }

    private static void writeMinimalEpub(File target, boolean compressMimetype) throws Exception {
        byte[] mime = "application/epub+zip".getBytes(StandardCharsets.UTF_8);
        CRC32 crc = new CRC32();
        crc.update(mime);
        try (ZipOutputStream output = new ZipOutputStream(new FileOutputStream(target))) {
            ZipEntry mimeEntry = new ZipEntry("mimetype");
            if (!compressMimetype) {
                mimeEntry.setMethod(ZipEntry.STORED);
                mimeEntry.setSize(mime.length);
                mimeEntry.setCompressedSize(mime.length);
                mimeEntry.setCrc(crc.getValue());
            }
            output.putNextEntry(mimeEntry);
            output.write(mime);
            output.closeEntry();

            output.putNextEntry(new ZipEntry("META-INF/container.xml"));
            output.write("<container/>".getBytes(StandardCharsets.UTF_8));
            output.closeEntry();
        }
    }
}
