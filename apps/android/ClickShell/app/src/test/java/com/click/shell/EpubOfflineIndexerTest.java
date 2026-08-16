package com.click.shell;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

public final class EpubOfflineIndexerTest {
    @Test
    public void spineOrderAndAllChapterTextSurviveASecondIndexPass() throws Exception {
        File epub = Files.createTempFile("click-offline-index-", ".epub").toFile();
        writeEpub(epub, false);

        List<EpubOfflineIndexer.Chapter> first = EpubOfflineIndexer.index(epub);
        List<EpubOfflineIndexer.Chapter> afterRestart = EpubOfflineIndexer.index(epub);

        assertEquals(2, first.size());
        assertEquals("OEBPS/chapter-two.xhtml", first.get(0).locator);
        assertEquals("第二章", first.get(0).title);
        assertTrue(first.get(0).html.contains("重启以后仍然可以搜索"));
        assertEquals("OEBPS/chapter-one.xhtml", first.get(1).locator);
        assertEquals(first.get(0).html, afterRestart.get(0).html);
        assertEquals(first.get(1).html, afterRestart.get(1).html);
    }

    @Test
    public void packageTraversalCannotReadOutsideArchiveRoot() throws Exception {
        File epub = Files.createTempFile("click-offline-index-escape-", ".epub").toFile();
        writeEpub(epub, true);
        try {
            EpubOfflineIndexer.index(epub);
            fail("escaping package path was accepted");
        } catch (SecurityException expected) {
            assertTrue(expected.getMessage().contains("archive root"));
        }
    }

    @Test
    public void xmlDocumentTypeCannotResolveExternalEntities() throws Exception {
        File epub = Files.createTempFile("click-offline-index-xxe-", ".epub").toFile();
        writeDoctypeEpub(epub);

        try {
            EpubOfflineIndexer.index(epub);
            fail("EPUB XML document type was accepted");
        } catch (SecurityException expected) {
            assertTrue(expected.getMessage().contains("declarations"));
        }
    }

    private static void writeEpub(File target, boolean escapingPackage) throws Exception {
        byte[] mime = "application/epub+zip".getBytes(StandardCharsets.UTF_8);
        CRC32 crc = new CRC32();
        crc.update(mime);
        try (ZipOutputStream output = new ZipOutputStream(new FileOutputStream(target))) {
            ZipEntry mimeEntry = new ZipEntry("mimetype");
            mimeEntry.setMethod(ZipEntry.STORED);
            mimeEntry.setSize(mime.length);
            mimeEntry.setCompressedSize(mime.length);
            mimeEntry.setCrc(crc.getValue());
            output.putNextEntry(mimeEntry);
            output.write(mime);
            output.closeEntry();

            add(
                    output,
                    "META-INF/container.xml",
                    "<?xml version=\"1.0\"?><container xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">"
                            + "<rootfiles><rootfile full-path=\""
                            + (escapingPackage ? "../../outside.opf" : "OEBPS/package.opf")
                            + "\" media-type=\"application/oebps-package+xml\"/></rootfiles></container>"
            );
            if (!escapingPackage) {
                add(
                        output,
                        "OEBPS/package.opf",
                        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><package xmlns=\"http://www.idpf.org/2007/opf\">"
                                + "<manifest>"
                                + "<item id=\"one\" href=\"chapter-one.xhtml\" media-type=\"application/xhtml+xml\"/>"
                                + "<item id=\"two\" href=\"chapter-two.xhtml\" media-type=\"application/xhtml+xml\"/>"
                                + "</manifest><spine><itemref idref=\"two\"/><itemref idref=\"one\"/></spine></package>"
                );
                add(output, "OEBPS/chapter-one.xhtml", "<html><body><h1>第一章</h1><p>全文甲</p></body></html>");
                add(output, "OEBPS/chapter-two.xhtml", "<html><body><h1>第二章</h1><p>重启以后仍然可以搜索</p></body></html>");
            }
        }
    }

    private static void add(ZipOutputStream output, String path, String value) throws Exception {
        output.putNextEntry(new ZipEntry(path));
        output.write(value.getBytes(StandardCharsets.UTF_8));
        output.closeEntry();
    }

    private static void writeDoctypeEpub(File target) throws Exception {
        byte[] mime = "application/epub+zip".getBytes(StandardCharsets.UTF_8);
        CRC32 crc = new CRC32();
        crc.update(mime);
        try (ZipOutputStream output = new ZipOutputStream(new FileOutputStream(target))) {
            ZipEntry mimeEntry = new ZipEntry("mimetype");
            mimeEntry.setMethod(ZipEntry.STORED);
            mimeEntry.setSize(mime.length);
            mimeEntry.setCompressedSize(mime.length);
            mimeEntry.setCrc(crc.getValue());
            output.putNextEntry(mimeEntry);
            output.write(mime);
            output.closeEntry();
            add(
                    output,
                    "META-INF/container.xml",
                    "<?xml version=\"1.0\"?>"
                            + "<!DOCTYPE container [<!ENTITY xxe SYSTEM \"file:///etc/passwd\">]>"
                            + "<container xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">"
                            + "<rootfiles><rootfile full-path=\"&xxe;\"/></rootfiles></container>"
            );
        }
    }
}
